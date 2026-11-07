"""Read-only agent tools: grounded specs, codebase-aware chat.

Filesystem tools are jailed to the configured project root (an application
setting) — traversal outside it is rejected. Nothing here writes; execution
with write access belongs to phase 2 (worktrees + QA gates).
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
import shlex
import subprocess as sp
from collections.abc import Callable, Iterator
from pathlib import Path

from sqlmodel import Session, select

from . import pods, procs
from .guardrails import path_denied
from .models import Story, StoryKind

# imported lazily inside ToolBox methods: runner.py imports agent_tools at
# module level, so a top-level import here would be a circular import
VERIFY_OUTPUT_TAIL = 4000  # same tail the verify stage reports

log = logging.getLogger(__name__)

_TEST_WORKERS_RE = re.compile(r"(?<!\S)TEST_WORKERS=[^\s;&|]*")
_XDIST_FLAG_RE = re.compile(r"(?<!\S)(-n|--numprocesses)(=|\s+)?([^\s;&|]+)")
_SEGMENT_SPLIT_RE = re.compile(r"(&&|\|\||;|\|)")


def _xdist_flag_sub(match: re.Match[str]) -> str:
    flag, sep = match.group(1), match.group(2) or ""
    if flag == "--numprocesses" and not sep:
        return match.group(0)  # "--numprocessesX" is not a flag
    return f"{flag}{sep}1"


def single_worker_command(command: str) -> tuple[str, bool]:
    """Rewrite a test command to use one worker; returns (command, changed).

    A `TEST_WORKERS=<n>` shell word anywhere becomes `TEST_WORKERS=1`, and
    in segments (split on && || ; |) that contain a `pytest` token the xdist
    flags -n / --numprocesses get the value 1 (not `-p no:xdist`, which makes
    an existing -n flag an unrecognised argument).
    """
    rewritten = _TEST_WORKERS_RE.sub("TEST_WORKERS=1", command)
    parts = _SEGMENT_SPLIT_RE.split(rewritten)
    for i in range(0, len(parts), 2):
        if any(t.rsplit("/", 1)[-1] == "pytest" for t in parts[i].split()):
            parts[i] = _XDIST_FLAG_RE.sub(_xdist_flag_sub, parts[i])
    rewritten = "".join(parts)
    return rewritten, rewritten != command


def _verify_runner():
    """Resolve runner.py lazily (circular-import safe)."""
    from . import runner

    return runner


MAX_LISTED = 300
MAX_READ_BYTES = 120_000
MAX_READ_LINES = 800
MAX_SEARCH_RESULTS = 40

SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", ".next", "target", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", "data", ".fubala-worktrees",
}


def _ok(result: object) -> str:
    """JSON tool result as the agent-facing string (strings pass through)."""
    return result if isinstance(result, str) else json.dumps(result, indent=2)


def _err(message: str) -> str:
    return json.dumps({"error": message})


class ToolBox:
    """Executes one round of model-requested tools. `engine` powers the story
    tools; `project_root` gates the filesystem tools (None disables them)."""

    def __init__(
        self,
        engine=None,
        project_root: str | None = None,
        deny_globs: tuple[str, ...] = (),
        workspace: str | None = None,
        allow_commands: tuple[str, ...] | None = None,
        skip_permissions: bool = False,
        extra_roots: list[str] | None = None,
        board_writable: bool = False,
        writable: bool = False,
        verify_command: str | None = None,
        verify_timeout: float = 600.0,
        verify_project_root: str | None = None,
        test_command: str | None = None,
        story_id: str | None = None,
        watchdog_floor: Callable[[float | None], None] | None = None,
        executor: procs.Executor | None = None,
        prep_parent_id: str | None = None,
    ) -> None:
        self.engine = engine
        # prep spec run: create_story is exposed, limited to children of this
        # prep story; created ids are recorded for the caller
        self.prep_parent_id = prep_parent_id or None
        self.created_ids: list[str] = []
        self.root = Path(project_root).resolve() if project_root else None
        self.deny_globs = deny_globs
        # write/execute jail (defaults to the read root when omitted)
        self.workspace = Path(workspace).resolve() if workspace else self.root
        self.allow_commands = allow_commands if allow_commands is not None else ()
        self.skip_permissions = skip_permissions
        # board write access (create_story): on for chat/spec agents, off for
        # the coder (mid-execution board mutations are a foot-gun)
        self.board_writable = board_writable
        # write_file/run_command are the CODER's tools, jailed to the
        # workspace. Read-only agents (chat, spec writer) never get them —
        # their workspace would be the live checkout, and a spec-stage
        # write leaking untracked files into the real repo is exactly the
        # class of bug fubala-prompts/003 reported.
        self.writable = writable
        if writable:
            self.workspace = self.workspace or self.root
        else:
            self.workspace = None
        # run_verify: the operator's trusted verify command, resolved once by
        # the runner (same source as the verify stage — they cannot drift).
        # None = unconfigured = the tool is not exposed at all. No parameters:
        # the model can trigger the command but never choose it.
        self.verify_command = (verify_command or "").strip() or None
        self.verify_timeout = verify_timeout
        # verify runs with the MAIN checkout as project_root (shared caches,
        # warm-verify stamps — identical to a verify round)
        self.verify_project_root = verify_project_root
        # run_tests: the project's declared TEST command, resolved once by the
        # runner. Same trust model as verify_command (the model may trigger it
        # but never choose it), with one narrow extension: optional `paths`
        # are appended as argv to scope the run to specific test files.
        # None = unconfigured = the tool is not exposed at all.
        self.test_command = (test_command or "").strip() or None
        # process-group attribution: when set, every subprocess this toolbox
        # spawns is marked and registered under this story id, so cancel/stop
        # and the /proc sweep can kill the whole tree (see app.procs)
        self.story_id = story_id or None
        # watchdog floor hook (runner-supplied): a tool-invoked verify is as
        # bus-silent as a verify round, so the runner floors its watchdog
        # with verify_timeout for the duration. None (the default) keeps the
        # many direct-construction call sites untouched.
        self.watchdog_floor = watchdog_floor
        # worker pods: the run's verify executor, used by run_verify and
        # run_tests ONLY (run_command and the file tools stay local). None =
        # in-process.
        self.executor = executor
        # soft guardrail: the project root plus operator-granted extra roots;
        # escaping needs request_permission (unless skip mode)
        self.allowed_roots: list[Path] = [self.root] if self.root else []
        for extra in extra_roots or []:
            p = Path(extra).resolve()
            if p not in self.allowed_roots:
                self.allowed_roots.append(p)

    # -- schema --------------------------------------------------------

    def schemas(self) -> list[dict]:
        tools = []
        if self.root:
            tools += [
                {
                    "type": "function",
                    "function": {
                        "name": "list_files",
                        "description": (
                            "List files under the project (relative paths). Use this first to "
                            "ground yourself in the codebase layout."
                        ),
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "path": {
                                    "type": "string",
                                    "description": "Subdirectory to list, e.g. 'src/api'; empty = root",  # noqa: E501
                                }
                            },
                            "required": [],
                        },
                    },
                },
                {
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "description": "Read a text file from the project. Returns numbered lines.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string", "description": "Relative file path"},
                                "offset": {"type": "integer", "description": "1-based start line"},
                                "limit": {
    "type": "integer", "description": "Max lines (default 200)"
},
                            },
                            "required": ["path"],
                        },
                    },
                },
                {
                    "type": "function",
                    "function": {
                        "name": "search_files",
                        "description": (
                            "Search file contents (case-insensitive substring) across the "
                            "project. Returns path:line matches."
                        ),
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "query": {"type": "string"},
                                "glob": {
                                    "type": "string",
                                    "description": "Filename filter, e.g. '*.py' (default all)",
                                },
                            },
                            "required": ["query"],
                        },
                    },
                },
            ]
        if self.writable and self.workspace:
            tools += [
                {
                    "type": "function",
                    "function": {
                        "name": "edit_file",
                        "description": (
                            "Edit an existing file with an exact text replacement — "
                            "preferred for large files (a whole-file write_file may hit "
                            "provider size limits). old_text must match exactly."
                        ),
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string", "description": "Relative file path"},
                                "old_text": {
                                    "type": "string",
                                    "description": (
                                        "Exact existing text to replace "
                                        "(include surrounding lines for uniqueness)"
                                    ),
                                },
                                "new_text": {"type": "string", "description": "Replacement text"},

                                "replace_all": {
                                    "type": "boolean",
                                    "description": (
                                        "Replace every occurrence (default false: exactly one)"
                                    ),
                                },
                            },
                            "required": ["path", "old_text", "new_text"],
                        },
                    },
                },
                {
                    "type": "function",
                    "function": {
                        "name": "write_file",
                        "description": (
                            "Create or overwrite a file in the workspace with the given content."
                        ),
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                                "content": {"type": "string"},
                            },
                            "required": ["path", "content"],
                        },
                    },
                },
                {
                    "type": "function",
                    "function": {
                        "name": "run_command",
                        "description": (
                            "Run a shell command in the workspace (build, test, inspect). "
                            "Subject to the command allowlist unless permissions are skipped."
                        ),
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "command": {"type": "string"},
                                "timeout": {
                                    "type": "integer",
                                    "description": "Seconds (default 60, max 180)",
                                },
                            },
                            "required": ["command"],
                        },
                    },
                },
            ]
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": "request_permission",
                    "description": (
                        "Ask the operator for access to a path outside the project roots "
                        "(or approval for a blocked command). The operator decides live; "
                        "you receive the answer. Explain WHY you need it."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "Absolute path you need access to",
                            },
                            "reason": {"type": "string"},
                        },
                        "required": ["path", "reason"],
                    },
                },
            }
        )
        if self.verify_command:
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": "run_verify",
                        "description": (
                            "Run the project's configured verification command (the same "
                            "one the pipeline's verify stage runs: same cwd, env and "
                            "timeout) in the workspace and report the exit code + output "
                            "tail. Takes NO arguments — you cannot choose the command."
                        ),
                        "parameters": {
                            "type": "object",
                            "properties": {},
                            "required": [],
                        },
                    },
                }
            )
        if self.test_command:
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": "run_tests",
                        "description": (
                            "Run the project's declared test command (e.g. `uv run "
                            "--python 3.15 -- pytest -q`) in the workspace — no "
                            "permission request, real stdout/stderr and exit code, "
                            "same cwd/env/timeout as a verify round. Omit `paths` to "
                            "run the full suite exactly as declared; pass `paths` to "
                            "scope the run to specific test files (workspace-relative)."
                        ),
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "paths": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": (
                                        "Optional test files/dirs, relative to the "
                                        "workspace (e.g. ['tests/test_x.py']). Empty or "
                                        "omitted = the full declared suite."
                                    ),
                                }
                            },
                            "required": [],
                        },
                    },
                }
            )
        tools += [
            {
                "type": "function",
                "function": {
                    "name": "list_stories",
                        "description": "List stories on the board with their status.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "status": {
                                    "type": "string",
                                    "enum": [
                                        "backlog", "queued", "in_progress",
                                        "ai_review", "human_review", "done", "failed",
                                    ],
                                }
                            },
                            "required": [],
                        },
                    },
                },
            {
                "type": "function",
                "function": {
                    "name": "get_story",
                        "description": "Read one story: description, acceptance criteria, spec.",
                        "parameters": {
                            "type": "object",
                            "properties": {"story_id": {"type": "string"}},
                            "required": ["story_id"],
                        },
                    },
                },
        ]
        if self.engine and (self.board_writable or self.prep_parent_id):
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": "create_story",
                        "description": (
                            "Create a new story on the board (lands in backlog; a human "
                            "queues it). Use for concrete, valuable work worth tracking."
                        ),
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "title": {
                                    "type": "string",
                                    "description": "Short imperative summary",
                                },
                                "description": {
                                    "type": "string",
                                    "description": "What and why; context for the spec writer",
                                },
                                "acceptance": {
                                    "type": "string",
                                    "description": "Optional acceptance criteria",
                                },
                                "skill": {
                                    "type": "string",
                                    "enum": ["low", "medium", "high"],
                                    "description": "Model skill tier (default medium)",
                                },
                                "kind": {
                                    "type": "string",
                                    "enum": ["code", "info", "prep"],
                                    "description": (
                                        "code (default) runs the worktree pipeline; "
                                        "info's deliverable is the document itself; "
                                        "prep's deliverable is child stories"
                                    ),
                                },
                                "depends_on": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "Story ids this story waits on",
                                },
                                "epic_id": {
                                    "type": "string",
                                    "description": "Epic to attach the story to",
                                },
                            },
                            "required": ["title", "description"],
                        },
                    },
                }
            )
        return tools

    # -- execution -------------------------------------------------------

    def execute(self, name: str, arguments_json: str) -> str:
        try:
            args = json.loads(arguments_json) if arguments_json.strip() else {}
        except ValueError:
            return _err(f"arguments are not valid JSON: {arguments_json[:100]}")
        handler = {
            "list_files": self._list_files,
            "read_file": self._read_file,
            "search_files": self._search_files,
            "write_file": self._write_file,
            "edit_file": self._edit_file,
            "run_command": self._run_command,
            "request_permission": self._request_permission_stub,
            "list_stories": self._list_stories,
            "get_story": self._get_story,
            "create_story": self._create_story,
        }
        if self.verify_command:
            handler["run_verify"] = self._run_verify
        if self.test_command:
            handler["run_tests"] = self._run_tests
        handler = handler.get(name)
        if handler is None:
            return _err(f"unknown tool: {name}")
        try:
            return _ok(handler(args))
        except Exception as e:  # tool errors are data for the model, not crashes
            return _err(f"{type(e).__name__}: {e}")

    def _resolve(self, rel: str) -> Path:
        assert self.root is not None
        target = (self.root / rel).resolve()
        if not self._in_allowed_roots(target):
            raise PermissionError(
                f"path escapes the project roots: {rel} — use the "
                "request_permission tool to ask the operator for access"
            )
        return target

    def _in_allowed_roots(self, target: Path) -> bool:
        for root in self.allowed_roots:
            if target == root or root in target.parents:
                return True
            # also allow absolute paths that stay under an allowed root
            if target.is_absolute() and str(target).startswith(str(root) + "/"):
                return True
        return False

    def absolute_path_status(self, path: str) -> str | None:
        """'inside' | 'outside' for an absolute path (permission checks)."""
        target = Path(path).resolve()
        return "inside" if self._in_allowed_roots(target) else "outside"

    def _walk(self, base: Path) -> Iterator[Path]:
        """Yield allowed files under base, sorted, raising past MAX_LISTED."""
        count = 0
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(
                d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")
            )
            for fname in sorted(filenames):
                if fname.startswith(".") or path_denied(fname, self.deny_globs):
                    continue
                count += 1
                if count > MAX_LISTED:
                    raise TruncationWarning(f"listing truncated at {MAX_LISTED} entries")
                yield Path(dirpath) / fname

    def _list_files(self, args: dict[str, object]) -> str:
        base = self._resolve(args.get("path") or "")
        if not base.exists():
            return _err(f"no such directory: {args.get('path')}")
        lines = []
        try:
            for path in self._walk(base):
                lines.append(str(path.relative_to(self.root)))
        except TruncationWarning as e:
            lines.append(f"… ({e})")
        return "\n".join(lines) or "(empty)"

    def _read_file(self, args: dict[str, object]) -> str:
        rel = args.get("path", "")
        if path_denied(rel, self.deny_globs):
            return _err(f"access denied: {rel} matches the sensitive-file policy")
        path = self._resolve(rel)
        if not path.is_file():
            return _err(f"no such file: {rel}")
        if path.stat().st_size > MAX_READ_BYTES * 4:
            return _err(f"file too large to read: {args.get('path')}")
        text = path.read_text(errors="replace")
        lines = text.splitlines()
        offset = max(1, int(args.get("offset") or 1))
        limit = min(int(args.get("limit") or 200), MAX_READ_LINES)
        window = lines[offset - 1 : offset - 1 + limit]
        body = "\n".join(f"{i + offset}: {line}" for i, line in enumerate(window))
        if len(lines) > offset - 1 + limit:
            body += f"\n… ({len(lines)} lines total)"
        return body

    def _search_files(self, args: dict[str, object]) -> str:
        query = (args.get("query") or "").lower()
        pattern = args.get("glob") or "*"
        if not query:
            return _err("query is required")
        matches = []
        for path in self._walk(self.root):
            if not fnmatch.fnmatch(path.name, pattern):
                continue
            if path_denied(path.name, self.deny_globs):
                continue
            try:
                if path.stat().st_size > MAX_READ_BYTES:
                    continue
                for lineno, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
                    if query in line.lower():
                        trimmed = line.strip()[:160]
                        rel = path.relative_to(self.root)
                        matches.append(f"{rel}:{lineno}: {trimmed}")
                        if len(matches) >= MAX_SEARCH_RESULTS:
                            return "\n".join(matches) + f"\n… (capped at {MAX_SEARCH_RESULTS})"
            except OSError:
                continue
        return "\n".join(matches) or "(no matches)"

    def _request_permission_stub(self, args: dict[str, object]) -> str:
        # replaced by GuardedExecutor's live handler; direct calls (no
        # executor) just report what would be needed
        return json.dumps(
            {
                "error": "permission requests need the guarded executor",
                "requested": args.get("path"),
            }
        )

    def _write_file(self, args: dict[str, object]) -> str:

        if self.workspace is None:
            return _err("no workspace configured")
        rel = args.get("path", "")
        content = args.get("content", "")
        if path_denied(rel, self.deny_globs):
            return _err(f"write denied: {rel} matches the sensitive-file policy")
        target = (self.workspace / rel).resolve()
        if self.workspace not in target.parents and target != self.workspace:
            return _err(f"write denied: {rel} escapes the workspace")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return f"wrote {len(content)} chars to {rel}"

    def _edit_file(self, args: dict[str, object]) -> str:
        if not self.writable or self.workspace is None:
            return _err("this agent cannot edit files")
        rel = args.get("path", "")
        old = args.get("old_text") or ""
        new = args.get("new_text") or ""
        if not rel or not old:
            return _err("path and old_text are required")
        if len(old) > 32_000:
            return _err("old_text too large — use a smaller, unique fragment")
        if path_denied(rel, self.deny_globs):
            return _err(f"edit denied: {rel} matches the sensitive-file policy")
        target = (self.workspace / rel).resolve()
        if self.workspace not in target.parents and target != self.workspace:
            return _err(f"edit denied: {rel} escapes the workspace")
        if not target.is_file():
            return _err(f"no such file: {rel}")
        text = target.read_text(errors="replace")
        count = text.count(old)
        if count == 0:
            return _err(
                f"old_text not found in {rel} — copy it EXACTLY (read_file first)"
            )
        if count > 1 and not args.get("replace_all"):
            return _err(
                f"old_text occurs {count}x in {rel} — add surrounding lines to make it "
                "unique, or set replace_all"
            )
        replace_all = bool(args.get("replace_all"))
        updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
        target.write_text(updated)
        n = count if replace_all else 1
        return f"edited {rel}: {n} replacement(s), now {len(updated)} chars"

    def _run_command(self, args: dict[str, object]) -> str:
        if self.workspace is None:
            return _err("no workspace configured")
        command = (args.get("command") or "").strip()
        if not command:
            return _err("command is required")
        try:
            argv = shlex.split(command)
        except ValueError as e:
            return _err(f"unparsable command: {e}")
        base = (argv[0] or "").rsplit("/", 1)[-1]
        # a `-C <path>` (or `--git-dir`) can point git (or others) at a repo
        # OUTSIDE the workspace — the main checkout included. Refuse it.
        for i, arg in enumerate(argv[1:], 1):
            if arg in ("-C", "--git-dir", "--work-tree") and i + 1 < len(argv):
                target = (Path(argv[i + 1]).resolve())
                if not self._in_allowed_roots(target):
                    return _err(
                        f"'{arg} {argv[i + 1]}' points outside the workspace — "
                        "commands must operate inside it"
                    )
        if not self.skip_permissions and base not in self.allow_commands:
            return _err(
                f"'{base}' is not on the allowed commands list "
                f"({', '.join(self.allow_commands) or 'empty'}). Ask the operator to add it "
                "or enable skip_permissions."
            )
        timeout = min(int(args.get("timeout") or 60), 180)
        # shared package caches (warm verify): the coder's `npm ci` /
        # `uv sync` reuse the repo-level cache instead of re-downloading
        # per worktree. Imported lazily — workspaces pulls git state the
        # read-only import graph doesn't need.
        env = None
        if self.root is not None:
            from .workspaces import scrub_child_env, shared_cache_env  # lazy: no import cycle

            # scrub: a leaked GIT_INDEX_FILE/GIT_DIR/GIT_WORK_TREE would aim
            # the child's own git calls at a scratch index or another tree, and
            # a leaked VIRTUAL_ENV/PYTHONHOME/PYTHONPATH (e.g. from the `uv run`
            # wrapper around the suite itself) would redirect interpreter
            # selection — children derive both from cwd and declared command
            env = scrub_child_env({**os.environ, **shared_cache_env(str(self.root))})
        # marked only when attributable to a story: the /proc sweep must never
        # kill a subprocess nobody registered (see app.procs.mark_env)
        try:
            # own session + Popen+register under the module lock (app.procs):
            # a plain sp.run(timeout=) kills only the direct child, orphaning
            # grandchildren — the `sh tools/test.sh` -> `pytest -n 4` incident
            # class — and an unregistered marked group would be swept mid-run.
            # Unregistered (no story id) is still timeout-killable.
            child, pgid = procs.popen(
                argv,
                story_id=self.story_id,
                cwd=self.workspace,
                stdout=sp.PIPE,
                stderr=sp.PIPE,
                text=True,
                env=env,
            )
        except OSError as e:
            return _err(f"{type(e).__name__}: {e}")
        reason = "round_end"
        try:
            out, err = child.communicate(timeout=timeout)
        except sp.TimeoutExpired:
            # SIGTERM the whole group (SIGKILL after the grace) so xdist
            # workers and helper children die with the command
            reason = "timeout"
            procs.kill_group(pgid, wait=True, proc=child)
            return _err(f"command timed out after {timeout}s")
        finally:
            # setsid/nohup escapees are in no group: reap them by run id
            procs.reap_run(
                self.story_id, getattr(child, "fubala_run_id", None), pgid, reason=reason
            )
            if self.story_id:
                procs.unregister(self.story_id, pgid)
        out = (out or "")[:8_000]
        err = (err or "")[:4_000]
        stderr_suffix = f"\nstderr: {err}" if err.strip() else ""
        return f"exit={child.returncode}\n{out}{stderr_suffix}"

    def _train_ci_adjust(self, command: str) -> tuple[str, str]:
        """While a merge train's CI is pending, run tests with one worker.

        Returns (command, notice). Never raises: the hold check must not
        block a model round.
        """
        if self.engine is None:
            return command, ""
        try:
            from . import train

            with Session(self.engine) as db:
                held = train.train_ci_hold(db) is not None
        except Exception:
            log.debug("train CI hold check failed", exc_info=True)
            return command, ""
        if not held:
            return command, ""
        executor = self.executor
        if executor is not None and executor.is_pod:
            from . import pods

            try:
                executor.prepare()
            except Exception:  # noqa: BLE001 - falls back to in-process below
                log.debug("pod prepare for train CI check failed", exc_info=True)
            if executor.node_name and executor.node_name != pods.backend_node():
                return command, "train CI running: pod on dev node, workers not cut\n"
        new, changed = single_worker_command(command)
        if changed:
            return new, "train CI running: tests with 1 worker\n"
        return command, (
            "train CI running: no worker setting in the test command, "
            "worker reduction skipped\n"
        )

    def _executor_kwargs(self) -> dict[str, object]:
        # only passed when set: keeps the in-process call identical to before
        return {"executor": self.executor} if self.executor is not None else {}

    def _run_verify(self, args: dict[str, object]) -> dict[str, object]:
        """Run the operator's trusted verify command in the workspace.

        Deliberately ignores `args`: the model may not choose the command,
        the timeout, or anything else — it can only trigger the same command
        the pipeline's verify stage runs. Everything (cwd = the worktree,
        shared-cache env, warm-verify npm-ci rewrite, 124/125 sentinels)
        comes from runner._run_verify_command, so the coder sees EXACTLY
        what a verify round sees. Called via asyncio.to_thread by the
        guardrails executor, so a long verify never blocks the event loop.
        """
        if not self.verify_command or self.workspace is None:
            return {"error": "run_verify is not configured (no verify command set)"}
        runner = _verify_runner()
        command, notice = self._train_ci_adjust(self.verify_command)
        if self.watchdog_floor is not None:
            self.watchdog_floor(self.verify_timeout)
        try:
            rc, output = runner._run_verify_command(
                self.workspace,
                command,
                self.verify_timeout,
                self.verify_project_root,
                self.story_id,
                **self._executor_kwargs(),
            )
        except pods.FallbackCapFull:
            return {"error": "no worker pod could be had and local runs are disabled; retry later"}
        finally:
            if self.watchdog_floor is not None:
                self.watchdog_floor(None)
        tail = output[-VERIFY_OUTPUT_TAIL:]
        return {
            "passed": rc == 0,
            "command": command,
            "exit_code": rc,
            "output": notice + tail,
        }

    def _validate_test_paths(self, paths: list[str]) -> list[str]:
        """Jail the model-supplied `paths` to the workspace.

        `paths` is the ONLY place the model influences the argv of a trusted
        command, so validation is strict: relative to the workspace, no `..`,
        resolves inside it, and not denied by the path globs. Valid paths are
        returned in a shell-safe form for appending to the command string.
        """
        assert self.workspace is not None
        checked: list[str] = []
        for raw in paths:
            p = str(raw).strip()
            if not p:
                continue
            if "\x00" in p:
                raise ValueError(f"invalid test path: {p!r} (NUL byte)")
            if p.startswith(("/", "~", "\\")) or re.match(r"^[A-Za-z]:[\\/]", p):
                raise ValueError(
                    f"test path must be relative to the workspace, got: {p!r}"
                )
            if ".." in Path(p).parts:
                raise ValueError(f"test path may not contain '..': {p!r}")
            resolved = (self.workspace / p).resolve()
            if resolved != self.workspace and self.workspace not in resolved.parents:
                raise ValueError(
                    f"test path escapes the workspace: {p!r}"
                )
            if path_denied(p, self.deny_globs):
                raise ValueError(f"test path is denied by policy: {p!r}")
            checked.append(shlex.quote(p))
        return checked

    def _run_tests(self, args: dict[str, object]) -> dict[str, object]:
        """Run the project's declared TEST command in the workspace.

        Trusted exactly like run_verify (same runner helper, cwd, shared-cache
        env, timeout semantics, output tail) — the model may trigger the
        command but never choose or rewrite it. The one extension: optional
        `paths` name test files, appended as argv AFTER the declared command
        so `pytest -q tests/test_x.py` scopes the run. Paths are validated
        (relative, jailed to the workspace, not denied) and shlex-quoted, so
        no shell metacharacter the model sends can reach the command.
        """
        if not self.test_command or self.workspace is None:
            return {"error": "run_tests is not configured (no test command set)"}
        raw_paths = args.get("paths") or []
        if not isinstance(raw_paths, list):
            return {"error": "paths must be a list of workspace-relative strings"}
        try:
            safe = self._validate_test_paths([str(p) for p in raw_paths])
        except ValueError as e:
            return {"error": str(e)}
        base, notice = self._train_ci_adjust(self.test_command)
        command = " ".join([base, *safe]) if safe else base
        runner = _verify_runner()
        if self.watchdog_floor is not None:
            self.watchdog_floor(self.verify_timeout)
        try:
            rc, output = runner._run_verify_command(
                self.workspace,
                command,
                self.verify_timeout,
                self.verify_project_root,
                self.story_id,
                **self._executor_kwargs(),
            )
        except pods.FallbackCapFull:
            return {"error": "no worker pod could be had and local runs are disabled; retry later"}
        finally:
            if self.watchdog_floor is not None:
                self.watchdog_floor(None)
        tail = output[-VERIFY_OUTPUT_TAIL:]
        return {
            "passed": rc == 0,
            "command": command,
            "exit_code": rc,
            "output": notice + tail,
        }

    def _list_stories(self, args: dict[str, object]) -> str:
        with Session(self.engine) as db:
            query = select(Story).order_by(Story.updated_at.desc())
            if args.get("status"):
                query = query.where(Story.status == args["status"])
            stories = db.exec(query).all()
        if not stories:
            return "(no stories)"
        return "\n".join(
            f"{s.id}: [{s.status}] {s.title} (model={s.model or f'auto:{s.skill}'}," 
            f" spec v{s.spec_version})" for s in stories[:30]
        )

    def _get_story(self, args: dict[str, object]) -> str:
        story_id = args.get("story_id", "")
        with Session(self.engine) as db:
            story = db.get(Story, story_id)
        if story is None:
            return _err(f"no such story: {story_id}")
        spec = (story.spec or "(no spec yet)")[:4000]
        return (
            f"# {story.title}\nstatus: {story.status}\nmodel: {story.model or story.skill}\n\n"
            f"## Description\n{story.description or '(none)'}\n\n"
            f"## Acceptance\n{story.acceptance or '(none)'}\n\n## Spec\n{spec}"
        )

    def _create_story(self, args: dict[str, object]) -> str:
        if not (self.board_writable or self.prep_parent_id):
            return _err("this agent may not create stories")
        title = (args.get("title") or "").strip()[:200]
        description = (args.get("description") or "").strip()
        if not title or not description:
            return _err("title and description are required")
        skill = args.get("skill") if args.get("skill") in ("low", "medium", "high") else None
        kind = args.get("kind") if args.get("kind") in StoryKind.ALL else StoryKind.CODE
        deps = [
            str(d).strip()
            for d in (args.get("depends_on") or [])
            if str(d).strip()
        ]
        epic_id = (str(args.get("epic_id")).strip() or None) if args.get("epic_id") else None
        from .events import bus
        from .runner import story_payload

        project_id = None
        if self.prep_parent_id:
            if self.prep_parent_id not in deps:
                return _err(
                    "children of a prep story must list its id in depends_on: "
                    f"depends_on=[{self.prep_parent_id!r}]"
                )
        with Session(self.engine) as db:
            if self.prep_parent_id:
                parent = db.get(Story, self.prep_parent_id)
                if parent is None:
                    return _err(f"no such prep story: {self.prep_parent_id}")
                if parent.epic_id:
                    if epic_id is None:
                        epic_id = parent.epic_id
                    elif epic_id != parent.epic_id:
                        return _err(
                            f"epic_id must be the prep story's epic ({parent.epic_id})"
                        )
                project_id = parent.project_id
                for dep in deps:
                    if dep != self.prep_parent_id and db.get(Story, dep) is None:
                        return _err(f"no such story in depends_on: {dep}")
            story = Story(
                title=title,
                description=description,
                acceptance=(args.get("acceptance") or "").strip(),
                skill=skill,
                created_by="agent",
                kind=kind,
                depends_on=json.dumps(deps),
                epic_id=epic_id,
                project_id=project_id,
            )
            db.add(story)
            db.commit()
            db.refresh(story)
            payload = story_payload(story)
        if self.prep_parent_id:
            self.created_ids.append(story.id)
        bus.publish("story", {"story": payload})
        return (
            f"created story {story.id} '{title}' in backlog — a human queues it "
            "when ready"
        )


class TruncationWarning(Exception):
    pass
