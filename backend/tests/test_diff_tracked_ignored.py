"""Story diff with tracked-but-gitignored files (homelab2 `.playwright-mcp/`).

`_stage_scratch_index` used to run `add -A` into an EMPTY index, which skips
tracked files matching .gitignore, so `git diff <base>` showed them deleted
and the AI reviewer rejected stories on files the branch never touched."""

import json
import subprocess
import time

from conftest import MINIMAL_SPEC
from fastapi.testclient import TestClient

from app import workspaces
from app.main import create_app

IGNORED = ".playwright-mcp/a.log"


def _run(repo, *args):
    subprocess.run(list(args), cwd=repo, check=True, capture_output=True)


def init_repo(tmp_path) -> str:
    repo = tmp_path / "project"
    repo.mkdir()
    (repo / "README.md").write_text("# demo repo\n")
    (repo / ".gitignore").write_text(".playwright-mcp/\n")
    (repo / ".playwright-mcp").mkdir()
    (repo / IGNORED).write_text("log line\n")
    _run(repo, "git", "init", "-q")
    _run(repo, "git", "add", "-A")
    _run(repo, "git", "add", "-f", IGNORED)
    _run(repo, "git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    return str(repo)


def test_diff_ignores_untouched_tracked_ignored_files(tmp_path):
    repo = init_repo(tmp_path)
    wt, base, _ = workspaces.create(repo, "story_ti_a")
    (wt / "feature.py").write_text("X = 1\n")
    d = workspaces.diff(repo, "story_ti_a", base, max_chars=None)
    assert "feature.py" in d
    assert ".playwright-mcp" not in d
    assert "deleted file mode" not in d


def test_deleting_tracked_ignored_file_shows_in_diff(tmp_path):
    repo = init_repo(tmp_path)
    wt, base, _ = workspaces.create(repo, "story_ti_d")
    (wt / "feature.py").write_text("X = 1\n")
    (wt / IGNORED).unlink()
    d = workspaces.diff(repo, "story_ti_d", base, max_chars=None)
    assert "deleted file mode" in d
    assert ".playwright-mcp/a.log" in d


def test_editing_tracked_ignored_file_shows_in_diff(tmp_path):
    repo = init_repo(tmp_path)
    wt, base, _ = workspaces.create(repo, "story_ti_e")
    (wt / IGNORED).write_text("changed\n")
    d = workspaces.diff(repo, "story_ti_e", base, max_chars=None)
    assert "+changed" in d
    assert "deleted file mode" not in d


def test_story_touched_paths_excludes_untouched_tracked_ignored(tmp_path):
    repo = init_repo(tmp_path)
    wt, _, _ = workspaces.create(repo, "story_ti_f")
    (wt / "feature.py").write_text("X = 1\n")
    assert workspaces._story_touched_paths(repo, "story_ti_f") == ["feature.py"]


def test_diff_endpoint_and_reviewer_input_skip_tracked_ignored(tmp_path, monkeypatch):
    repo = init_repo(tmp_path)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "ti.db"))
    review_prompts: list[str] = []

    class GW:
        def __init__(self):
            self.calls = 0

        async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
            from app.providers import Delta as D
            from app.providers import ToolCall as TC
            from app.providers import Usage as U

            self.calls += 1
            user_msg = next((m["content"] for m in messages if m["role"] == "user"), "")
            if self.calls == 1:
                yield D(text=MINIMAL_SPEC)
            elif self.calls == 2:
                yield D(text="Spec looks good.\n\nVERDICT: APPROVED")
            elif self.calls == 3:
                yield TC(
                    id="w1",
                    name="write_file",
                    arguments=json.dumps({"path": "feature.py", "content": "X = 1\n"}),
                )
            elif self.calls == 4:
                yield D(text="Done.")
            else:
                review_prompts.append(user_msg)
                yield D(text="Fine.\n\nVERDICT: APPROVED")
            yield U(prompt_tokens=10, completion_tokens=10)

    def wait(client, sid):
        deadline = time.time() + 30
        while time.time() < deadline:
            cur = client.get(f"/api/v1/stories/{sid}").json()
            if cur["status"] in ("human_review", "failed"):
                return cur
            time.sleep(0.05)
        return cur

    with TestClient(create_app(gateway=GW())) as client:
        client.patch(
            "/api/v1/settings",
            json={
                "story_poll_interval": 0.05,
                "project_root": repo,
                "write_tools_enabled": True,
                "max_tool_calls": 8,
            },
        )
        story = client.post("/api/v1/stories", json={"title": "tracked ignored"}).json()
        sid = story["id"]
        client.post(f"/api/v1/stories/{sid}/queue")
        assert wait(client, sid)["status"] == "human_review"
        client.post(f"/api/v1/stories/{sid}/approve", json={"by": "t"})
        cur = wait(client, sid)
        assert cur["status"] == "human_review", cur.get("error")

        r = client.get(f"/api/v1/stories/{sid}/diff")
        assert r.status_code == 200, r.text
        assert "feature.py" in r.text
        assert ".playwright-mcp" not in r.text
        assert "deleted file mode" not in r.text

    assert review_prompts, "code reviewer never ran"
    assert "feature.py" in review_prompts[-1]
    assert all(".playwright-mcp" not in p for p in review_prompts)
