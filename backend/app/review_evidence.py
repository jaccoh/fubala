"""Evidence for the AI code review: changed tests, mutations, the ledger.

Pure functions apart from reading worktree files. The runner runs the tests and
the mutations (phase B) and records what happened here, from its own ledger:
the reviewer's words never count as evidence. `decide` turns the reviewer's
verdict plus the ledger into approved / rejected / incomplete.
"""

from __future__ import annotations

import fnmatch
import json
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .guardrails import path_denied

MAX_MUTATIONS = 2
OUTPUT_TAIL = 1500

# mutation outcomes
SURVIVED = "survived"
KILLED = "killed"
TIMEOUT = "timeout"

_TEST_DIRS = {"tests"}
_TEST_GLOBS = ("test_*.py", "*_test.py", "*.test.ts", "*.test.tsx", "*.spec.ts", "*.spec.tsx")


def is_test_file(path: str) -> bool:
    p = PurePosixPath(path.replace("\\", "/"))
    if any(part in _TEST_DIRS for part in p.parts[:-1]):
        return True
    return any(fnmatch.fnmatch(p.name, g) for g in _TEST_GLOBS)


def _diff_path(raw: str) -> str:
    raw = raw.split("\t", 1)[0].strip()
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        raw = raw[1:-1]
    return raw[2:] if raw[:2] in ("a/", "b/") else raw


def changed_files(diff_text: str) -> list[str]:
    """Paths added or modified in a unified diff, in order. Deleted files
    (`+++ /dev/null`) are left out; binary changes carry no `+++` line."""
    paths: list[str] = []
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            target = line[4:]
            if target.strip() == "/dev/null":
                continue
            path = _diff_path(target)
            if path and path not in paths:
                paths.append(path)
    return paths


_HEADER = re.compile(r"^diff --git a/(.*?) b/(.*)$", re.MULTILINE)


def diff_paths(diff_text: str) -> list[str]:
    """Every path with a `diff --git` header (deleted files included)."""
    out: list[str] = []
    for m in _HEADER.finditer(diff_text or ""):
        if m.group(2) not in out:
            out.append(m.group(2))
    return out


def not_shown_paths(diff_text: str, shown_chunks: list[str]) -> list[str]:
    """Changed files the prompt does not (fully) show: every file outside the
    first chunk, plus the file the first chunk was cut inside."""
    if len(shown_chunks) <= 1:
        return []
    shown = diff_paths(shown_chunks[0])
    hidden = [p for p in diff_paths(diff_text) if p not in shown]
    if shown and not shown_chunks[1].startswith("diff --git "):
        hidden.insert(0, shown[-1])  # cut mid-file
    return hidden


def changed_test_files(diff_text: str) -> list[str]:
    return [p for p in changed_files(diff_text) if is_test_file(p)]


def safe_relative(path: str) -> str | None:
    """The path when it is a plain relative path without `..`, else None."""
    if not isinstance(path, str) or not path.strip():
        return None
    path = path.strip()
    norm = path.replace("\\", "/")
    if norm.startswith("/") or re.match(r"^[A-Za-z]:", norm) or norm.startswith("~"):
        return None
    if ".." in PurePosixPath(norm).parts or "\x00" in norm or norm.startswith("-"):
        return None
    return norm


_PY_RUNNERS = re.compile(r"\b(pytest|py\.test|python[\d.]*|unittest|tox|nox)\b")
_JS_RUNNERS = re.compile(r"\b(vitest|jest|mocha|npm|npx|pnpm|yarn|node|bun)\b")


def runnable_tests(command: str, tests: list[str]) -> list[str]:
    """The test files `command` can run: Python files for a Python runner,
    TS/JS files for a JS runner; every file when the runner is unknown or
    mixed. A `*.test.ts` is never handed to pytest."""
    py, js = bool(_PY_RUNNERS.search(command)), bool(_JS_RUNNERS.search(command))
    if py == js:
        return list(tests)
    is_py = lambda t: t.endswith(".py")  # noqa: E731
    return [t for t in tests if is_py(t) == py]


def scope_command(command: str, tests: list[str]) -> str:
    """The test command scoped to `tests` (shlex-quoted argv appended)."""
    return " ".join([command, *(shlex.quote(t) for t in tests)]) if tests else command


# ---- MUTATIONS block --------------------------------------------------------

_MARKER = re.compile(r"^[ \t]*(?:#+[ \t]*)?\**MUTATIONS\**[ \t]*:?[ \t]*$", re.MULTILINE)


def parse_mutations(text: str) -> tuple[bool, list[dict]]:
    """(block found, entries as written). The block is a `MUTATIONS` line
    followed by a JSON array (fenced or bare); the last such block wins. An
    unreadable array counts as found-but-empty."""
    marks = list(_MARKER.finditer(text or ""))
    if not marks:
        return False, []
    rest = text[marks[-1].end():]
    start = rest.find("[")
    if start < 0:
        return True, []
    try:
        data, _ = json.JSONDecoder().raw_decode(rest[start:])
    except ValueError:
        return True, []
    if not isinstance(data, list):
        return True, []
    return True, [e for e in data if isinstance(e, dict)]


def strip_mutations(text: str) -> str:
    """The review text without its trailing MUTATIONS block."""
    marks = list(_MARKER.finditer(text or ""))
    return text[: marks[-1].start()].rstrip() if marks else text


@dataclass
class Mutation:
    path: str
    line: int
    original: str
    replacement: str
    tests: list[str] = field(default_factory=list)


def _clean_tests(raw: object) -> list[str]:
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        rel = safe_relative(item) if isinstance(item, str) else None
        if rel and rel not in out:
            out.append(rel)
    return out


def _split_line(data: bytes, line: int) -> tuple[list[bytes], bytes, bytes] | None:
    lines = data.splitlines(keepends=True)
    if not 1 <= line <= len(lines):
        return None
    raw = lines[line - 1]
    body = raw.rstrip(b"\r\n")
    return lines, body, raw[len(body):]


def validate_mutation(
    entry: dict, worktree: Path, deny_globs: tuple[str, ...] = ()
) -> tuple[Mutation | None, bytes | None, str | None]:
    """(mutation, mutated file bytes, None) or (None, None, refusal reason)."""
    path = safe_relative(entry.get("path"))  # type: ignore[arg-type]
    if path is None:
        return None, None, "path is absolute, contains '..' or is empty"
    if path_denied(path, deny_globs):
        return None, None, "path matches a deny glob"
    if is_test_file(path):
        return None, None, "test files cannot be mutated"
    line = entry.get("line")
    if isinstance(line, bool) or not isinstance(line, int):
        return None, None, "line is not an integer"
    original, replacement = entry.get("original"), entry.get("replacement")
    if not isinstance(original, str) or not isinstance(replacement, str):
        return None, None, "original and replacement must be strings"
    if "\n" in replacement or "\r" in replacement:
        return None, None, "replacement spans more than one line"
    if replacement == original:
        return None, None, "replacement equals original"
    root = worktree.resolve()
    target = (root / path).resolve()
    if root not in target.parents or not target.is_file():
        return None, None, "path is not a file inside the worktree"
    try:
        data = target.read_bytes()
        parts = _split_line(data, line)
        if parts is None:
            return None, None, f"line {line} is outside the file"
        lines, body, eol = parts
        if body.decode("utf-8") != original:
            return None, None, f"original does not match line {line} of {path}"
        lines[line - 1] = replacement.encode("utf-8") + eol
    except (OSError, UnicodeError):
        return None, None, "file is not readable text"
    mutation = Mutation(path, line, original, replacement, _clean_tests(entry.get("tests")))
    return mutation, b"".join(lines), None


# ---- ledger -----------------------------------------------------------------


@dataclass
class TestRun:
    __test__ = False  # not a pytest class

    command: str
    rc: int
    files: list[str]
    output: str = ""

    @property
    def completed(self) -> bool:
        return self.rc != 124

    @property
    def passed(self) -> bool:
        return self.rc == 0


@dataclass
class MutationRun:
    mutation: Mutation
    status: str  # survived | killed | timeout
    rc: int
    command: str = ""
    output: str = ""


@dataclass
class Ledger:
    test_run: TestRun | None = None
    mutations: list[MutationRun] = field(default_factory=list)
    refused: list[tuple[dict, str]] = field(default_factory=list)
    mutations_block: bool = False
    no_test_command: bool = False
    changed_tests: list[str] = field(default_factory=list)
    over_budget: bool = False
    pod_unavailable: bool = False

    @property
    def has_mutation_result(self) -> bool:
        return any(m.status in (SURVIVED, KILLED) for m in self.mutations)

    @property
    def any_timeout(self) -> bool:
        return any(m.status == TIMEOUT for m in self.mutations) or bool(
            self.test_run and self.test_run.rc == 124
        )


def mutation_status(rc: int) -> str:
    return SURVIVED if rc == 0 else TIMEOUT if rc == 124 else KILLED


@dataclass
class SessionFlags:
    timed_out: bool = False
    turn_cap: bool = False


def decide(verdict: str | None, ledger: Ledger, flags: SessionFlags) -> tuple[str, str | None]:
    """(approved | rejected | incomplete, cause). A rejection stands on the
    reviewer's word (it cannot wave a bad diff through), so missing evidence
    only downgrades an approval; a session that never finished has no usable
    verdict either way."""
    if flags.timed_out:
        return "incomplete", "the review session timed out"
    if flags.turn_cap:
        return "incomplete", "the review session hit its turn / tool-call cap"
    if verdict == "rejected":
        return "rejected", None
    if ledger.pod_unavailable:
        return "incomplete", "the worker pod was unavailable twice; no evidence could be run"
    if ledger.no_test_command:
        return "incomplete", "the project has no test command"
    if ledger.over_budget:
        return "incomplete", "the evidence phase ran over its time budget"
    if ledger.any_timeout:
        return "incomplete", "a test or mutation run timed out"
    if not ledger.mutations_block:
        return "incomplete", "the reviewer gave no MUTATIONS block"
    if not ledger.mutations and not ledger.has_mutation_result:
        return "incomplete", "every mutation entry was refused"
    if verdict != "approved":
        return "incomplete", "the reviewer gave no verdict"
    if ledger.test_run is None or not ledger.test_run.completed:
        return "incomplete", "approved without a completed changed-test run"
    if not ledger.test_run.passed:
        return "incomplete", "approved while the changed tests fail"
    if not ledger.has_mutation_result:
        return "incomplete", "approved without a killed or survived mutation"
    return "approved", None


def _tail(text: str) -> str:
    text = (text or "").strip()
    return text[-OUTPUT_TAIL:]


def render_section(ledger: Ledger, cause: str | None = None) -> str:
    """The `## Review evidence` section, from the ledger only."""
    lines = ["## Review evidence", ""]
    if cause:
        lines += [f"Result: **incomplete** ({cause})", ""]
    run = ledger.test_run
    if ledger.no_test_command:
        lines.append("- Changed tests: not run (the project has no test command)")
    elif run is None:
        lines.append(
            "- Changed tests: not run"
            + (" (no changed test files)" if not ledger.changed_tests else "")
        )
    else:
        state = (
            "TIMED OUT" if run.rc == 124 else "PASSED" if run.passed else f"FAILED (rc {run.rc})"
        )
        lines.append(f"- Changed tests: {state} - `{run.command}`")
        if run.output.strip():
            lines += ["", "```", _tail(run.output), "```", ""]
    if ledger.over_budget:
        lines.append("- Evidence phase ran over its time budget; remaining runs were skipped")
    for i, m in enumerate(ledger.mutations, 1):
        mu = m.mutation
        lines.append(
            f"- Mutation {i}: **{m.status}** (rc {m.rc}) `{mu.path}:{mu.line}` "
            f"`{mu.original.strip()}` -> `{mu.replacement.strip()}`"
        )
        if m.command:
            lines.append(f"  - tests: `{m.command}`")
    for entry, reason in ledger.refused:
        lines.append(
            f"- Mutation refused: `{str(entry.get('path'))[:120]}` line "
            f"{str(entry.get('line'))[:10]}: {reason}"
        )
    if not ledger.mutations_block:
        lines.append("- No MUTATIONS block from the reviewer")
    return "\n".join(lines).rstrip() + "\n"
