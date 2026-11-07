"""code-review-graph (2.3.9) for story workspaces: graph data outside the tree.

Finding (read from the installed package, `incremental.get_data_dir`): EVERY
command that opens the graph resolves its data dir the same way, in this
order: (1) a registry entry for the repo, written by `--data-dir` into the
global registry under `$CRG_HOME`; (2) the `CRG_DATA_DIR` environment
variable, used verbatim; (3) `<repo>/.code-review-graph`. That covers `mcp`,
`impact` and `detect-changes` too, although only `build` and `update` have a
`--data-dir` flag. So the MCP server CAN read an external data dir: through
`CRG_DATA_DIR` in the server's `env`.

This module uses `CRG_DATA_DIR` for every command instead of `--data-dir`:
`--data-dir` mutates a global registry entry that outranks the env var, and
the entry would outlive the story. A private `CRG_HOME` (inside the graph dir)
keeps even the empty registry out of the user's home. Graph data therefore
never enters the worktree: no in-tree dir, no git exclude.

Embeddings are never enabled: `argv()` is the only argv builder, allows five
subcommands (no `embed`) and refuses any `--embedding*` flag. Cloud embedding
providers would send text derived from the source out of the cluster.

Installation: the package is NOT a backend dependency. code-review-graph 2.3.9
needs fastmcp 3.x, which needs `mcp<2`, while the backend runs on mcp 2.x (adding
it to pyproject downgrades mcp and breaks `app/main.py`). It is installed in its
own env instead (`uv tool install`, runtime and worker Dockerfiles); the single
pin lives in `[tool.fubala]` in pyproject.toml and `PIN` below, and
tests/test_worker_image_parity.py keeps all of them equal. Without the binary on
PATH, `_cli()` falls back to `uvx --from code-review-graph==PIN`.

No shared base graph: a graph records absolute file paths and `update` refuses
a graph built under another root, and every story has its own worktree root. So
`build()` updates the story's own graph if it exists (a retry in the same
worktree) and otherwise does a full build.

Every function times out and fails open: on failure it logs and returns None
and never fails a run.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SERVER_NAME = "code-review-graph"
# must equal [tool.fubala] code-review-graph in pyproject.toml and the pins in
# Dockerfile and docker/worker.Dockerfile (test_worker_image_parity.py)
PIN = "2.3.9"
MCP_TOOL_PREFIX = "mcp__code-review-graph__"
ALLOWED_TOOLS = "mcp__code-review-graph"

BUILD_TIMEOUT = 300.0
REPORT_TIMEOUT = 120.0
POD_BUILD_TIMEOUT = 300
POD_DATA_DIR = "/tmp/code-graph"

# `embed` is not here on purpose
SUBCOMMANDS = ("build", "update", "detect-changes", "impact", "mcp")

REPORT_CAP = 12000
UNAVAILABLE = "impact report unavailable"


def graph_dir(story_id: str) -> Path:
    return data_root() / "code-graph" / story_id


def data_root() -> Path:
    from .runner import config_data_dir

    return config_data_dir()


def _cli(local: bool = True) -> list[str]:
    """The tool's command: on PATH (image), else `uvx` at the pinned version.
    `local=False` is the worker pod, where it is always on PATH."""
    if not local:
        return ["code-review-graph"]
    found = shutil.which("code-review-graph")
    if found:
        return [found]
    return ["uvx", "--python", "3.12", "--from", f"code-review-graph=={PIN}", "code-review-graph"]


def argv(subcommand: str, *args: str, local: bool = True) -> list[str]:
    """The ONLY place a code-review-graph argv is built."""
    if subcommand not in SUBCOMMANDS:
        raise ValueError(f"code-review-graph subcommand not allowed: {subcommand}")
    for arg in args:
        if arg.startswith("--embed"):
            raise ValueError(f"embedding flags are never allowed: {arg}")
    return [*_cli(local), subcommand, *args]


def _env(out_dir: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["CRG_DATA_DIR"] = str(out_dir)
    env["CRG_HOME"] = str(out_dir / "_home")
    return env


def _exec(
    cmd: list[str], *, cwd: Path, env: dict[str, str], timeout: float
) -> subprocess.CompletedProcess[str] | None:
    """The only subprocess call. None on timeout, a missing binary or OS error."""
    try:
        return subprocess.run(  # noqa: S603
            cmd, cwd=str(cwd), env=env, capture_output=True, text=True,
            timeout=timeout, stdin=subprocess.DEVNULL,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        log.warning("code-review-graph %s failed: %s", cmd[3:5], e)
        return None


def _run(
    worktree: Path, out_dir: Path, subcommand: str, *args: str, timeout: float
) -> subprocess.CompletedProcess[str] | None:
    cmd = argv(subcommand, "--repo", str(worktree), *args)
    out_dir.mkdir(parents=True, exist_ok=True)
    done = _exec(cmd, cwd=worktree, env=_env(out_dir), timeout=timeout)
    if done is not None and done.returncode != 0:
        log.warning(
            "code-review-graph %s exited %s: %s",
            subcommand, done.returncode, (done.stderr or "")[-300:],
        )
        return None
    return done


def build(worktree: Path, out_dir: Path) -> Path | None:
    """A graph for `worktree` in `out_dir` (outside the tree); out_dir or None."""
    try:
        worktree, out_dir = Path(worktree), Path(out_dir)
        if (out_dir / "graph.db").is_file():
            if _run(worktree, out_dir, "update", timeout=BUILD_TIMEOUT) is not None:
                return out_dir
            # e.g. built under another root: start over
            shutil.rmtree(out_dir, ignore_errors=True)
        if _run(worktree, out_dir, "build", timeout=BUILD_TIMEOUT) is None:
            shutil.rmtree(out_dir, ignore_errors=True)
            return None
        return out_dir
    except Exception:  # noqa: BLE001 - fail open
        log.warning("code graph build failed", exc_info=True)
        return None


def cleanup(*story_ids: str) -> None:
    """Drop the per-story graph dirs."""
    for sid in story_ids:
        shutil.rmtree(graph_dir(sid), ignore_errors=True)


def mcp_config_json(worktree: Path, out_dir: Path | str, *, pod: bool = False) -> str:
    """Inline JSON for `--mcp-config`: the server reads `out_dir` via CRG_DATA_DIR."""
    cmd = argv("mcp", "--repo", str(worktree), local=not pod)
    return json.dumps(
        {
            "mcpServers": {
                SERVER_NAME: {
                    "command": cmd[0],
                    "args": cmd[1:],
                    "env": {
                        "CRG_DATA_DIR": str(out_dir),
                        "CRG_HOME": f"{out_dir}/_home",
                    },
                }
            }
        }
    )


def pod_mcp_config_json(repo_dir: str) -> str:
    return mcp_config_json(Path(repo_dir), POD_DATA_DIR, pod=True)


def pod_argv(cmd: list[str], repo_dir: str) -> list[str]:
    """Wrap a pod exec argv so the graph is built in the pod first (bounded,
    fail open) with its data under /tmp, then the original argv runs."""
    build_cmd = shlex.join(argv("build", "--repo", repo_dir, local=False))
    script = (
        f"CRG_DATA_DIR={POD_DATA_DIR} CRG_HOME={POD_DATA_DIR}/_home "
        f"timeout {POD_BUILD_TIMEOUT} {build_cmd} >/dev/null 2>&1 || true; "
        'exec "$@"'
    )
    return ["sh", "-c", script, "_", *cmd]


def count_graph_tool_calls(events: list[dict[str, Any]]) -> int:
    """mcp__code-review-graph__* tool_use blocks in a stream-json event list."""
    n = 0
    for event in events:
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_use"
                and str(block.get("name", "")).startswith(MCP_TOOL_PREFIX)
            ):
                n += 1
    return n


# ---- impact report ---------------------------------------------------------


def _json(done: subprocess.CompletedProcess[str] | None) -> dict[str, Any] | None:
    if done is None:
        return None
    text = done.stdout or ""
    start = text.find("{")
    try:
        data, _ = json.JSONDecoder().raw_decode(text[start:]) if start >= 0 else (None, 0)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _rel(path: str, worktree: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(worktree.resolve()))
    except (ValueError, OSError):
        return path


def _callers(db: Path, qualified: str) -> list[tuple[str, str, bool]]:
    """(name, file, covered) for each caller of `qualified`. Covered: the
    caller is a test, or a test (node flagged is_test) calls it or is linked
    to it by TESTED_BY."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    try:
        out: list[tuple[str, str, bool]] = []
        rows = con.execute(
            "SELECT n.qualified_name, n.name, n.file_path, n.is_test FROM edges e "
            "JOIN nodes n ON n.qualified_name = e.source_qualified "
            "WHERE e.kind = 'CALLS' AND e.target_qualified = ? "
            "AND n.kind != 'File' ORDER BY n.file_path, n.name",
            (qualified,),
        ).fetchall()
        for qn, name, file_path, is_test in rows:
            covered = bool(is_test) or con.execute(
                "SELECT 1 FROM edges e JOIN nodes t ON "
                "((e.kind = 'CALLS' AND e.target_qualified = ? "
                "AND t.qualified_name = e.source_qualified) "
                "OR (e.kind = 'TESTED_BY' AND e.source_qualified = ? "
                "AND t.qualified_name = e.target_qualified)) "
                "WHERE t.is_test = 1 LIMIT 1",
                (qn, qn),
            ).fetchone() is not None
            out.append((name, file_path, covered))
        return out
    finally:
        con.close()


def render_report(
    changes: dict[str, Any], impact: dict[str, Any] | None, db: Path, worktree: Path
) -> str:
    lines: list[str] = []
    functions = [
        f for f in changes.get("changed_functions") or []
        if isinstance(f, dict) and not f.get("is_test")
    ]
    lines.append("### Changed symbols and their callers")
    if not functions:
        lines.append("(none found in the graph)")
    for fn in functions[:60]:
        rel = _rel(str(fn.get("file_path", "")), worktree)
        lines.append(f"- {fn.get('name')} ({rel}:{fn.get('line_start')})")
        callers = _callers(db, str(fn.get("qualified_name", "")))
        if not callers:
            lines.append("  - no callers in the graph")
        for name, file_path, covered in callers[:30]:
            mark = "covered by a test" if covered else "UNCOVERED"
            lines.append(f"  - caller {name} ({_rel(file_path, worktree)}): {mark}")
    flows = changes.get("affected_flows") or []
    lines.append("")
    lines.append("### Affected flows")
    for flow in flows[:20]:
        if isinstance(flow, dict):
            lines.append(f"- {flow.get('name')} (criticality {flow.get('criticality')})")
    if not flows:
        lines.append("(none)")
    if impact:
        lines.append("")
        lines.append("### Blast radius")
        lines.append(str(impact.get("summary") or "").strip() or "(no summary)")
        files = [str(f) for f in impact.get("impacted_files") or []][:30]
        if files:
            lines.append("Impacted files: " + ", ".join(_rel(f, worktree) for f in files))
    return "\n".join(lines)[:REPORT_CAP]


def impact_report(worktree: Path, base_ref: str, out_dir: Path) -> str | None:
    """Markdown impact report of the diff against `base_ref` (changed symbols,
    callers, flows; callers with no covering test are marked UNCOVERED), or None."""
    try:
        worktree, out_dir = Path(worktree), Path(out_dir)
        if not (out_dir / "graph.db").is_file():
            return None
        # detect-changes is read-only: bring the graph to the story's HEAD first
        if _run(worktree, out_dir, "update", "--base", base_ref, timeout=REPORT_TIMEOUT) is None:
            return None
        changes = _json(
            _run(worktree, out_dir, "detect-changes", "--base", base_ref, timeout=REPORT_TIMEOUT)
        )
        if changes is None:
            return None
        impact = _json(
            _run(worktree, out_dir, "impact", "--base", base_ref, timeout=REPORT_TIMEOUT)
        )
        return render_report(changes, impact, out_dir / "graph.db", worktree)
    except Exception:  # noqa: BLE001 - fail open
        log.warning("code graph impact report failed", exc_info=True)
        return None


def report_section(report: str | None) -> str:
    """The review-prompt section: always fenced (impact output is data)."""
    if not report:
        return f"## Impact report\n{UNAVAILABLE}"
    fence = "```"
    while fence in report:
        fence += "`"
    return f"## Impact report\n{fence}\n{report}\n{fence}"
