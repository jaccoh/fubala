"""Agent tools: sandbox, execution caps, and the tool round-trip loop."""

import json

import pytest


@pytest.fixture
def anyio_backend():
    return "asyncio"

from app.agent import run_agent
from app.agent_tools import ToolBox
from app.providers import Delta, ToolCall, Usage


@pytest.fixture
def project(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def main():\n    return 'fubala'\n")
    (tmp_path / "README.md").write_text("# project\nhello world\n")
    (tmp_path / ".hidden").write_text("secret")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.js").write_text("junk")
    return tmp_path


# ---- ToolBox -----------------------------------------------------------


def test_sandbox_blocks_traversal(project):
    box = ToolBox(engine=None, project_root=str(project))
    for bad in ("../etc/passwd", "/etc/passwd", "src/../../outside"):
        result = box.execute("read_file", json.dumps({"path": bad}))
        assert "error" in result


def test_list_read_search(project):
    box = ToolBox(engine=None, project_root=str(project))
    listing = box.execute("list_files", "{}")
    assert "src/app.py" in listing and "README.md" in listing
    assert ".hidden" not in listing and "junk.js" not in listing  # skipped dirs/files

    content = box.execute("read_file", json.dumps({"path": "src/app.py"}))
    assert "def main():" in content and "1:" in content

    hits = box.execute("search_files", json.dumps({"query": "hello"}))
    assert "README.md:2" in hits

    assert "error" in box.execute("read_file", json.dumps({"path": "nope.py"}))


def test_story_tools_use_the_board(client):
    story = client.post("/api/v1/stories", json={"title": "Tool story"}).json()
    box = ToolBox(engine=client.app.state.engine, project_root=None)
    listing = box.execute("list_stories", "{}")
    assert "Tool story" in listing
    detail = box.execute("get_story", json.dumps({"story_id": story["id"]}))
    assert "Tool story" in detail
    assert "error" in box.execute("get_story", json.dumps({"story_id": "missing"}))


def test_no_project_root_means_no_fs_tools(client):
    box = ToolBox(engine=client.app.state.engine, project_root=None)
    names = [t["function"]["name"] for t in box.schemas()]
    assert "list_files" not in names and "list_stories" in names


# ---- agent loop --------------------------------------------------------


class ToolUsingGateway:
    """Round 1: request list_files + read_file. Round 2: final text."""

    def __init__(self):
        self.calls: list[dict] = []

    async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
        self.calls.append({"messages": messages, "tools": tools})
        if len(self.calls) == 1:
            assert tools, "tools must be passed through"
            yield ToolCall(id="call_1", name="list_files", arguments="{}")
            yield ToolCall(id="call_2", name="read_file", arguments='{"path": "README.md"}')
            yield Usage(prompt_tokens=10, completion_tokens=5)
        else:
            yield Delta(text="spec based on ")
            yield Delta(text="the real files")
            yield Usage(prompt_tokens=30, completion_tokens=9)


def test_run_agent_executes_tools_and_feeds_results(project):
    import asyncio

    asyncio.run(_tool_round_trip(project))


async def _tool_round_trip(project):
    box = ToolBox(engine=None, project_root=str(project))
    gateway = ToolUsingGateway()
    seen_tools: list[str] = []

    result = await run_agent(
        gateway,
        model="echo/dev",
        messages=[{"role": "user", "content": "spec it"}],
        tools=box.schemas(),
        execute=box.execute,
        on_delta=lambda t: None,
        on_tool=lambda n, a, r: seen_tools.append(n),
    )

    assert result.text == "spec based on the real files"
    assert result.rounds == 2
    assert result.tool_calls == 2
    assert seen_tools == ["list_files", "read_file"]

    # the second round saw the tool results
    round2 = gateway.calls[1]["messages"]
    tool_msgs = [m for m in round2 if m.get("role") == "tool"]
    assert len(tool_msgs) == 2
    assert "hello world" in tool_msgs[1]["content"]
    assistant_msg = [m for m in round2 if m.get("tool_calls")]
    assert assistant_msg and assistant_msg[0]["tool_calls"][0]["function"]["name"] == "list_files"


def test_run_agent_without_tools_is_single_round():
    import asyncio

    from test_stories import SequencedGateway

    gateway = SequencedGateway([["just ", "text"]])

    async def go():
        return await run_agent(
            gateway, model="echo/dev", messages=[{"role": "user", "content": "hi"}]
        )

    result = asyncio.run(go())
    assert result.text == "just text"
    assert result.rounds == 1 and result.tool_calls == 0


# ---- chat integration ----------------------------------------------------


def test_chat_streams_tool_events(client, monkeypatch, tmp_path):
    (tmp_path / "notes.md").write_text("fubala notes live here")
    client.patch("/api/v1/settings", json={"project_root": str(tmp_path)})

    from conftest import FakeGateway

    from app.providers import ToolCall as TC

    class ToolChatGateway(FakeGateway):
        def __init__(self):
            super().__init__()
            self.round = 0

        async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
            from app.providers import Delta, Usage

            self.round += 1
            if self.round == 1:
                assert any(t["function"]["name"] == "read_file" for t in tools or [])
                yield TC(id="c1", name="read_file", arguments='{"path": "notes.md"}')
                yield Usage(prompt_tokens=1, completion_tokens=1)
            else:
                yield Delta(text="the notes say: fubala")
                yield Usage(prompt_tokens=2, completion_tokens=4)



    # swap the gateway on the live app for the scripted one
    app = client.app
    app.state.gateway = ToolChatGateway()
    sid = client.post("/api/v1/chat/sessions", json={}).json()["id"]
    res = client.post(
        f"/api/v1/chat/sessions/{sid}/messages",
        json={"content": "what do the notes say"},
    )
    assert res.status_code == 200

    from conftest import parse_sse

    events = parse_sse(res.text)
    kinds = [e for e, _ in events]
    assert "tool" in kinds
    tool_event = dict(events)["tool"]
    assert tool_event["name"] == "read_file"
    assert "fubala notes" in tool_event["preview"]
    assert kinds[-1] == "done"
    deltas = "".join(d["text"] for k, d in events if k == "delta")
    assert "the notes say: fubala" in deltas


def test_create_story_tool(tmp_path, monkeypatch):
    """The chat agent can write to the board; the coder toolbox cannot."""
    from sqlmodel import Session

    from app.agent_tools import ToolBox
    from app.db import init_db, make_engine
    from app.models import Story

    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "tools.db"))
    from app.config import get_settings
    engine = make_engine(get_settings())
    init_db(engine)

    chat = ToolBox(engine=engine, project_root=str(tmp_path), board_writable=True)
    names = [t["function"]["name"] for t in chat.schemas()]
    assert "create_story" in names

    res = chat.execute("create_story", json.dumps(
        {"title": "Add dark mode", "description": "Users want a theme toggle", "skill": "low"}
    ))
    assert "backlog" in res
    with Session(engine) as db:
        from sqlmodel import select as _select
        story = db.exec(_select(Story).where(Story.title == "Add dark mode")).one()
        assert story.created_by == "agent"
        assert story.skill == "low"

    listed = chat.execute("list_stories", "{}")
    assert story.id in listed

    coder = ToolBox(engine=engine, project_root=str(tmp_path))
    assert "create_story" not in [t["function"]["name"] for t in coder.schemas()]
    denied = json.loads(coder.execute("create_story", json.dumps(
        {"title": "nope", "description": "should be refused"}
    )))
    assert "error" in denied


@pytest.mark.anyio
async def test_empty_rounds_are_nudged_not_terminal():
    """GLM thinking-only rounds surface as no content + no tool calls; the
    loop must nudge and continue (fubala-prompts/005 'coder writes
    nothing'), and give up only after repeated true silence."""
    from app.agent import run_agent
    from app.providers import Delta, Usage

    class EmptyThenWork:
        def __init__(self):
            self.calls = 0

        async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
            self.calls += 1
            if self.calls == 1:
                # z.ai thinking-only round: placeholder text, no tool calls
                yield Delta(text="[System: Empty message content sanitised to satisfy protocol]\n")
            elif self.calls == 2:
                from app.providers import ToolCall as TC

                yield TC(id="w", name="list_files", arguments="{}")
            else:
                yield Delta(text="did the work")
            yield Usage(prompt_tokens=1, completion_tokens=1)

    gw = EmptyThenWork()
    executed = []

    async def execute(name, args):
        executed.append(name)
        return "(files)"

    res = await run_agent(gw, model="m", messages=[{"role": "user", "content": "go"}],
                          tools=[{"type": "function", "function": {"name": "list_files",
                                    "parameters": {"type": "object", "properties": {}}}}],
                          execute=execute)
    assert executed == ["list_files"]
    assert res.text == "did the work"
    # the nudge round is visible in what the gateway was sent
    assert any("no output" in str(m).lower() or "continue the task" in str(m).lower()
               for m in gw.__dict__.get("sent", [])) or gw.calls == 3

    class AlwaysEmpty:
        async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
            yield Usage(prompt_tokens=1, completion_tokens=1)

    res2 = await run_agent(AlwaysEmpty(), model="m",
                           messages=[{"role": "user", "content": "go"}])
    assert res2.text == ""  # gives up gracefully on true silence


def test_edit_file_exact_match_semantics(tmp_path):
    import json as _json

    from app.agent_tools import ToolBox

    f = tmp_path / "big.py"
    f.write_text("HEADER\n\ndef a():\n    return 1\n\ndef b():\n    return 2\n")
    box = ToolBox(engine=None, project_root=str(tmp_path), workspace=str(tmp_path),
                  deny_globs=(), writable=True)

    ok = box.execute("edit_file", _json.dumps({
        "path": "big.py",
        "old_text": "def a():\n    return 1",
        "new_text": "def a():\n    return 10",
    }))
    assert "1 replacement" in ok
    assert "return 10" in f.read_text() and "return 2" in f.read_text()

    missing = box.execute("edit_file", _json.dumps({
        "path": "big.py", "old_text": "NOT THERE", "new_text": "x",
    }))
    assert "not found" in missing

    dup = box.execute("edit_file", _json.dumps({
        "path": "big.py", "old_text": "return", "new_text": "yield",
    }))
    assert "occurs" in dup

    all_ok = box.execute("edit_file", _json.dumps({
        "path": "big.py", "old_text": "return", "new_text": "yield", "replace_all": True,
    }))
    assert "2 replacement" in all_ok

    escaped = box.execute("edit_file", _json.dumps({
        "path": "../outside.py", "old_text": "x", "new_text": "y",
    }))
    assert "escapes" in escaped

    denied = box.execute("edit_file", _json.dumps({
        "path": ".env", "old_text": "x", "new_text": "y",
    }))
    assert "sensitive-file policy" in denied


@pytest.mark.anyio
async def test_truncated_tool_arguments_are_retried_with_guidance():
    """fubala-prompts/006: the provider truncates very large single
    write_file argument JSON. The loop must treat the parse-error signature
    as retryable-with-guidance, not crash the run."""
    from app.agent import TRUNCATION_GUIDANCE, TRUNCATION_MARKERS, run_agent
    from app.providers import Delta, GatewayError, ToolCall, Usage

    class TruncatingThenWorking:
        def __init__(self):
            self.calls = 0
            self.seen = []

        async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
            self.calls += 1
            self.seen = [dict(m) for m in messages]
            if self.calls == 1:
                # the exact provider signature: real content, cut mid-string
                raise GatewayError(
                    "litellm.BadRequestError: AnthropicException - Failed to "
                    "parse tool call arguments for tool 'write_file' "
                    "Error: Unterminated string starting at: line 1 column 47"
                )
            if self.calls == 2:
                yield ToolCall(id="e1", name="edit_file",
                               arguments='{"path":"big.py","old_text":"1","new_text":"2"}')
            else:
                yield Delta(text="done after splitting the change")
            yield Usage(prompt_tokens=1, completion_tokens=1)

    gw = TruncatingThenWorking()
    executed = []

    async def execute(name, args):
        executed.append(name)
        return "ok"

    res = await run_agent(
        gw, model="m", messages=[{"role": "user", "content": "go"}],
        tools=[{"type": "function", "function": {"name": "edit_file",
                "parameters": {"type": "object", "properties": {}}}}],
        execute=execute, on_delta=lambda t: None,
    )
    assert executed == ["edit_file"], "run must survive truncation and continue"
    guidance_sent = any(TRUNCATION_GUIDANCE[:40] in str(m) for m in gw.seen)
    assert guidance_sent, "the split-the-change guidance must reach the model"

    # persistent truncation exhausts retries and raises (runner handles it)
    class AlwaysTruncated:
        async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
            raise GatewayError(
                "BadRequestError: Failed to parse tool call arguments - "
                "Unterminated string"
            )
            yield  # pragma: no cover

    try:
        await run_agent(AlwaysTruncated(), model="m",
                        messages=[{"role": "user", "content": "go"}])
        raised = False
    except GatewayError:
        raised = True
    assert raised, "exhausted truncation retries must surface the error"

    assert any(m in "failed to parse tool call arguments" for m in TRUNCATION_MARKERS)


@pytest.mark.anyio
async def test_bad_tool_args_400_is_corrected_in_round():
    """Incident e56b558d: the model emits a tool call whose arguments string
    is not JSON. The offending turn must be dropped, a corrective user
    message appended and on_correction fired — the run completes."""

    from app.agent import (
        bad_tool_args_correction,
        run_agent,
    )
    from app.providers import Delta, GatewayError, ToolCall, Usage

    class BadThenGood:
        def __init__(self):
            self.calls = 0
            self.seen: list[list[dict]] = []

        async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
            self.calls += 1
            self.seen.append([dict(m) for m in messages])
            if self.calls == 1:
                raise GatewayError(
                    "litellm.BadRequestError: AnthropicException - Failed to "
                    "parse tool call arguments for tool 'edit_file' "
                    "Error: Expecting property name enclosed in double quotes: "
                    "line 1 column 2 (char 1)"
                )
            if self.calls == 2:
                yield ToolCall(id="e1", name="edit_file",
                               arguments='{"path":"big.py","old_text":"1","new_text":"2"}')
            else:
                yield Delta(text="fixed and finished")
            yield Usage(prompt_tokens=1, completion_tokens=1)

    gw = BadThenGood()
    executed: list[str] = []
    corrections: list[tuple[str, int]] = []

    async def execute(name, args):
        executed.append(name)
        return "ok"

    res = await run_agent(
        gw, model="m", messages=[{"role": "user", "content": "go"}],
        tools=[{"type": "function", "function": {"name": "edit_file",
                "parameters": {"type": "object", "properties": {}}}}],
        execute=execute,
        on_correction=lambda tool, rnd: corrections.append((tool, rnd)),
    )
    assert res.text == "fixed and finished"
    assert executed == ["edit_file"]
    assert corrections == [("edit_file", 1)], "the tool name and round must be reported"
    # the corrective message names the tool and reached the gateway
    assert any(
        bad_tool_args_correction("edit_file") in str(m)
        for m in gw.seen[1]
    ), "the corrective user turn must reach the model"


@pytest.mark.anyio
async def test_bad_tool_args_400_raises_after_two_corrections():
    """A model stuck emitting garbage JSON: exactly MAX_BAD_TOOL_ARGS_CORRECTIONS
    in-round repairs, then the provider error surfaces (the story fails)."""
    from app.agent import MAX_BAD_TOOL_ARGS_CORRECTIONS, run_agent
    from app.providers import GatewayError

    class AlwaysBad:
        def __init__(self):
            self.calls = 0

        async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
            self.calls += 1
            raise GatewayError(
                "BadRequestError: Failed to parse tool call arguments for "
                "tool 'edit_file' Error: Expecting value: line 1 column 1"
            )
            yield  # pragma: no cover

    gw = AlwaysBad()
    corrections: list[tuple[str, int]] = []
    try:
        await run_agent(
            gw, model="m", messages=[{"role": "user", "content": "go"}],
            tools=[{"type": "function", "function": {"name": "edit_file",
                    "parameters": {"type": "object", "properties": {}}}}],
            execute=lambda n, a: "ok",
            on_correction=lambda tool, rnd: corrections.append((tool, rnd)),
        )
        raised = False
    except GatewayError:
        raised = True
    assert raised, "a persistent bad-args 400 must fail the run"
    assert len(corrections) == MAX_BAD_TOOL_ARGS_CORRECTIONS
    assert gw.calls == MAX_BAD_TOOL_ARGS_CORRECTIONS + 1


@pytest.mark.anyio
async def test_bad_tool_args_echo_poison_turn_is_dropped():
    """The echo case: the provider 400s because OUR stored assistant turn
    carries unparsable arguments (the truncation sub-marker may also be
    present — the poison turn routes it here, not to 006). The retried
    conversation must contain no unparsable assistant turn."""
    import json as _json

    from app.agent import run_agent
    from app.providers import Delta, GatewayError, ToolCall, Usage

    poison_args = '{"path":"big.py","old_text":"abc'  # cut mid-string, no closer

    class PoisonEcho:
        def __init__(self):
            self.calls = 0
            self.seen: list[list[dict]] = []

        async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
            self.calls += 1
            self.seen.append([dict(m) for m in messages])
            if self.calls == 1:
                # the model emits the poison call; run_agent stores it and the
                # executor reports the same unparsable JSON back
                yield ToolCall(id="p1", name="edit_file", arguments=poison_args)
                yield Usage(prompt_tokens=1, completion_tokens=1)
                return
            if self.calls == 2:
                # the NEXT stream echoes the stored turn: provider refuses
                raise GatewayError(
                    "litellm.BadRequestError: AnthropicException - Failed to "
                    "parse tool call arguments for tool 'edit_file' "
                    "Error: Unterminated string starting at: line 1 column 47"
                )
            if self.calls == 3:
                yield ToolCall(id="e1", name="edit_file",
                               arguments='{"path":"big.py","old_text":"abc","new_text":"z"}')
            else:
                yield Delta(text="recovered")
            yield Usage(prompt_tokens=1, completion_tokens=1)

    gw = PoisonEcho()
    corrections: list[tuple[str, int]] = []

    def execute(name, args):
        try:
            _json.loads(args)
        except ValueError:
            return "error: arguments are not valid JSON"
        return "1 replacement"

    res = await run_agent(
        gw, model="m", messages=[{"role": "user", "content": "go"}],
        tools=[{"type": "function", "function": {"name": "edit_file",
                "parameters": {"type": "object", "properties": {}}}}],
        execute=execute,
        on_correction=lambda tool, rnd: corrections.append((tool, rnd)),
    )
    assert res.text == "recovered"
    assert corrections == [("edit_file", 2)]
    # no assistant turn handed to the gateway after the correction carries
    # unparsable arguments
    for batch in gw.seen[2:]:
        for message in batch:
            for call in message.get("tool_calls") or []:
                _json.loads(call["function"]["arguments"])  # must not raise


def test_truncation_sub_marker_without_poison_turn_stays_006():
    """The discriminator: an 'unterminated string' 400 with NO poison
    assistant turn in history is the 006 truncation case — guidance, not the
    drop path. Pinned by the existing truncation test at the run level; this
    pins the predicate itself."""
    from app.agent import _unparsable_calls

    assert _unparsable_calls(None) == []
    assert _unparsable_calls({"role": "user", "content": "hi"}) == []
    assert _unparsable_calls({"role": "assistant", "content": "thinking"}) == []
    assert _unparsable_calls({
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": "a", "type": "function",
            "function": {"name": "edit_file", "arguments": '{"path":"x"}'},
        }],
    }) == []
    poison = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": "a", "type": "function",
            "function": {"name": "edit_file", "arguments": '{"path":"x'},
        }],
    }
    assert _unparsable_calls(poison) == poison["tool_calls"]



def test_prep_toolbox_creates_only_children_of_the_prep_story(client):
    from sqlmodel import Session, select

    from app.models import Story

    engine = client.app.state.engine
    prep = client.post("/api/v1/stories", json={"title": "prep", "kind": "prep"}).json()

    plain = ToolBox(engine=engine)
    assert "create_story" not in [t["function"]["name"] for t in plain.schemas()]

    box = ToolBox(engine=engine, prep_parent_id=prep["id"])
    assert "create_story" in [t["function"]["name"] for t in box.schemas()]
    refused = json.loads(box.execute("create_story", json.dumps(
        {"title": "stray", "description": "no link"}
    )))
    assert "error" in refused
    with Session(engine) as db:
        assert db.exec(select(Story).where(Story.title == "stray")).first() is None

    ok = box.execute("create_story", json.dumps(
        {"title": "child", "description": "d", "depends_on": [prep["id"]]}
    ))
    assert "created story" in ok
    assert len(box.created_ids) == 1
