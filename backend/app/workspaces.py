"""Git workspaces: one worktree + branch per executing story.

The upstream Auto-Claude lesson, kept: a story never touches the main
checkout — it works in `.fubala-worktrees/<story>` on branch
`fubala/<story>`, and only an explicit human approve merges it back.
"""

from __future__ import annotations

import codecs
import itertools
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

log = logging.getLogger(__name__)

WORKTREE_DIR = ".fubala-worktrees"
CACHE_DIR = ".fubala-cache"

# Build artefacts that must never ride along in a coder commit, however
# they got staged. Each entry is matched against WHOLE path components
# (`*.egg-info` matches any component ENDING in `.egg-info`), so
# `src/dist_helper.py` or `tools/build.py` survive — the ems incident had
# setuptools write `build/lib/...` during a non-editable verify; `git
# check-ignore` (run afterwards, on the main repo) said "ignored" while the
# worktree's own ignore state at commit time let `add -A` stage it.
# `__pycache__` is the same shape: a verify that runs the story's python
# writes bytecode into the worktree (unless PYTHONDONTWRITEBYTECODE is set,
# as in the image but not in a CI pod), and the next round's commit would
# count it as coder work and defeat the no-op gate.
ARTEFACT_PATTERNS: tuple[str, ...] = (
    "build",
    "dist",
    ".venv",
    "*.egg-info",
    "__pycache__",
)
# components matching these patterns are dropped from the index pre-commit
ARTEFACT_SUFFIXES: tuple[str, ...] = tuple(
    p[1:] for p in ARTEFACT_PATTERNS if p.startswith("*")
)
ARTEFACT_NAMES: frozenset[str] = frozenset(
    p for p in ARTEFACT_PATTERNS if not p.startswith("*")
)
# how many artefact root paths share one `git reset` invocation — pathspec
# lists are bounded, artefact trees can hold thousands of files
UNSTAGE_BATCH = 50

# git-control env vars that must NEVER leak into coder/verify children: a
# stray GIT_INDEX_FILE makes the child's `git add` write into a scratch (or
# missing) index and quietly corrupt what the next commit sees
GIT_ENV_KEYS: tuple[str, ...] = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")

# interpreter-activation env vars that must never leak either: the verify
# chain runs under `uv run`, which exports ITS OWN venv (VIRTUAL_ENV) to the
# suite; a child `uv run --python X` then reuses that active environment
# instead of installing the requested interpreter into the shared cache
# (UV_PYTHON_PREFERENCE=only-managed excludes *discovered* system pythons,
# not an *activated* venv) — the cache stays empty and every worktree reuses
# the board's .venv by accident. PYTHONHOME/PYTHONPATH hijack interpreter and
# import resolution the same way. Children must derive their interpreter from
# the declared command and project config, never from the board process's
# activated environment.
VENV_ENV_KEYS: tuple[str, ...] = ("VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH")


def scrub_child_env(env: dict[str, str] | None) -> dict[str, str] | None:
    """Drop host-state variables from a child env (git + interpreter hygeine).

    Two families: git-control vars (GIT_DIR/GIT_WORK_TREE/GIT_INDEX_FILE —
    a leaked scratch index would aim the child's own git calls at a throwaway
    index) and interpreter-activation vars (VIRTUAL_ENV/PYTHONHOME/PYTHONPATH
    — leaked from whoever launched the board, or from the `uv run` wrapper
    around the verify command itself, they silently redirect a child's
    interpreter selection away from the declared command's intent). Children
    get git state from their cwd alone and tool versions from the declared
    command, so stripping both is always safe."""
    if env is None:
        return None
    return {
        k: v
        for k, v in env.items()
        if k not in GIT_ENV_KEYS and k not in VENV_ENV_KEYS
    }

# unique suffixes for per-call scratch index files (see _scratch_index_path)
_SCRATCH_SEQ = itertools.count()


class WorkspaceError(Exception):
    """User-presentable workspace failure (message goes to story.error)."""


# ---- push delivery: keep the root current with the remote default branch ----
#
# The 033 incident: a push project's root checkout sat on a stale commit for
# days while the remote default branch moved on, so every story was specced,
# coded and verified against code nobody else had. sync_root is the fix —
# called from the runner before each spec run, worktree creation and
# take-in-before-verify. It is deliberately toothless: the ONLY mutation it
# may perform is a fast-forward. A root that is ahead, diverged, dirty (in
# tracked files) or unreachable stays exactly as the operator left it, with
# a warning instead of a repair.


@dataclass(frozen=True)
class SyncOutcome:
    """What sync_root did (or refused to do). `kind` is the stable key the
    runner maps to a warning; `ref` is the remote default ref it compared
    against; `old`/`new` carry the SHAs when a fast-forward happened."""

    kind: str  # ff | up_to_date | diverged | dirty | fetch_failed | no_remote
    message: str = ""
    ref: str = ""
    old: str = ""
    new: str = ""


def _remote_default_ref(repo: Path, remote: str) -> str:
    """refs/remotes/<remote>/HEAD, seeded when a fresh clone lacks it.

    `git remote set-head origin --auto` writes the ref from the remote's own
    HEAD advertisement; when even that cannot resolve it (an empty remote,
    an old git) we fall back to `ls-remote --symref`, which names the default
    branch without touching any local ref."""
    ref = f"refs/remotes/{remote}/HEAD"
    try:
        if _git(repo, "symbolic-ref", "--quiet", ref).strip():
            return ref
    except WorkspaceError:
        pass
    try:
        _git(repo, "remote", "set-head", remote, "--auto")
    except WorkspaceError:
        pass
    try:
        if _git(repo, "symbolic-ref", "--quiet", ref).strip():
            return ref
    except WorkspaceError:
        pass
    # last resort: ask the remote directly which branch HEAD points at
    try:
        out = _git(repo, "ls-remote", "--symref", remote, "HEAD")
    except WorkspaceError:
        return ""
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("ref: "):
            target = line[len("ref: ") :].split()[0].strip()
            if target.startswith("refs/heads/"):
                return f"refs/remotes/{remote}/{target[len('refs/heads/'):]}"
    return ""


def sync_root(project_root: str, remote: str = "origin") -> SyncOutcome:
    """Fast-forward the project root to the remote default branch.

    Never raises, never rewinds, never resets: a root that is not strictly
    behind is returned to the caller untouched (with a kind saying why), and
    the story proceeds on local HEAD — a remote problem must never fail a
    story. The comparison is against the remote's DEFAULT branch (what
    `origin/HEAD` points at), not the root's own branch name: a push root
    legitimately sits on `fubala/integration` while `origin/main` advances.
    """
    try:
        repo = _repo_root(project_root)
    except WorkspaceError as e:
        return SyncOutcome("fetch_failed", f"project root unusable: {e}")

    try:
        _git(repo, "remote", "get-url", remote)
    except WorkspaceError:
        return SyncOutcome(
            "no_remote", f"remote '{remote}' is not configured; using local HEAD"
        )

    try:
        _git(repo, "fetch", remote)
    except WorkspaceError as e:
        return SyncOutcome("fetch_failed", f"fetch {remote} failed: {e}")

    ref = _remote_default_ref(repo, remote)
    if not ref:
        return SyncOutcome(
            "no_remote", f"cannot resolve {remote}'s default branch; using local HEAD"
        )
    try:
        remote_head = _git(repo, "rev-parse", "--verify", ref).strip()
        local_head = _git(repo, "rev-parse", "HEAD").strip()
    except WorkspaceError as e:
        return SyncOutcome("fetch_failed", f"cannot resolve {ref}: {e}")

    # Dirty means TRACKED changes only (gate note): untracked mockups and
    # editor droppings never block a fast-forward and must not turn every
    # story into a warning. A tracked edit would be overwritten (or worse,
    # merged into the ff commit), so it is the operator's to land first.
    # Checked BEFORE the SHA comparison: a dirty root is dirty even when the
    # remote has not moved, and the operator should hear about it every time.
    if _git(repo, "status", "--porcelain", "--untracked-files=no").strip():
        return SyncOutcome(
            "dirty",
            "project root is dirty (uncommitted tracked changes); left on local HEAD",
            ref=ref,
            old=local_head,
            new=remote_head,
        )

    if remote_head == local_head:
        return SyncOutcome("up_to_date", ref=ref, old=local_head, new=remote_head)

    try:
        _git(repo, "merge-base", "--is-ancestor", "HEAD", remote_head)
    except WorkspaceError:
        # HEAD is NOT an ancestor of the remote tip: the root has commits the
        # remote lacks (ahead or genuinely diverged). The operator's to push
        # or rebase — we never reset or force anything.
        return SyncOutcome(
            "diverged",
            f"project root is ahead of/diverged from {remote}'s default branch "
            f"({ref}); left on local HEAD",
            ref=ref,
            old=local_head,
            new=remote_head,
        )

    old = local_head
    try:
        _git(repo, "merge", "--ff-only", remote_head)
    except WorkspaceError as e:
        # e.g. dirt that slipped between the check and the merge: git refused,
        # nothing changed — report it, never fail the story over it
        return SyncOutcome(
            "diverged",
            f"fast-forward of project root refused by git: {e}",
            ref=ref,
            old=old,
            new=remote_head,
        )
    new = ""
    try:
        new = _git(repo, "rev-parse", "HEAD").strip()
    except WorkspaceError:
        pass
    return SyncOutcome("ff", ref=ref, old=old, new=new or remote_head)


# One in-process lock per git working area (worktree or main checkout). The
# board polls GET /stories/{id}, whose code diff runs `git add -A` in the
# story worktree on a request thread — while the coder pipeline commits in
# that same worktree on the runner's task. Two index writers collide on
# index.lock and the coder's commit dies ("Unable to create .../index.lock:
# File exists"), failing a story that was executing fine. Git calls sharing
# a working area are therefore serialized; the retry below covers writers
# this process can't see (selfupdate helper, agent shell commands).
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(workdir: Path) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(str(workdir), threading.Lock())


def _is_lock_contention(stderr: str) -> bool:
    lowered = (stderr or "").lower()
    return "index.lock" in lowered or "lock file" in lowered


def _git(
    project_root: Path,
    *args: str,
    cwd: Path | None = None,
    env: dict | None = None,
    ok_codes: tuple[int, ...] = (),
) -> str:
    workdir = Path(cwd or project_root).resolve()
    lock = _lock_for(workdir)
    run_env = {**os.environ, **env} if env else None
    for attempt in range(5):
        with lock:
            try:
                res = subprocess.run(
                    ["git", "-C", str(cwd or project_root), *args],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    env=run_env,
                )
            except (OSError, subprocess.TimeoutExpired) as e:
                raise WorkspaceError(f"git failed: {e}") from e

        if res.returncode == 0 or not _is_lock_contention(res.stderr):
            break
        # someone else holds the lock (another process): back off and retry
        time.sleep(0.2 * (attempt + 1))
    if res.returncode != 0 and res.returncode not in ok_codes:
        raise WorkspaceError(f"git {' '.join(args[:2])}: {res.stderr.strip()[:300]}")
    return res.stdout


def _repo_root(project_root: str) -> Path:
    root = Path(project_root).resolve()
    if not root.exists():
        raise WorkspaceError(f"project root does not exist: {project_root}")
    try:
        top = _git(root, "rev-parse", "--show-toplevel").strip()
    except WorkspaceError:
        raise WorkspaceError(
            "project root is not a git repository — execution needs one so changes stay isolated"
        ) from None
    return Path(top)


def worktree_exists(project_root: str | None, story_id: str) -> bool:
    """True when a story worktree directory is present — without resolving
    a repo (a missing/unset root is simply "no worktree"), so callers can
    ask "should I bother discarding?" before doing any git work."""
    if not project_root:
        return False
    for base in (Path(project_root), Path(project_root).parent):
        if (base / WORKTREE_DIR / story_id).exists():
            return True
    return False


def worktree_path(project_root: str, story_id: str) -> Path:
    """Effective worktree path: inside the project root by design (the
    sandbox's jail), falling back to the legacy parent-of-repo location for
    stories whose worktree already lives there."""
    repo = _repo_root(project_root)
    in_repo = repo / WORKTREE_DIR / story_id
    legacy = repo.parent / WORKTREE_DIR / story_id
    if legacy.exists() and not in_repo.exists():
        return legacy
    return in_repo


def _exclude_path(repo: Path) -> Path:
    """The repo's local exclude file, correct for BOTH placements.

    A linked worktree's `.git` is a FILE (the gitdir pointer), so
    `repo/.git/info/exclude` is "not a directory" there. `git rev-parse
    --git-path info/exclude` resolves the real path for main checkouts and
    worktrees alike (info/ is common to all worktrees of a repo); the plain
    path stays as a fallback when git itself is unusable."""
    try:
        resolved = _git(repo, "rev-parse", "--git-path", "info/exclude").strip()
        if resolved:
            return Path(resolved) if resolved.startswith("/") else repo / resolved
    except WorkspaceError:
        pass
    return repo / ".git" / "info" / "exclude"


def _exclude_dir(repo: Path, entry: str, comment: str) -> None:
    """Keep an untracked directory out of git status via the repo's local
    exclude file — never the tracked .gitignore (it's the user's).

    `entry` is an exclude-pattern line (e.g. `.fubala-cache/`), `comment`
    the human-readable line written above it on first add."""
    exclude = _exclude_path(repo)
    info = exclude.parent
    try:
        info.mkdir(parents=True, exist_ok=True)
        existing = exclude.read_text() if exclude.exists() else ""
        if entry not in existing:
            with exclude.open("a") as fh:
                fh.write(f"\n# {comment}\n{entry}\n")
    except OSError:
        pass  # best-effort; an untracked dir is noise, not a failure


def _exclude_worktrees_dir(repo: Path) -> None:
    """Keep the in-repo .fubala-worktrees/ out of git status."""
    _exclude_dir(repo, f"{WORKTREE_DIR}/", "fubala story worktrees")


def _main_checkout(repo: Path) -> Path:
    """The main checkout that owns `repo`.

    A linked worktree IS its own toplevel, so `--show-toplevel` there names
    the throwaway worktree. The shared cache has to live outside it (in the
    main checkout, which outlives every story), so resolve the common git
    dir and take its parent. Falls back to `repo` when git can't tell us."""
    try:
        common = _git(repo, "rev-parse", "--git-common-dir").strip()
    except WorkspaceError:
        return repo
    if not common or common == ".git":
        return repo  # already the main checkout
    main = Path(common) if common.startswith("/") else repo / common
    return main.resolve().parent


def shared_cache_env(project_root: str) -> dict[str, str]:
    """Env vars pointing npm and uv at caches shared by every story worktree.

    Worktrees are throwaway checkouts, so per-worktree default caches would
    re-download every package on each verify round. Instead both tools get a
    single cache rooted at the main checkout that owns the repo
    (`.fubala-cache/`), which the local git exclude keeps out of `git
    status`. The main checkout — never a linked worktree — is the anchor so
    the cache outlives the worktree that happens to be verifying.

    Best-effort by contract: caching must never fail a run, so any problem
    (not a repo, unwritable disk, git missing) returns an empty env and the
    child simply uses its tool defaults. Legacy parent-of-repo worktrees are
    fine too — the cache always resolves to the same main checkout, so both
    placements share it.
    """
    try:
        repo = _main_checkout(_repo_root(project_root))
        cache = repo / CACHE_DIR
        npm_dir = cache / "npm"
        uv_dir = cache / "uv"
        uv_python_dir = uv_dir / "python"
        npm_dir.mkdir(parents=True, exist_ok=True)
        uv_dir.mkdir(parents=True, exist_ok=True)
        uv_python_dir.mkdir(parents=True, exist_ok=True)
        _exclude_dir(repo, f"{CACHE_DIR}/", "fubala shared package caches")
        # UV_PYTHON_INSTALL_DIR: `uv run --python 3.x` downloads interpreters
        # to $HOME/.local/share/uv/python by default — outside every root the
        # coder is allowed to touch, so the install stalls on a permission
        # request. Pointing it inside the shared cache keeps interpreter
        # downloads inside the repo (and shared across worktrees). Existing
        # interpreters are NOT reused — the first run re-downloads into the
        # cache (acceptable; documented with the run_tests story).
        # UV_PYTHON_PREFERENCE=only-managed ENFORCES that: without it uv
        # happily discovers a system/pyenv/virtualenv interpreter matching
        # the requested version (e.g. VIRTUAL_ENV pointing at the backend's
        # own .venv) and the cache stays empty — the exact 120s stall this
        # env exists to prevent, just moved to cache-miss surprises.
        return {
            "npm_config_cache": str(npm_dir),
            "UV_CACHE_DIR": str(uv_dir),
            "UV_PYTHON_INSTALL_DIR": str(uv_python_dir),
            "UV_PYTHON_PREFERENCE": "only-managed",
        }
    except Exception:  # noqa: BLE001 — never fail a run over caching
        log.debug("shared cache env unavailable for %s", project_root, exc_info=True)
        return {}


def create(project_root: str, story_id: str) -> tuple[Path, str, bool]:
    """Ensure the story's worktree exists; returns (path, base_commit,
    created_new).

    The base is always the original fork point from the main branch — never
    the story branch head — so retry attempts diff against where the story
    started, not against their own previous commits. Pre-fix legacy
    worktrees (parent-of-repo) and stale branches are discarded so every
    story ends up in a trusted in-root worktree based on current main; a
    fresh creation tells the caller to re-pin its base_ref (a stale pin
    would show already-merged main commits as coder output — the 005 bug).
    """
    repo = _repo_root(project_root)
    branch = f"fubala/{story_id}"
    legacy = repo.parent / WORKTREE_DIR / story_id
    wt = repo / WORKTREE_DIR / story_id

    if legacy.exists() and not wt.exists():
        # pre-003 placement: untrusted by the sandbox and possibly stale —
        # drop it (and its branch) and rebuild in-root from current main
        for cleanup in (
            ["worktree", "remove", "--force", str(legacy)],
            ["branch", "-D", branch],
        ):
            try:
                _git(repo, *cleanup)
            except WorkspaceError as e:
                log.debug("legacy worktree cleanup %s failed: %s", cleanup, e)
    if wt.exists():
        base = _git(repo, "merge-base", branch, "HEAD").strip() or _git(
            repo, "rev-parse", branch
        ).strip()
        return wt, base, False

    _exclude_worktrees_dir(repo)
    wt.parent.mkdir(parents=True, exist_ok=True)
    # a branch surviving its worktree (004) would make `worktree add -b`
    # fail — clear it first so creation is always fresh
    try:
        _git(repo, "branch", "-D", branch)
    except WorkspaceError as e:
        log.debug("pre-creation branch cleanup %s failed: %s", branch, e)
    base = _git(repo, "rev-parse", "HEAD").strip()
    _add_worktree(repo, branch, wt)
    return wt, base, True


def _add_worktree(repo: Path, branch: str, wt: Path) -> None:
    """`git worktree add -b` with one best-effort cleanup-and-retry.

    The proactive `branch -D` above covers the common stale-branch case, but
    a branch can also be resurrected mid-flight (an overlapping attempt, a
    reflog race), which git rejects with "already exists". One retry after
    pruning is enough to clear that; anything that still fails (dirty path,
    permissions) is a real error and is surfaced with its stderr.
    """
    try:
        _git(repo, "worktree", "add", "-b", branch, str(wt), "HEAD")
        return
    except WorkspaceError as first_error:
        for cleanup in (["worktree", "prune"], ["branch", "-D", branch]):
            try:
                _git(repo, *cleanup)
            except WorkspaceError:
                pass  # best-effort; the retry below tells us if it mattered
        try:
            _git(repo, "worktree", "add", "-b", branch, str(wt), "HEAD")
            return
        except WorkspaceError as second_error:
            raise WorkspaceError(
                f"worktree add failed after cleanup (branch {branch}): "
                f"{second_error} — first attempt: {first_error}"
            ) from second_error


def head(project_root: str) -> str:
    """Current HEAD commit of the main checkout."""
    return _git(_repo_root(project_root), "rev-parse", "HEAD").strip()


CONFLICT_START = "<<<<<<<"
CONFLICT_END = ">>>>>>>"
_MARKER_EXCERPT_CAP = 2000


def _conflicted_paths(wt: Path) -> list[str]:
    """Paths git still reports as unmerged in `wt`'s index."""
    return [
        line.strip()
        for line in _git(wt, "diff", "--name-only", "--diff-filter=U").splitlines()
        if line.strip()
    ]


def _story_touched_paths(project_root: str, story_id: str) -> list[str]:
    """Paths the STORY changed against its base commit — the only files whose
    conflict markers matter for approval.

    Same set `diff()` reports: committed story work, uncommitted edits and
    untracked files, staged into a throwaway index (the worktree's real
    index is never touched). The base is the merge-base of the project
    root's HEAD and `fubala/<story_id>`, computed in the PROJECT ROOT — in
    the worktree HEAD is the story branch itself, so the merge-base there
    would be the branch tip and every committed change would vanish. A
    missing branch means nothing to merge: diff against "HEAD" (no-op).

    Never raises: a git hiccup here must not block or crash a merge, so the
    caller falls back to "no story-touched paths" (index U-status still
    applies unconditionally)."""
    try:
        wt = worktree_path(project_root, story_id)
        if not wt.exists() or not _registered_worktree(wt):
            return []
        repo = _repo_root(project_root)
        branch = f"fubala/{story_id}"
        try:
            base = (
                _git(repo, "merge-base", "HEAD", branch).strip()
                if _branch_exists(repo, branch)
                else "HEAD"
            )
        except WorkspaceError:
            base = "HEAD"
        scratch = _stage_scratch_index(wt)
        try:
            out = _git(
                wt, "diff", "--name-only", base, env={"GIT_INDEX_FILE": str(scratch)}
            )
        finally:
            _unlink_quietly(scratch)
    except WorkspaceError as e:
        log.debug("story-touched paths for %s failed: %s", story_id, e)
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def _marker_paths(wt: Path, paths: list[str]) -> list[str]:
    """Files still carrying conflict markers among `paths`.

    Only the given pathspecs are scanned: a literal `<<<<<<<` line in a file
    the story never touched is pre-existing project content, not an
    unresolved conflict, and must not block approval. Paths absent from the
    worktree (deleted by the story) are skipped — `git add -A` would
    silently stage a markered file as "resolved", so for the files the story
    DID touch, marker presence matters as much as the index state (`git
    grep` exits 1 when nothing matches — expected, not an error)."""
    existing = [p for p in paths if p and (wt / p).is_file()]
    if not existing:
        return []
    try:
        out = _git(
            wt,
            "grep",
            "--untracked",
            "-lE",
            r"^\s*(<<<<<<<|>>>>>>>)",
            "--",
            *existing,
            ok_codes=(1,),
            env={"GIT_LITERAL_PATHSPECS": "1"},
        )
    except WorkspaceError as e:
        log.debug("marker scan in %s failed: %s", wt, e)
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def _file_markers(wt: Path, path: str) -> list[str]:
    """The conflict-marker lines in one worktree file (empty when absent)."""
    try:
        text = (wt / path).read_text(errors="replace")
    except (OSError, IsADirectoryError):
        return []
    return [
        line
        for line in text.splitlines()
        if line.startswith(CONFLICT_START) or line.startswith(CONFLICT_END)
    ]


def _merge_markers_excerpt(wt: Path, path: str) -> str:
    """A short excerpt of the conflict markers in `path` (best-effort)."""
    markers = _file_markers(wt, path)
    if not markers:
        return ""
    excerpt = "\n".join(markers)
    if len(excerpt) > _MARKER_EXCERPT_CAP:
        excerpt = excerpt[:_MARKER_EXCERPT_CAP] + "\n[... markers truncated ...]"
    return excerpt


def markers_excerpt(project_root: str, story_id: str, path: str) -> str:
    """Public wrapper: the marker excerpt for one conflicted path."""
    wt = worktree_path(project_root, story_id)
    if not wt.exists():
        return ""
    return _merge_markers_excerpt(wt, path)


def take_in_main(project_root: str, story_id: str) -> tuple[bool, list[str]]:
    """Merge the project root's CURRENT HEAD into the story worktree.

    "Current main" is whatever HEAD the project root checkout sits on —
    resolved with `git rev-parse HEAD`, never a branch literally named
    "main" (project roots may live on `fubala/integration`, `integration`,
    ...). Runs INSIDE the worktree so the story branch takes main in, not
    the other way round.

    Returns (conflicted, paths). "Already up to date." is a clean no-op.
    A hard git failure raises WorkspaceError — a story must never continue
    (or merge) on top of an unknown state."""
    repo = _repo_root(project_root)
    wt = worktree_path(project_root, story_id)
    if not wt.exists():
        raise WorkspaceError(f"worktree for {story_id} does not exist")
    main_head = _git(repo, "rev-parse", "HEAD").strip()
    try:
        _git(
            wt,
            "-c",
            "user.name=fubala",
            "-c",
            "user.email=fubala@local",
            "merge",
            "--no-edit",
            main_head,
        )
    except WorkspaceError:
        conflicted = _conflicted_paths(wt)
        if not conflicted:
            # merge refused to start (dirty tree, unborn HEAD, ...) — never
            # guess: abort and surface the failure
            try:
                _git(wt, "merge", "--abort", ok_codes=(1, 128))
            except WorkspaceError:
                pass
            raise
        return True, conflicted
    # merge reported success — but a marker left behind by an aborted
    # earlier attempt still means "unresolved" for the caller
    return False, _conflicted_paths(wt)


def unmerged(
    project_root: str, story_id: str, conflicted_paths: list[str] | None = None
) -> list[str]:
    """Paths still unresolved after a coder resolution round: unmerged in
    the index OR still carrying conflict markers in the worktree files.

    An index-level resolution (`git add`) with markers still in the file,
    and a file edited clean but not yet `git add`-ed, both count as
    unresolved — `workspaces.commit`'s `add -A` would otherwise stage
    markers as a resolution.

    Marker scanning is scoped to the files the STORY touched plus whatever
    `conflicted_paths` the take-in reported: a literal `<<<<<<<` line in an
    untouched main file is pre-existing project content, not a conflict
    this story must resolve. Index U-status applies unconditionally."""
    wt = worktree_path(project_root, story_id)
    if not wt.exists():
        return []
    index_unmerged = _conflicted_paths(wt)
    scan = set(_story_touched_paths(project_root, story_id))
    scan.update(p for p in (conflicted_paths or []) if p)
    marker_paths = _marker_paths(wt, sorted(scan))
    unresolved: list[str] = []
    seen: set[str] = set()
    for p in [*index_unmerged, *marker_paths]:
        if p and p not in seen:
            seen.add(p)
            unresolved.append(p)
    return unresolved


def diff(
    project_root: str,
    story_id: str,
    base_ref: str | None = None,
    max_chars: int | None = 60_000,
) -> str:
    """Unified diff of the worktree against its base commit.

    Includes both committed and uncommitted changes: diff from base_ref to
    the staged worktree. Without base_ref, falls back to workdir vs HEAD.
    Capped at max_chars by default with a clear truncation notice; pass
    max_chars=None for the complete uncapped diff.
    """
    wt = worktree_path(project_root, story_id)
    if not wt.exists() or not _registered_worktree(wt):
        return ""
    scratch = _stage_scratch_index(wt)
    try:
        against = _diff_base(project_root, story_id, base_ref)
        # same throwaway index: the freshly staged (possibly untracked)
        # files are what the diff must compare against the base commit
        raw = _git(wt, "diff", against, env={"GIT_INDEX_FILE": str(scratch)})
        if max_chars is not None and len(raw) > max_chars:
            return raw[:max_chars] + f"\n\n[... diff truncated at {max_chars} characters ...]\n"
        return raw
    finally:
        _unlink_quietly(scratch)


def diff_added_paths(diff_text: str) -> list[str]:
    """Added file paths from a unified diff (pure — no git, no worktree).

    Only genuinely ADDED paths count: `+++ b/<path>` lines whose counterpart
    `---` side is `/dev/null` (renames/modifications are not mass additions).
    Used by the vendored-dependency guard over the base_ref diff."""
    added: list[str] = []
    old_side: str | None = None
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            old_side = None  # new file section
        elif line.startswith("--- "):
            old_side = line[4:].strip()
        elif line.startswith("+++ "):
            if old_side == "/dev/null":
                added.append(_strip_ab_prefix(line[4:].strip()))
            old_side = None
    return added


def _strip_ab_prefix(path: str) -> str:
    return path[2:] if path.startswith(("a/", "b/")) else path


def _diff_base(project_root: str, story_id: str, base_ref: str | None) -> str:
    """What `diff` compares the worktree against.

    With a pinned base_ref and an existing story branch, the base is the
    merge-base of the project root's HEAD and that branch — computed in the
    PROJECT ROOT, never inside the worktree (there HEAD is the story branch
    itself, so the merge-base is its own tip and every diff would come back
    empty: "coder produced no changes"). After take_in_main this is current
    main, so main's own progress never leaks into the story's diff; with no
    merge taken in it equals the fork point, preserving the old behavior.
    Any git hiccup falls back to the raw pin."""
    if not base_ref:
        return "HEAD"
    repo = _repo_root(project_root)
    branch = f"fubala/{story_id}"
    try:
        if not _branch_exists(repo, branch):
            return base_ref
        base = _git(repo, "merge-base", "HEAD", branch).strip()
    except WorkspaceError:
        return base_ref
    return base or base_ref


def _stage_scratch_index(wt: Path) -> Path:
    """Stage everything into a throwaway index so diffs include untracked
    files WITHOUT writing the worktree's shared index.lock — a poll of
    GET /stories/{id} must never race the coder's commit in the same
    worktree, and must never leave the board's read as a side effect.

    The scratch index starts FILLED (a copy of the real index, else
    `read-tree HEAD`), never empty: `add -A` into an empty index treats
    tracked files that match .gitignore as ignored and leaves them out, so
    the diff reported them as deleted (homelab2 `.playwright-mcp/`). A
    filled index keeps them tracked. The real index is only read: git
    writes it by atomic rename, so a plain copy is consistent and takes no
    index.lock.

    Returns the scratch index path: callers diff against it, then remove it."""
    scratch = _scratch_index_path(wt)
    _unlink_quietly(scratch)
    env = {"GIT_INDEX_FILE": str(scratch)}
    seeded = False
    try:
        real = Path(_git(wt, "rev-parse", "--git-path", "index").strip())
        if not real.is_absolute():
            real = wt / real
        shutil.copyfile(real, scratch)
        seeded = True
    except (OSError, WorkspaceError) as e:
        log.debug("copying real index for %s failed: %s", wt, e)
    if not seeded:
        _unlink_quietly(scratch)
        try:
            _git(wt, "read-tree", "HEAD", env=env)
        except WorkspaceError as e:
            log.debug("read-tree HEAD for %s failed (unborn?): %s", wt, e)
    _git(wt, "add", "-A", env=env)
    return scratch


def _unlink_quietly(path: Path) -> None:
    """Remove a scratch file if present; absence or failure is non-fatal."""
    try:
        path.unlink()
    except OSError as e:
        log.debug("removing %s failed: %s", path, e)


def _scratch_index_path(wt: Path) -> Path:
    """Where the throwaway index lives.

    A linked worktree's `.git` is a FILE (the gitdir pointer), not a
    directory — so `wt/.git/anything` fails with "Not a directory". The
    scratch index must sit inside the real git dir instead, which
    --absolute-git-dir reports for both main checkouts and worktrees.

    UNIQUE PER CALL, never one shared name: the board poll (capped
    `code_diff` on GET /stories/{id}) and the code reviewer (uncapped diff
    in `Runner._run_code_review`) stage into a scratch index from different
    threads over the same worktree. With a single fixed path, one caller's
    `_unlink_quietly` landed between the other's `add -A` and its `diff`,
    so git compared the base commit against a missing (read: empty) index —
    every tracked file showed as deleted, the "diff" collapsed to a few
    hundred chars, and the reviewer silently reviewed one chunk of nothing.
    Per-call names keep concurrent diffs independent; each call still
    removes only its own file."""
    git_dir = _git(wt, "rev-parse", "--absolute-git-dir").strip()
    return Path(git_dir) / f"fubala-scratch-index-{os.getpid()}-{next(_SCRATCH_SEQ)}"



def _registered_worktree(wt: Path) -> bool:
    """True only if wt is a live git worktree whose top-level is itself —
    protects against committing/diffing in the MAIN checkout when a
    worktree was removed mid-run (the 005 incident: a story commit landed
    on main with the operator's uncommitted files)."""
    try:
        top = _git(wt, "rev-parse", "--show-toplevel").strip()
    except WorkspaceError:
        return False
    return Path(top).resolve() == wt.resolve()


def _unescape_porcelain_path(raw: str) -> str:
    """Undo git's quoted-path encoding (`"a b/\303\251.txt"` → `a b/é.txt`).

    git quotes any path containing non-ASCII (or quotes/backslashes/control
    chars) and octal-escapes the high bytes; a literal parse would hand the
    guard a path git then fails to unstage."""
    if len(raw) < 2 or not (raw.startswith('"') and raw.endswith('"')):
        return raw
    try:
        return codecs.decode(raw[1:-1], "unicode_escape").encode("latin-1").decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return raw


def staged_paths(porcelain: str) -> list[str]:
    """Paths with STAGED changes from `git status --porcelain` output.

    Pure (no git, no worktree): line XY + space + path (+ ` -> ` + dst for
    renames). Only index-side codes count — X is the index status, Y the
    worktree's; `??` (untracked) is not staged, ` M` (worktree-only edit)
    is not either. Untracked-but-unstaged artefacts are therefore invisible
    here, which is exactly the contract: the guard is pre-commit only."""
    out: list[str] = []
    for line in porcelain.splitlines():
        if len(line) < 4:
            continue
        x, y = line[0], line[1]
        if x in (" ", "?", "!"):
            continue
        path = _unescape_porcelain_path(line[3:])
        if y in ("C", "R"):
            path = path.split(" -> ")[0]
        out.append(path)
    # de-duplicate (a rename can appear with both sides parsed) while
        # keeping a stable order for the note/callers
    seen: set[str] = set()
    unique: list[str] = []
    for p in out:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique


def artefact_matches(paths: list[str]) -> list[str]:
    """Subset of `paths` whose components look like build artefacts.

    Pure: `build`, `dist`, `.venv` match whole components anywhere in the
    path; `*.egg-info` matches a component ENDING in `.egg-info`. So
    `build/lib/x.py` and `pkg.egg-info/PKG-INFO` are artefacts, while
    `src/dist_helper.py`, `dist_utils/x.py` and `tools/build.py` are not —
    substring matching would eat legitimate files."""
    matched: list[str] = []
    for path in paths:
        parts = PurePosixPath(path).parts
        if any(
            seg in ARTEFACT_NAMES
            or (bool(ARTEFACT_SUFFIXES) and seg.endswith(ARTEFACT_SUFFIXES))
            for seg in parts
        ):
            matched.append(path)
    return matched


GitFn = Callable[..., str]
"""`git(*args, ok_codes=())` -> stdout, raising WorkspaceError: the local
`_git` bound to a worktree, or a worker pod's `PodExecutor.git`."""


def _local_git(wt: Path) -> GitFn:
    def git(*args: str, ok_codes: tuple[int, ...] = ()) -> str:
        return _git(wt, *args, ok_codes=ok_codes)

    return git


def _rm_cached_batches(git: GitFn, paths: list[str]) -> None:
    """`git rm -r -f -q --cached -- <paths>` in batches — the exact unstage.

    `--cached` untracks from the index alone and leaves the worktree file
    on disk; `-f` only overrides the "differs from HEAD" safety check (a
    freshly regenerated artefact always differs). Used INSTEAD of `git
    reset --`: reset restores the HEAD entry, so an artefact a previous
    coder round already committed (the ems worktrees bbac771/4d74aba shape)
    would stay tracked forever; rm --cached drops it and the commit then
    records the deletion. It also works on an unborn branch, where reset
    has no HEAD to reset to."""
    for start in range(0, len(paths), UNSTAGE_BATCH):
        batch = paths[start : start + UNSTAGE_BATCH]
        try:
            git(
                "rm",
                "-r",
                "-f",
                "-q",
                "--cached",
                "--",
                *batch,
                ok_codes=(1, 128),
            )
        except WorkspaceError as e:
            log.warning("rm --cached of %s failed: %s", batch[:3], e)


def _tracked_artefact_roots(git: GitFn) -> list[str]:
    """Artefact paths ALREADY in the index before this round's `add -A`.

    `git ls-files` lists index entries, so a `build/lib/...` a previous
    coder round committed shows up here even though `git status` reports
    it as clean (unchanged) — the guard over `staged_paths` alone would
    never see it and the artefact would stay tracked forever. Returns the
    exact tracked paths (not just roots) so `rm --cached` can batch them.
    """
    try:
        out = git("ls-files", "-z").split("\0")
    except WorkspaceError as e:
        log.debug("ls-files failed: %s", e)
        return []
    return artefact_matches([p for p in out if p])


def _unstage_paths(git: GitFn, paths: list[str]) -> list[str]:
    """Remove artefact paths from the index (worktree files untouched).

    Batching keeps pathspec lists bounded — a setuptools `build/lib` tree
    can hold thousands of files. Failures are logged, never raised: a
    warning note is the right response, a failed story is not."""
    if not paths:
        return []
    _rm_cached_batches(git, paths)
    return list(paths)


def commit(
    project_root: str,
    story_id: str,
    message: str,
    on_artefacts: Callable[[list[str]], None] | None = None,
    git: GitFn | None = None,
) -> str:
    """Commit any pending worktree changes onto the story branch.

    `git` (worker pods): run every git command through this callable instead
    of the local worktree (a pod's `PodExecutor.git`); the artefact guard is
    the same either way. The local worktree must still be registered.

    ARTEFACT GUARD: after `add -A` and before committing, staged paths
    matching `ARTEFACT_PATTERNS` (build/, dist/, .venv/, *.egg-info/ — as
    whole path components, at any depth) are unstaged in batches. The
    worktree files stay on disk; only the commit is kept clean. When
    nothing but artefacts were staged, the staged set is empty afterwards
    and the commit is skipped exactly as before. `on_artefacts` (optional,
    runner passes a StoryNote writer) hears the unstaged paths."""
    wt = worktree_path(project_root, story_id)
    if not wt.exists() or not _registered_worktree(wt):
        return ""
    if git is None:
        git = _local_git(wt)
    git("add", "-A")
    status = git("status", "--porcelain").strip()
    reported: list[str] = []
    # (1) artefacts sitting in the index — which, because `add -A` just ran,
    # covers BOTH a tree this round produced and one an earlier coder round
    # already committed (the bbac771/4d74aba shape: `git status` shows it as
    # clean/unchanged, so the staged-paths guard alone would never see it and
    # it would stay tracked forever). `ls-files` reads the index directly.
    # This runs BEFORE the empty-status bail: a round whose only "change" is
    # a tracked artefact still has to untrack it, otherwise the artefact
    # would ride along in every future commit.
    tracked = _tracked_artefact_roots(git)
    if tracked:
        reported.extend(_unstage_paths(git, tracked))
        # the unstage registers index deletions: refresh so the emptiness
        # check below and the returned status reflect the real staged set
        status = git("status", "--porcelain").strip()
    if not status and not reported:
        return ""
    # (2) anything still staged that matches the patterns
    still_staged = staged_paths(git("status", "--porcelain"))
    reported.extend(_unstage_paths(git, artefact_matches(still_staged)))
    if reported:
        # de-duplicate while keeping a stable order for the note
        seen_paths: set[str] = set()
        unique = [p for p in reported if not (p in seen_paths or seen_paths.add(p))]
        if on_artefacts is not None:
            try:
                on_artefacts(unique)
            except Exception:  # noqa: BLE001 — a note must never fail a commit
                log.exception("artefact-note callback failed")
        # After the unstage the artefacts are UNTRACKED (`??`) in the
        # worktree, so status can be non-empty while nothing is staged —
        # `git commit` would then die on "nothing added to commit". Decide
        # on the STAGED set, not on the raw porcelain text.
        if not staged_paths(git("status", "--porcelain")):
            return ""
    git("-c", "user.name=fubala", "-c", "user.email=fubala@local", "commit", "-qm", message)
    return status


def _branch_exists(repo: Path, branch: str) -> bool:
    try:
        _git(repo, "rev-parse", "--verify", "--quiet", branch)
    except WorkspaceError:
        return False
    return True


def branch_head(project_root: str, story_id: str) -> str:
    """The story branch's current commit, or "" when the branch is gone.

    A human rejecting at the code gate pins this so the rerun can tell a
    no-op coder round (no new non-merge commits) from a real one. A missing
    branch (discarded worktree, rebuilt repo) reads as "" — callers must
    treat that as "any commit counts", never a no-op."""
    repo = _repo_root(project_root)
    branch = f"fubala/{story_id}"
    if not _branch_exists(repo, branch):
        return ""
    try:
        return _git(repo, "rev-parse", branch).strip()
    except WorkspaceError:
        return ""


def head_branch(project_root: str, story_id: str) -> str:
    """The worktree HEAD's branch name — "" when detached or worktree gone.

    The code-review gate compares this to fubala/<story_id>: a coder that
    left the worktree on another branch (the ext/* incident) has commits the
    story branch will not carry, and review should say so while the diff —
    read from the worktree FILES — still shows the change. Never raises: a
    git hiccup reads as "no branch", and the gate is advisory."""
    try:
        wt = worktree_path(project_root, story_id)
    except WorkspaceError:
        return ""
    if not wt.exists() or not _registered_worktree(wt):
        return ""
    try:
        out = _git(wt, "rev-parse", "--abbrev-ref", "HEAD").strip()
    except WorkspaceError:
        return ""
    return "" if out == "HEAD" else out


def commits_since(project_root: str, story_id: str, since_ref: str) -> int:
    """Non-merge first-parent commits on the story branch after `since_ref`.

    `--first-parent` walks the story branch's own line only, so main's own
    progress — reachable through the take-in MERGE commit but not authored
    on this branch — is never counted as coder work (AC4). Coder, fix and
    retry rounds land as ordinary commits directly on the branch, so
    `--no-merges` then counts exactly "work the agent did" without matching
    commit messages. An empty or unknown `since_ref` (missing branch at
    reject time, rebuilt worktree) returns 1: never false-park a story for
    a base we cannot resolve."""
    repo = _repo_root(project_root)
    branch = f"fubala/{story_id}"
    if not _branch_exists(repo, branch):
        # the branch itself is gone: there is nothing to count against, and
        # a rebuilt worktree re-pins base_ref anyway — fail open
        return 1
    since = (since_ref or "").strip()
    if not since:
        return 1
    try:
        out = _git(
            repo,
            "rev-list",
            "--count",
            "--first-parent",
            "--no-merges",
            f"{since}..{branch}",
            ok_codes=(1, 128),
        ).strip()
    except WorkspaceError:
        return 1
    try:
        return int(out)
    except ValueError:
        return 1


def _is_merged(repo: Path, branch: str) -> bool:
    """True when branch is already reachable from HEAD."""
    try:
        _git(repo, "merge-base", "--is-ancestor", branch, "HEAD")
    except WorkspaceError:
        return False
    return True


def merge(project_root: str, story_id: str) -> str:
    """Merge the story branch back into the main checkout (--no-ff).

    Idempotent: a branch that is missing or already merged means this
    story was merged before (and its worktree discarded) — report
    "already merged" instead of failing a retried approve. A conflicting
    merge is aborted, so the main checkout keeps neither MERGE_HEAD nor a
    half-staged index."""
    repo = _repo_root(project_root)
    branch = f"fubala/{story_id}"
    # Never silently merged: unmerged paths or leftover markers in the story
    # worktree would be staged as "resolved" by commit()'s `git add -A` and
    # land on main with `<<<<<<<` hunks in them. Refuse before that happens —
    # the approve endpoint surfaces this to the operator instead.
    unresolved = unmerged(project_root, story_id)
    if unresolved:
        raise WorkspaceError(
            "merge with main has conflicts — resolve before approving: "
            + ", ".join(unresolved)
        )
    # commit FIRST: until the worktree's work is committed, the branch tip
    # is the fork point itself and would trivially read as "merged"
    commit(project_root, story_id, f"story {story_id}: executed changes")
    if not _branch_exists(repo, branch) or _is_merged(repo, branch):
        log.info("merge story %s skipped: branch %s missing or already merged", story_id, branch)
        return "already merged"
    try:
        output = _git(repo, "merge", "--no-ff", branch, "-m", f"merge story {story_id}")
    except WorkspaceError as e:
        # (a) best-effort: which files conflicted
        conflicts: list[str] = []
        try:
            conflicts = [
                line.strip()
                for line in _git(repo, "diff", "--name-only", "--diff-filter=U").splitlines()
                if line.strip()
            ]
        except WorkspaceError as ce:
            log.debug("listing conflicted files for story %s failed: %s", story_id, ce)
        # (b) best-effort: undo the half-merge (fails with "no merge to
        # abort" when git refused to start at all, e.g. dirty tree)
        try:
            _git(repo, "merge", "--abort")
        except WorkspaceError as ae:
            log.debug("git merge --abort for story %s failed: %s", story_id, ae)
        # (c) sanity: main must be clean again — warn only, operator dirt
        # that predates the merge is not ours to fail on
        try:
            residue = _git(repo, "status", "--porcelain").strip()
        except WorkspaceError:
            residue = ""
        if residue:
            log.warning(
                "main checkout not clean after aborting merge of story %s:\n%s",
                story_id,
                residue,
            )
        detail = str(e)
        if conflicts:
            detail += f"; conflicting files: {', '.join(conflicts)}"
        detail += " — resolve the conflict manually or discard the story"
        raise WorkspaceError(detail) from e
    return output.strip()


# git push stderr markers that mean "the transport died", not "git refused":
# DNS, TCP connect, reset and route failures are all worth another attempt,
# while a rejected refspec or a missing remote is a permanent error.
_PUSH_TRANSIENT_MARKERS: tuple[str, ...] = (
    "could not resolve host",
    "connection was reset",
    "failed to connect",
    "timed out",
    "route to host",
)
# same bounded policy as the PR client (app/pr.py) so both halves of the
# landing step spend the same window before a story is declared failed
PUSH_MAX_ATTEMPTS = max(1, int(os.environ.get("FUBALA_PUSH_MAX_ATTEMPTS", "3")))
PUSH_BACKOFF_BASE = max(0.0, float(os.environ.get("FUBALA_PUSH_BACKOFF_BASE", "1.0")))
PUSH_RETRY_WINDOW = max(0.0, float(os.environ.get("FUBALA_PUSH_RETRY_WINDOW", "30.0")))


def _is_transient_push_failure(stderr: str) -> bool:
    """True when git's own error text names a network-shaped failure."""
    lowered = stderr.lower()
    return any(marker in lowered for marker in _PUSH_TRANSIENT_MARKERS)


class PushedSha(str):
    """The sha `push` delivered, plus how it relates to the story branch.

    It IS the plain sha — equality, truthiness and JSON all keep working —
    but it carries what the approve path wants to note when the coder had
    left the worktree on some other branch: `adopted` (the story branch had
    to be moved onto the worktree HEAD first) and `head_branch` (the branch
    the worktree was actually on, "" when detached)."""

    adopted: bool = False
    head_branch: str = ""


def push(
    project_root: str,
    story_id: str,
    remote: str = "origin",
    base_ref: str | None = None,
) -> str:
    """Push the story's work to the remote; returns the pushed sha.

    Delivery mode "push": the worktree's committed HEAD (work committed
    first, exactly as merge does) is pushed to `remote` as
    refs/heads/fubala/<id>. The pushed sha is INVARIANTLY the worktree HEAD
    at approve time — whatever branch the coder left checked out, its tip is
    what the reviewer saw (diff reads the worktree files) and what must
    land. When `fubala/<id>` does not already point at that commit the
    branch is adopted first: `git branch [-f] fubala/<id> <head>` from the
    project root, which is safe exactly because the ref is not checked out
    anywhere when it diverged; when the worktree still sits on
    `fubala/<id>` the tips match and nothing is ever forced. The returned
    PushedSha carries `adopted`/`head_branch` so the caller can note it.

    `base_ref` (the story's PINNED fork point, never current main) guards
    the empty push: a HEAD that is an ancestor of (or equal to) the base
    adds no story work, and pushing it would land nothing while reporting
    success — refuse loudly instead. A branch cut from NEWER main plus one
    commit is a valid push, not "empty", which is why the comparison is
    against the pin.

    The main checkout is never touched — no fetch, no merge, no reset — so
    this is safe to run while a human works in the root.

    Unlike merge, a missing/unregistered worktree is an error, not "already
    done": there is no idempotent local state to observe, and silently
    reporting success would hide a story whose branch never existed.

    Transport-marker failures (DNS, reset, connect, timeout, route) are
    retried with the same bounded policy as the PR client; on exhaustion the
    WorkspaceError message is prefixed `transient network error (N attempts)`
    so story.error says "transient" on both halves of the landing step.
    """
    repo = _repo_root(project_root)
    branch = f"fubala/{story_id}"
    wt = worktree_path(project_root, story_id)
    if not wt.exists() or not _registered_worktree(wt):
        raise WorkspaceError(
            f"worktree for {story_id} does not exist — nothing to push"
        )
    # commit FIRST (same contract as merge): until the worktree's work is
    # committed, the branch tip is the fork point and would push nothing.
    commit(project_root, story_id, f"story {story_id}: executed changes")
    head = _git(wt, "rev-parse", "HEAD").strip()
    if base_ref:
        try:
            _git(repo, "merge-base", "--is-ancestor", head, base_ref)
        except WorkspaceError:
            pass  # not an ancestor: there IS story work beyond the base
        else:
            raise WorkspaceError(
                f"push refused: {head} adds nothing beyond base {base_ref} — "
                "no story work to land"
            )
    # adopt the worktree HEAD onto the story branch when it is not there
    # already (a coder that committed on another branch, or a branch that
    # lost its worktree). tip == head — the normal checkout — skips the
    # force entirely: a checked-out ref must never be forced.
    tip = branch_head(project_root, story_id)
    adopted = tip != head
    if adopted:
        _git(repo, *(["branch", "-f", branch, head] if tip else ["branch", branch, head]))
    on_branch = ""
    try:
        on_branch = _git(wt, "rev-parse", "--abbrev-ref", "HEAD").strip()
    except WorkspaceError:
        pass
    # Always force the remote ref to the local branch: a re-run story pushes
    # a rewritten branch and must overwrite whatever the earlier attempt left
    # on the remote (it is fubala's own namespace).
    last_error = ""
    deadline = time.monotonic() + PUSH_RETRY_WINDOW
    for attempt in range(1, PUSH_MAX_ATTEMPTS + 1):
        try:
            _git(
                repo,
                "push",
                "--force",
                remote,
                f"{branch}:refs/heads/{branch}",
            )
            result = PushedSha(head)
            result.adopted = adopted
            result.head_branch = "" if on_branch == "HEAD" else on_branch
            return result
        except WorkspaceError as e:
            message = str(e)
            if not _is_transient_push_failure(message):
                raise
            last_error = message
            if attempt >= PUSH_MAX_ATTEMPTS:
                break
            # only an exhausted WINDOW stops the retrying: a zero backoff
            # base must retry at once, not give up after the first attempt
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            delay = min(PUSH_BACKOFF_BASE * (2 ** (attempt - 1)), remaining)
            log.warning(
                "push of %s failed transiently (attempt %d/%d): %s — retrying in %.1fs",
                branch,
                attempt,
                PUSH_MAX_ATTEMPTS,
                message,
                delay,
            )
            if delay > 0:
                time.sleep(delay)
    raise WorkspaceError(
        f"transient network error ({attempt}): git push {branch}: {last_error}"
    )


def discard(project_root: str, story_id: str) -> None:
    """Drop the worktree (wherever it lives) and branch; the story's changes
    evaporate."""
    repo = _repo_root(project_root)
    branch = f"fubala/{story_id}"
    # current in-repo location and the legacy parent-of-repo one, for stories
    # created before the move
    for wt in (
        repo / WORKTREE_DIR / story_id,
        repo.parent / WORKTREE_DIR / story_id,
    ):
        if wt.exists():
            try:
                _git(repo, "worktree", "remove", "--force", str(wt))
            except WorkspaceError:
                pass  # stale metadata; branch cleanup below still runs
    try:
        _git(repo, "branch", "-D", branch)
    except WorkspaceError:
        pass  # branch may not exist; discard is best-effort


def changes_exist(project_root: str, story_id: str, base_ref: str | None = None) -> bool:
    return bool(diff(project_root, story_id, base_ref).strip())


SNAPSHOT_REF_PREFIX = "refs/fubala-snapshot/"
_SNAPSHOT_IDENT = {
    "GIT_AUTHOR_NAME": "fubala",
    "GIT_AUTHOR_EMAIL": "fubala@localhost",
    "GIT_AUTHOR_DATE": "1970-01-01T00:00:00Z",
    "GIT_COMMITTER_NAME": "fubala",
    "GIT_COMMITTER_EMAIL": "fubala@localhost",
    "GIT_COMMITTER_DATE": "1970-01-01T00:00:00Z",
}


def snapshot_bundle(
    worktree: Path | str, story_id: str, since: str | None = None
) -> tuple[bytes, str]:
    """Bundle the worktree's current state, uncommitted and untracked included.

    Returns ``(bundle_bytes, sha)``: `sha` is a snapshot commit (parent HEAD,
    tree = everything `git add -A` sees) reachable in the bundle as
    ``refs/fubala-snapshot/<story_id>``. With `since` (a sha the receiver
    already has) only ``since..`` is bundled; when that leaves nothing new the
    bytes are empty and the receiver just checks `sha` out again.

    Nothing is pushed and nothing the board relies on is touched: the commit is
    built through a COPY of the index (GIT_INDEX_FILE), the temporary ref is
    removed in `finally`, and HEAD, the real index and the files stay as they
    were. The fixed author/committer (and date) make equal trees give equal
    shas, which is what makes the "nothing new" case detectable.
    """
    wt = Path(worktree)
    ref = f"{SNAPSHOT_REF_PREFIX}{story_id}"
    scratch = _scratch_index_path(wt)
    bundle = scratch.with_name(scratch.name + ".bundle")
    env = {"GIT_INDEX_FILE": str(scratch), **_SNAPSHOT_IDENT}
    ref_made = False
    try:
        try:
            real = Path(_git(wt, "rev-parse", "--git-path", "index").strip())
            if not real.is_absolute():
                real = wt / real
            shutil.copyfile(real, scratch)
        except OSError:
            # no index yet (fresh checkout): the add below fills it
            _unlink_quietly(scratch)
        _git(wt, "add", "-A", env=env)
        tree = _git(wt, "write-tree", env=env).strip()
        sha = _git(
            wt, "commit-tree", tree, "-p", "HEAD", "-m", f"fubala snapshot {story_id}",
            env=env,
        ).strip()
        _git(wt, "update-ref", ref, sha)
        ref_made = True
        if since and since == sha:
            return b"", sha
        args = ["bundle", "create", str(bundle), ref]
        if since:
            try:
                _git(wt, "cat-file", "-e", f"{since}^{{commit}}")
                args.append(f"^{since}")
            except WorkspaceError:
                pass  # the base is gone locally: send a full bundle
        _git(wt, *args)
        return bundle.read_bytes(), sha
    except OSError as e:
        raise WorkspaceError(f"snapshot bundle failed: {e}") from e
    finally:
        if ref_made:
            try:
                _git(wt, "update-ref", "-d", ref)
            except WorkspaceError as e:
                log.debug("removing %s failed: %s", ref, e)
        _unlink_quietly(scratch)
        _unlink_quietly(bundle)


PULL_REF_PREFIX = "refs/fubala-pull/"


def worktree_head(worktree: Path | str) -> str:
    return _git(Path(worktree), "rev-parse", "HEAD").strip()


def merge_in_progress(worktree: Path | str) -> bool:
    """True while a merge (MERGE_HEAD) is pending in the worktree."""
    try:
        _git(Path(worktree), "rev-parse", "-q", "--verify", "MERGE_HEAD")
    except WorkspaceError:
        return False
    return True


def pull_bundle(
    worktree: Path | str, story_id: str, bundle: bytes, start: str, new: str
) -> None:
    """Fast-forward the worktree from `start` to `new` with a bundle of
    `start..new` (a worker pod's commits).

    The bundle is fetched into a temporary ref; local HEAD must still be
    `start` and `start` an ancestor of `new`. Then `reset --hard new`: plain
    `merge --ff-only` refuses while the same files are still dirty locally
    (the uncommitted work the pod just committed). The temporary ref is
    removed; no other ref changes."""
    wt = Path(worktree)
    ref = f"{PULL_REF_PREFIX}{story_id}"
    fd, name = tempfile.mkstemp(prefix="fubala-pull-", suffix=".bundle")
    path = Path(name)
    ref_made = False
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(bundle)
        _git(wt, "fetch", "-q", "--no-tags", "--no-write-fetch-head", str(path), f"+HEAD:{ref}")
        ref_made = True
        fetched = _git(wt, "rev-parse", ref).strip()
        if fetched != new:
            raise WorkspaceError(f"pulled bundle head {fetched[:12]} != {new[:12]}")
        head_now = worktree_head(wt)
        if head_now != start:
            raise WorkspaceError(
                f"local HEAD moved during the pod run ({head_now[:12]} != {start[:12]})"
            )
        _git(wt, "merge-base", "--is-ancestor", start, new)
        _git(wt, "reset", "-q", "--hard", new)
    except OSError as e:
        raise WorkspaceError(f"pull bundle failed: {e}") from e
    finally:
        if ref_made:
            try:
                _git(wt, "update-ref", "-d", ref)
            except WorkspaceError as e:
                log.debug("removing %s failed: %s", ref, e)
        _unlink_quietly(path)
