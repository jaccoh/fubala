"""Tests for Fubala prompt 007: AI reviewer diff cap and chunking.

Acceptance criteria verified:
1. Workspaces diff supports capped (UI display) and uncapped (reviewer) output,
   marking truncated diffs clearly with notice text.
2. Reviewer processes diff in chunks when it exceeds the chunk cap, without
   rejecting intermediate chunks for missing files that live in subsequent chunks.
3. Regression test: A story with a diff > cap gets a review verdict that
   references content from the FINAL chunk (placed past the cap boundary).
"""

import json
import subprocess
import time
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from app import workspaces
from app.main import create_app
from app.runner import DIFF_CHUNK_SIZE, chunk_diff

from conftest import MINIMAL_SPEC


def init_repo(tmp_path) -> str:
    repo = tmp_path / "project"
    repo.mkdir()
    (repo / "README.md").write_text("# demo repo\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"],
        cwd=repo,
        check=True,
    )
    return str(repo)


def test_workspaces_diff_capped_and_uncapped(tmp_path):
    """AC1: 60k cap remains for UI/payload display clearly marked truncated;
    pass max_chars=None for complete raw diff."""
    repo = init_repo(tmp_path)
    wt, base, _ = workspaces.create(repo, "story_diff_cap")

    # Create a large diff (~75,000 characters)
    large_payload = "X" * 100 + "\n"
    content = large_payload * 750
    marker = "UNIQUE_MARKER_PAST_60K_BOUNDARY"
    (wt / "large_file.py").write_text(content + marker + "\n")

    # Default call (used for UI code_diff payload) caps at 60_000 and adds note
    diff_capped = workspaces.diff(repo, "story_diff_cap", base)
    assert len(diff_capped) > 60_000
    assert "[... diff truncated at 60000 characters ...]" in diff_capped
    assert marker not in diff_capped

    # Uncapped call (used by code reviewer) has complete diff including marker
    diff_uncapped = workspaces.diff(repo, "story_diff_cap", base, max_chars=None)
    assert "[... diff truncated" not in diff_uncapped
    assert marker in diff_uncapped
    assert len(diff_uncapped) > 75_000


def test_chunk_diff_boundaries():
    """AC2: chunk_diff respects file boundaries and line breaks."""
    # Under limit: single chunk
    short = "diff --git a/f1.py b/f1.py\n+print('hello')\n"
    assert chunk_diff(short, max_chars=500) == [short]

    # Two distinct files exceeding max_chars
    file1 = "diff --git a/f1.py b/f1.py\n" + "+line\n" * 10
    file2 = "diff --git a/f2.py b/f2.py\n" + "+other\n" * 10
    combined = file1 + file2

    # max_chars fits file1 and file2 individually, but not combined
    chunks = chunk_diff(combined, max_chars=len(file1) + 20)
    assert len(chunks) == 2
    assert chunks[0] == file1
    assert chunks[1] == file2

    # Oversized single file splits cleanly on lines without cutting text
    huge_file = "diff --git a/huge.py b/huge.py\n" + "+line_with_content\n" * 50
    h_chunks = chunk_diff(huge_file, max_chars=100)
    assert len(h_chunks) > 1
    recombined = "".join(h_chunks)
    assert recombined == huge_file


def test_concurrent_diffs_never_see_each_others_scratch_index(tmp_path):
    """Two simultaneous diff() calls (board poll + code reviewer) must not
    corrupt each other: with a shared scratch-index path, one caller's
    unlink landed between the other's `add -A` and its `diff`, so git
    compared against a missing (empty) index and reported every tracked
    file as deleted."""
    import threading

    repo = init_repo(tmp_path)
    wt, base, _ = workspaces.create(repo, "story_scratch_race")
    (wt / "part1.py").write_text("# part 1\n" + "A = 1\n" * 150)
    (wt / "part2.py").write_text("# part 2\n" + "B = 2\n" * 150)
    workspaces.commit(repo, "story_scratch_race", "coder output")

    results: dict[str, str] = {}
    errors: list[Exception] = []

    def run(name: str, cap: int | None) -> None:
        try:
            results[name] = workspaces.diff(repo, "story_scratch_race", base, max_chars=cap)
        except Exception as e:  # noqa: BLE001 — test collects failures
            errors.append(e)

    threads = [
        threading.Thread(target=run, args=(f"t{i}", cap))
        for i, cap in enumerate([None, 60_000, None, 60_000, None, 60_000])
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    for name, d in results.items():
        assert "part1.py" in d and "part2.py" in d, name
        assert "README.md" not in d, (name, d[:200])
        # a clean worktree diff must never read like a mass deletion
        assert not any(
            line.startswith("-# demo repo") for line in d.splitlines()
        ), name


def test_code_reviewer_prompt_caps_the_diff_and_lists_the_files_it_cut(
    tmp_path, monkeypatch, legacy_review
):
    """AC2 & AC3: a diff over the cap is shown capped in the ONE review prompt,
    the files the cap cut are listed for the reviewer to read from the
    worktree, and the verdict can reference content from such a file."""
    repo = init_repo(tmp_path)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "diff_cap.db"))

    # cap the prompt's diff at 2,000 chars so part2.py falls outside it
    monkeypatch.setattr("app.runner.DIFF_CHUNK_SIZE", 2000)

    marker_final = "MARKER_VERIFIED_IN_FINAL_CHUNK_77831"

    prompts_seen = []

    class ChunkReviewGateway:
        def __init__(self):
            self.calls = 0

        async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
            from app.providers import Delta as D
            from app.providers import ToolCall as TC
            from app.providers import Usage as U

            self.calls += 1
            user_msg = next((m["content"] for m in messages if m["role"] == "user"), "")
            prompts_seen.append(user_msg)

            if self.calls == 1:  # spec
                yield D(text=MINIMAL_SPEC)
            elif self.calls == 2:  # spec ai-review
                yield D(text="Spec looks good.\n\nVERDICT: APPROVED")
            elif self.calls == 3:  # coder writes files
                # Write file 1 (chunk 1: ~1,050 chars)
                yield TC(
                    id="w1",
                    name="write_file",
                    arguments=json.dumps({
                        "path": "part1.py",
                        "content": "# part 1\n" + "A = 1\n" * 150,
                    }),
                )
            elif self.calls == 4:  # coder writes file 2 (final chunk with marker: ~1,050 chars)
                yield TC(
                    id="w2",
                    name="write_file",
                    arguments=json.dumps({
                        "path": "part2.py",
                        "content": f"# part 2\nKEY = '{marker_final}'\n" + "B = 2\n" * 150,
                    }),
                )
            elif self.calls == 5:  # coder finishes
                yield D(text="Implemented part1 and part2.")
            elif self.calls == 6:  # code review: one prompt, capped diff
                assert user_msg.startswith("=== CODE REVIEW REQUEST")
                assert "part1.py" in user_msg and marker_final not in user_msg
                assert "## Changed files not shown above" in user_msg
                assert "- part2.py" in user_msg
                yield D(
                    text=(
                        f"Read part2.py from the worktree: found expected marker '{marker_final}'. "
                        "All checks pass.\n\nVERDICT: APPROVED"
                    )
                )
            yield U(prompt_tokens=10, completion_tokens=10)

    gateway = ChunkReviewGateway()
    with TestClient(create_app(gateway=gateway)) as client:
        client.patch(
            "/api/v1/settings",
            json={
                "story_poll_interval": 0.05,
                "project_root": repo,
                "write_tools_enabled": True,
                "max_tool_calls": 8,
            },
        )
        story = client.post("/api/v1/stories", json={"title": "multi-chunk story"}).json()
        client.post(f"/api/v1/stories/{story['id']}/queue")

        # Wait for spec review -> human_review
        deadline = time.time() + 30
        while time.time() < deadline:
            cur = client.get(f"/api/v1/stories/{story['id']}").json()
            if cur["status"] in ("human_review", "failed"):
                break
            time.sleep(0.05)
        assert cur["status"] == "human_review", cur.get("error")

        # Approve spec -> executing -> coder -> chunked code review -> human_review
        client.post(f"/api/v1/stories/{story['id']}/approve", json={"by": "tester"})

        deadline = time.time() + 30
        while time.time() < deadline:
            cur = client.get(f"/api/v1/stories/{story['id']}").json()
            if cur["status"] in ("human_review", "failed"):
                break
            time.sleep(0.05)
        assert cur["status"] == "human_review", cur.get("error")

        # Verify the AI review result
        detail = client.get(f"/api/v1/stories/{story['id']}").json()
        assert detail["ai_verdict"] == "approved"
        assert detail["ai_review"] is not None

        # AC3 check: the verdict references content from the file the cap cut
        assert marker_final in detail["ai_review"]
