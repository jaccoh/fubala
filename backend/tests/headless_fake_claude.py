"""The fake `claude` CLI used by the headless CODE-executor tests.

Copied onto PATH as an executable `claude` by `install_fake_claude()` in
test_headless.py. Behaviour is driven entirely by the mode baked into the
wrapper (never an ambient env var, so parallel tests cannot cross-talk):

  ok          exit 0, a stream-json session (assistant text, tool_use,
              tool_result, then a final `result` event) on stdout, and a
              source file written into cwd (the "work"). Also writes a
              session transcript JSONL under $CLAUDE_CONFIG_DIR/projects/.
  capture     exit 0 immediately, dumping argv to $FAKE_CLAUDE_CAPTURE —
              used to assert on the prompt/flags the runner delivered.
  crash       exit 3 with a stderr tail (after emitting some stream lines).
  noresult    exit 0 with stream lines but NO final result event: a failure.
  ratelimit   exit 1 with a 429/rate-limit stderr line.
  streamlimit exit 1 with NO stderr; the only 429/usage-limit evidence is an
              isApiErrorMessage assistant event on stdout (the 26 Oct
              incident shape). The reset time defaults to ~1h out in the
              provider's own zone (UTC+8); FAKE_CLAUDE_RESET_AT overrides it
              verbatim ("YYYY-MM-DD HH:MM:SS").
  exit1       exit 1, silent: no stream, no stderr — a real failure.
  turns       exit 0 with a result whose num_turns == --max-turns (a failure).
  sleep       block forever; records its pid so tests can assert killpg.
  echokey     exit 0, but the stream text includes the auth token — proving
              the redactor sweeps the CLI's own output.
  denials     exit 0 with a result event carrying permission_denials.
  conflict_resolve / conflict_markers / conflict_crash
              behave like ok, except for a prompt starting with
              "=== MERGE CONFLICT RESOLUTION REQUEST ===": resolve rewrites
              each listed file without markers and `git add`s it, markers
              leaves the file untouched, crash exits 3.
  fix_ok / fix_crash
              behave like ok, except for a prompt starting with
              "=== FIX REQUEST ===" (the verify fix round): fix_ok writes
              fixed.txt into cwd and finishes cleanly, fix_crash writes to
              stderr and exits 3.
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

MODE = os.environ.get("FAKE_CLAUDE_MODE", "ok")

argv = sys.argv[1:]
try:
    max_turns = int(argv[argv.index("--max-turns") + 1])
except (ValueError, IndexError):
    max_turns = 40


def emit(obj) -> None:
    """One stream-json line on stdout, flushed so the live relay sees it."""
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def write_transcript() -> None:
    """The CLI persists its session under CLAUDE_CONFIG_DIR/projects/."""
    cfg = os.environ.get("CLAUDE_CONFIG_DIR", "")
    if not cfg:
        return
    try:
        proj = os.path.join(cfg, "projects", "demo-repo")
        os.makedirs(proj, exist_ok=True)
        token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
        with open(os.path.join(proj, "session-abc.jsonl"), "w") as fh:
            fh.write(json.dumps({"type": "user", "message": {"content": "go"}}) + "\n")
            fh.write(
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": f"secret {token}"}]},
                    }
                )
                + "\n"
            )
    except OSError:
        pass


def result_event(**extra) -> dict:
    payload = {
        "type": "result",
        "subtype": "success",
        "result": "Implemented the spec in feature.py.",
        "num_turns": 3,
        "usage": {
            "input_tokens": 120,
            "output_tokens": 80,
            "cache_creation_input_tokens": 40,
            "cache_read_input_tokens": 200,
        },
    }
    payload.update(extra)
    return payload


if MODE == "sleep":
    marker = os.environ.get("FAKE_CLAUDE_SLEEP_MARKER", "")
    if marker:
        try:
            with open(marker, "w") as fh:
                fh.write(str(os.getpid()))
        except OSError:
            pass
    while True:
        time.sleep(1)

if MODE == "capture":
    # dump the argv the headless runner actually delivered, so tests can
    # assert on the real prompt (the blocker: no test asserted it), and
    # answer with a well-formed result so the stage succeeds
    target = os.environ.get("FAKE_CLAUDE_CAPTURE", "")
    if target:
        try:
            with open(target, "w") as fh:
                fh.write("\n".join(argv))
        except OSError:
            pass
    emit(result_event(result="captured", num_turns=1, usage={}))
    sys.exit(0)

if MODE == "crash":
    emit({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "planning edits..."}]}})
    write_transcript()
    sys.stderr.write("fatal: claude crashed while planning edits\n")
    sys.exit(3)

if MODE == "noresult":
    with open("feature.py", "w") as fh:
        fh.write("VALUE = 'headless'\n")
    emit({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "working..."}]}})
    emit({"type": "user", "message": {"content": [
        {"type": "tool_result", "content": "1 file changed", "is_error": False}]}})
    write_transcript()
    sys.exit(0)

if MODE == "ratelimit":
    sys.stderr.write("API Error: 429 rate_limit_error (too many requests)\n")
    sys.exit(1)

if MODE == "streamlimit":
    # the 26 Oct incident, verbatim except the reset time: exit 1, empty
    # stderr, the 429 living only in the stream as an isApiErrorMessage
    # assistant event. The stated reset is NAIVE provider-local (UTC+8).
    reset_at = os.environ.get("FAKE_CLAUDE_RESET_AT")
    if not reset_at:
        provider_now = datetime.now(timezone(timedelta(hours=8)))
        reset_at = (provider_now + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    emit(
        {
            "type": "assistant",
            "isApiErrorMessage": True,
            "message": {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "API Error: 429 "
                            '{"type":"error","error":{"type":"rate_limit_error",'
                            '"code":"1308","message":"[1308][Usage limit reached '
                            f'for 5 hour. Your limit will reset at {reset_at}]"}}'
                        ),
                    }
                ]
            },
        }
    )
    sys.exit(1)

if MODE == "exit1":
    sys.exit(1)

if MODE == "echokey":
    token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
    emit({"type": "assistant", "message": {"content": [
        {"type": "text", "text": f"auth was {token}"}]}})
    emit(result_event(result=f"done; auth was {token}", num_turns=2,
                      usage={"input_tokens": 11, "output_tokens": 7}))
    sys.exit(0)

if MODE == "denials":
    emit({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": "rm -rf /"}}]}})
    emit(result_event(
        permission_denials=[
            {"tool": "Bash", "input_summary": "rm -rf /"},
        ],
    ))
    write_transcript()
    sys.exit(0)

if MODE == "turns":
    emit(result_event(result="stopped early", num_turns=max_turns))
    sys.exit(0)

CONFLICT_MARK = "=== MERGE CONFLICT RESOLUTION REQUEST ==="
conflict_prompt = next((a for a in argv if a.startswith(CONFLICT_MARK)), None)
if conflict_prompt is not None and MODE.startswith("conflict_"):
    if MODE == "conflict_crash":
        sys.stderr.write("fatal: claude crashed during the conflict round\n")
        sys.exit(3)
    if MODE == "conflict_resolve":
        import subprocess

        for line in conflict_prompt.splitlines():
            if line.startswith("- "):
                path = line[2:].strip()
                with open(path, "w") as fh:
                    fh.write("VALUE = 'resolved'\n")
                subprocess.run(["git", "add", path], check=False)
    emit({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Handled the conflict round."}]}})
    emit(result_event(result="conflict round done", num_turns=2))
    write_transcript()
    sys.exit(0)

FIX_MARK = "=== FIX REQUEST ==="
fix_prompt = next((a for a in argv if a.startswith(FIX_MARK)), None)
if fix_prompt is not None and MODE.startswith("fix_"):
    if MODE == "fix_crash":
        sys.stderr.write("fatal: claude crashed during the fix round\n")
        sys.exit(3)
    with open("fixed.txt", "w") as fh:
        fh.write("fixed\n")
    emit({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Handled the fix round."}]}})
    emit(result_event(result="fix round done", num_turns=2))
    write_transcript()
    sys.exit(0)

# default: ok
with open("feature.py", "w") as fh:
    fh.write("VALUE = 'headless'\n")
emit({"type": "assistant", "message": {"content": [
    {"type": "text", "text": "Implementing the spec in feature.py."}]}})
emit({"type": "assistant", "message": {"content": [
    {"type": "tool_use", "name": "Write", "input": {"file_path": "feature.py"}}]}})
emit({"type": "user", "message": {"content": [
    {"type": "tool_result", "content": "wrote 20 bytes", "is_error": False}]}})
emit({"type": "assistant", "message": {"content": [
    {"type": "text", "text": "Verify green; done."}]}})
emit(result_event())
write_transcript()
sys.exit(0)
