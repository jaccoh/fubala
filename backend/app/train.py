"""Merge train: batch a project's gate-approved stories into one PR.

A project with ``merge_train`` on (push delivery, full pr_* wiring) never
opens a story PR again. Instead:

  join    — ``_approve_push`` pushes ``fubala/<story>`` and drops the story
            into the project's current *forming* batch (no PR).
  flush   — the scheduler tick flushes a forming batch once
            ``train_window_s`` elapsed since the batch was created or the
            member count reached ``train_max``: branch ``train/<batch_id>``
            off the base, merge every member branch in gate order, push it,
            open ONE pull request. A member that conflicts is dropped and
            bounced to the human gate with the conflicted files.
  judge   — the poller judges the BATCH, never the member's own PR fields:
            refresh the branch head from the remote, read CI only from that
            head. First red on it consumes exactly one flake re-run
            (``pr.update_branch``); a second red bisects the batch into two
            child batches; a 1-member batch bounces its story to the coder
            with the CI failure. A merged batch PR lands every member whose
            branch is an ancestor of main.

Everything is resumable: the batch row (state, pr_index, head_sha, red_runs)
is the whole truth, so a crash between the push and the PR create simply
re-creates the PR on the next tick.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from . import pr as pr_module
from . import workspaces
from .events import bus
from .models import (
    Project,
    ProjectDelivery,
    Story,
    StoryStatus,
    TrainBatch,
    TrainBatchState,
    utcnow,
)
from .schemas import as_utc_iso

log = logging.getLogger(__name__)

# One lock for the train: the scheduler tick holds it per project and every
# operator function (pause, rerun, bounce, land-now, force-merge) holds it for
# its whole read-decide-write, so a route never interleaves with a tick that
# is flushing or judging the same batch. Re-entrant: operators call helpers
# that are also used by the tick.
_TRAIN_LOCK = threading.RLock()

# The batch branch namespace. Never collides with fubala/<story> because the
# batch id is a hex uuid and story ids are hex uuids too — the prefix is the
# discriminator.
TRAIN_PREFIX = "train"
DEFAULT_WINDOW_S = 300
DEFAULT_MAX = 6

# Tests hold a forming batch open with this FLAG until every intended member
# has joined, so batch formation never touches the wall clock (the load
# reproductions behind the last reject: a 1 s window under `-n 8` let the
# batch depart before the later members joined, splitting it). A flag, not a
# timer: nothing about formation can race a loaded node. Production never
# sets it — the window alone governs — and the TRAIN_MAX_HOLD_S bound below
# still overrides it, so even a forgotten flag cannot stall a train forever.
TRAIN_HOLD_FORMING = False

# How long a red re-run's note is remembered for de-duplication: the note is
# written once per red (keyed on the batch state), never per poll.
_RE_RUN_NOTE = "merge train: batch re-run after a red (possible flake)"

# A merged batch PR whose members are NOT (yet) in main. The merge verdict
# and the fetch of main are two observations that can legitimately disagree
# for a moment (the merge commit is landing on the remote while our fetch
# already answered), so a single "not in main" reading is never a verdict:
# it only counts after it has persisted this long — the same dwell shape the
# land poller uses for merge conflicts (_LAND_CONFLICT in runner.py).
_LAND_MISSING = "batch_merge_pending"
LAND_MISSING_MIN_DWELL = float(
    os.environ.get("FUBALA_TRAIN_LAND_MISSING_MIN_DWELL", "20.0")
)


def _missing_since(land_state: str | None) -> datetime | None:
    """When the current run of `not in main` observations started.

    land_state holds "<_LAND_MISSING>:<ISO-8601 UTC timestamp>" so the dwell
    survives a restart (all state lives on the row). None = not dwelling."""
    if not land_state or not land_state.startswith(_LAND_MISSING):
        return None
    stamp = land_state[len(_LAND_MISSING) :].lstrip(":")
    try:
        seen = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=UTC)  # SQLite round trips lose tzinfo
    return seen


def _as_utc(value: datetime | None) -> datetime | None:
    """Normalize a datetime read back from SQLite (naive = UTC)."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def train_now() -> datetime:
    """Injectable clock for the flush window (tests monkeypatch this)."""
    return utcnow()


# While a batch's CI runs on the shared node, the scheduler starts no story and
# pipeline verifies wait. A CI that never reports lifts the hold after the cap.
TRAIN_CI_HOLD_CAP_S = 1800


def _pending_ci_batches(db: Session) -> list[tuple[TrainBatch, float]]:
    """OPEN, pending-CI batches of train-ready projects with their age in s."""
    out: list[tuple[TrainBatch, float]] = []
    rows = db.exec(
        select(TrainBatch)
        .where(
            TrainBatch.state == TrainBatchState.OPEN,
            TrainBatch.ci_status == "pending",
        )
        .order_by(TrainBatch.created_at)
    ).all()
    for batch in rows:
        project = db.get(Project, batch.project_id)
        if project is None or not project.merge_train or not train_ready(project):
            continue
        created = _as_utc(batch.created_at)
        age = (train_now() - created).total_seconds() if created else 0.0
        out.append((batch, age))
    return out


def train_ci_hold(db: Session) -> TrainBatch | None:
    """The oldest batch whose CI is pending inside the cap, else None."""
    for batch, age in _pending_ci_batches(db):
        if age < TRAIN_CI_HOLD_CAP_S:
            return batch
    return None


def train_ci_hold_expired(db: Session) -> list[TrainBatch]:
    """Pending batches past the cap (the hold no longer covers them)."""
    return [b for b, age in _pending_ci_batches(db) if age >= TRAIN_CI_HOLD_CAP_S]


# A CI pod that never came up is infrastructure, not a verdict on the code:
# the batch is retried unchanged after a backoff and never bisected.
INFRA_RETRY_BASE_S = 300
INFRA_RETRY_MAX_S = 1800
INFRA_RETRY_CAP = 3
INFRA_LOG_TAIL_CHARS = 4000
_INFRA_RE = re.compile(r"timeout waiting for (test|land) pod", re.IGNORECASE)
_PYTEST_FAILED_LINE_RE = re.compile(r"^[ \t]*(?:\S+Z[ \t]+)?FAILED (.+)$", re.MULTILINE)
_PYTEST_FAILED_COUNT_RE = re.compile(r"(?<![\w.])(\d+) failed\b")
PYTEST_FAILED_IDS_MAX = 50


def _pytest_failures(text: str) -> list[str] | None:
    """Failing pytest test ids in a CI log: a list of ids, [] when only a
    "N failed" count (N > 0) was found, None when there is no pytest failure.

    Collection errors ("N error") are deliberately not a failure here."""
    ids: list[str] = []
    for m in _PYTEST_FAILED_LINE_RE.finditer(text):
        node = m.group(1).split(" - ", 1)[0].strip()
        if node and node not in ids:
            ids.append(node)
    if ids:
        return ids[:PYTEST_FAILED_IDS_MAX]
    if any(int(m.group(1)) > 0 for m in _PYTEST_FAILED_COUNT_RE.finditer(text)):
        return []
    return None


def _record_note(db: Session, story_id: str, data: dict[str, Any]) -> None:
    from .models import StoryEvent

    db.add(StoryEvent(story_id=story_id, kind="note", data=json.dumps(data)))


def _publish_story(db: Session, story: Story) -> None:
    """Commit + publish one story snapshot (commit first: the phantom-status
    rule from routers/stories._commit_and_publish)."""
    from .runner import story_payload

    story.updated_at = utcnow()
    db.add(story)
    db.commit()
    bus.publish("story", {"story": story_payload(story)})


# ---- project helpers -------------------------------------------------------


def train_enabled(project: Project) -> bool:
    return bool(project.merge_train)


def train_window_s(project: Project) -> int:
    # `or` would read an explicit 0 as "unset" (0 is falsy) — and 0 is a
    # meaningful setting: "depart as soon as the batch is quiet", which is
    # also the deterministic departure trigger the tests use.
    value = project.train_window_s
    return DEFAULT_WINDOW_S if value is None else int(value)


def train_max(project: Project) -> int:
    return int(project.train_max or DEFAULT_MAX)


def train_ready(project: Project | None) -> bool:
    """Can this project run a train? Push delivery + the full pr_* wiring —
    the batch PR is an ordinary PR, so everything the story PR needed is
    still needed."""
    if project is None:
        return False
    if (project.delivery or ProjectDelivery.MERGE) != ProjectDelivery.PUSH:
        return False
    return all(
        (
            project.pr_api_base,
            project.pr_repo,
            project.pr_base_branch,
            project.pr_token_file,
        )
    )


# ---- join (called from _approve_push) --------------------------------------


def join(
    db: Session,
    project: Project,
    story: Story,
    prev: str,
    approved_by: str | None,
    pushed_sha: str,
) -> Story:
    """Put a gate-approved, already-pushed story on the project's train.

    The branch is on the remote (``workspaces.push`` ran in _approve_push);
    no PR is opened here. The story sits in LANDING until its batch lands.
    """
    batch = db.exec(
        select(TrainBatch).where(
            TrainBatch.project_id == project.id,
            TrainBatch.state == TrainBatchState.FORMING,
            TrainBatch.not_before == None,  # noqa: E711 — replacements are sealed
        )
    ).first()
    if batch is None:
        batch = TrainBatch(project_id=project.id, state=TrainBatchState.FORMING)
        db.add(batch)
        db.flush()  # stamp batch.id before it is referenced
    members = json.loads(batch.story_ids or "[]")
    members.append(story.id)
    batch.story_ids = json.dumps(members)
    db.add(batch)

    story.train_batch_id = batch.id
    story.pushed_sha = pushed_sha or None
    story.land_state = None
    story.error = None
    story.status = StoryStatus.LANDING
    _record_note(
        db,
        story.id,
        {
            "delivery": "push",
            "merge_train": True,
            "train_batch_id": batch.id,
            "batch_members": len(members),
            "pushed_sha": pushed_sha or None,
            "approved_by": approved_by,
        },
    )
    from .runner import record_event

    record_event(
        db,
        story.id,
        "status",
        {
            "from": prev,
            "to": story.status,
            "approved_by": approved_by,
            "pushed": True,
            "pushed_sha": pushed_sha,
            "landing": True,
            "merge_train": True,
            "train_batch_id": batch.id,
        },
    )
    db.commit()
    return story


# ---- git plumbing for the batch branch -------------------------------------


def _batch_branch(batch_id: str) -> str:
    return f"{TRAIN_PREFIX}/{batch_id}"


def _retire(
    project: Project, batch: TrainBatch, reason: str, *, close: bool = True
) -> None:
    """Close a replaced or abandoned batch PR and delete its branch.

    Best effort and never raises: call it only after the state change that
    makes the batch obsolete is committed. A failure is logged and recorded
    as a note on the members."""
    try:
        token = pr_module.read_token(project.pr_token_file or "")
        if close and batch.pr_index is not None:
            try:
                info = pr_module.get_pull(
                    project.pr_api_base, project.pr_repo, token, int(batch.pr_index)
                )
            except pr_module.PullRequestError:
                info = {"state": "open"}
            if info.get("state") != "closed" and not info.get("merged"):
                try:
                    pr_module.close_pull(
                        project.pr_api_base,
                        project.pr_repo,
                        token,
                        int(batch.pr_index),
                    )
                except pr_module.PullRequestError as e:
                    log.warning(
                        "retiring batch %s: closing PR #%s failed: %s",
                        batch.id,
                        batch.pr_index,
                        e,
                    )
        pr_module.delete_branch(
            project.pr_api_base, project.pr_repo, token, _batch_branch(batch.id)
        )
    except Exception as e:  # noqa: BLE001 — retiring must never sink the tick
        log.warning("retiring batch %s (%s) failed: %s", batch.id, reason, e)
        db = Session.object_session(batch)
        if db is None:
            return
        try:
            for sid in json.loads(batch.story_ids or "[]"):
                _record_note(
                    db,
                    sid,
                    {
                        "merge_train": True,
                        "train_batch_id": batch.id,
                        "reason": f"retiring batch {batch.id} ({reason}) failed: {e}",
                    },
                )
            db.commit()
        except Exception:  # noqa: BLE001
            log.exception("recording the retire failure for %s failed", batch.id)


def _batch_worktree(project_root: str, batch_id: str) -> Path:
    repo = workspaces._repo_root(project_root)
    return repo / workspaces.WORKTREE_DIR / f"{TRAIN_PREFIX}-{batch_id}"


def _git(repo: Path, *args: str) -> str:
    return workspaces._git(repo, *args)


def _conflict_files(wt: Path) -> list[str]:
    try:
        out = _git(wt, "diff", "--name-only", "--diff-filter=U")
    except workspaces.WorkspaceError:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def _abort_merge(wt: Path) -> None:
    try:
        _git(wt, "merge", "--abort")
    except workspaces.WorkspaceError:
        pass  # git refused to start the merge at all: nothing to undo


def _remove_worktree(project_root: str, wt: Path) -> None:
    try:
        repo = workspaces._repo_root(project_root)
        _git(repo, "worktree", "remove", "--force", str(wt))
    except workspaces.WorkspaceError as e:
        log.debug("removing batch worktree %s failed: %s", wt, e)


def _is_ancestor(project_root: str, ref: str, base: str) -> bool:
    repo = workspaces._repo_root(project_root)
    try:
        _git(repo, "merge-base", "--is-ancestor", ref, base)
    except workspaces.WorkspaceError:
        return False
    return True


def _fetch_origin(project_root: str) -> None:
    repo = workspaces._repo_root(project_root)
    _git(repo, "fetch", "origin")


def _bounce(
    db: Session,
    story: Story,
    message: str,
    note: dict[str, Any],
) -> None:
    """Return a train member to the human gate (conflict / CI failure)."""
    from .runner import record_event

    prev = story.status
    story.status = StoryStatus.HUMAN_REVIEW
    story.error = message
    story.land_state = None
    # the member is out of the batch: its own PR fields describe the BATCH
    # PR, which no longer speaks for this story
    story.train_batch_id = None
    story.pr_index = None
    story.pr_url = None
    _record_note(db, story.id, note)
    record_event(
        db,
        story.id,
        "status",
        {
            "from": prev,
            "to": story.status,
            "landing": True,
            "merge_train": True,
            "error": message,
            **{k: v for k, v in note.items() if k != "reason"},
        },
    )
    _publish_story(db, story)


# ---- flush -----------------------------------------------------------------


def _flush(db: Session, project: Project, batch: TrainBatch, red_runs: int = 0) -> None:
    """Build the batch branch, push it, open the ONE pull request."""
    from .runner import record_event

    project_root = project.root
    base = project.pr_base_branch or "main"
    members = json.loads(batch.story_ids or "[]")
    if not members:
        batch.state = TrainBatchState.RED
        batch.red_runs = red_runs
        db.add(batch)
        db.commit()
        return

    wt = _batch_worktree(project_root, batch.id)
    branch = _batch_branch(batch.id)
    repo = workspaces._repo_root(project_root)
    _fetch_origin(project_root)
    base_ref = f"origin/{base}"
    # pin the flush-time base from the SAME base_ref every flush: the done
    # gate later tells "the member's own commit is in main" from "the branch
    # added nothing" by comparing pushed_sha against THIS sha, so a re-flush
    # (crash recovery, update-branch conflict) must re-stamp it consistently
    # even though the re-flush rewrites head_sha.
    try:
        base_sha = _git(repo, "rev-parse", base_ref).strip()
    except workspaces.WorkspaceError:
        base_sha = ""
    conflicted: list[str] = []
    landed: list[str] = []
    head_sha = ""
    try:
        _remove_worktree(project_root, wt)  # a crashed earlier attempt
        _git(repo, "worktree", "add", "--detach", str(wt), base_ref)
        _git(wt, "checkout", "-q", "-B", branch, base_ref)
        for sid in members:
            story = db.get(Story, sid)
            if story is None or story.status != StoryStatus.LANDING:
                # rejected/discarded while the batch was forming: not ours
                _record_note(
                    db,
                    sid,
                    {
                        "delivery": "push",
                        "merge_train": True,
                        "dropped": True,
                        "train_batch_id": batch.id,
                        "reason": "story left landing before the flush",
                    },
                )
                continue
            member_ref = f"origin/fubala/{sid}"
            try:
                _git(wt, "merge", "--no-ff", member_ref, "-m", f"train {batch.id}: story {sid}")
                landed.append(sid)
            except workspaces.WorkspaceError:
                files = _conflict_files(wt)
                _abort_merge(wt)
                conflicted.append(sid)
                if story is not None:
                    detail = (
                        f"land conflict (batch {batch.id}): "
                        + (", ".join(files) if files else "unknown files")
                    )
                    _bounce(
                        db,
                        story,
                        f"land failed: {detail}",
                        {
                            "delivery": "push",
                            "merge_train": True,
                            "train_batch_id": batch.id,
                            "conflict": detail,
                            "files": files,
                            "reason": detail,
                        },
                    )
        if not landed:
            # every member conflicted: nothing to ship, no PR to open
            batch.state = TrainBatchState.RED
            batch.red_runs = red_runs
            batch.story_ids = json.dumps(landed)
            db.add(batch)
            db.commit()
            _remove_worktree(project_root, wt)
            return
        head_sha = _git(wt, "rev-parse", "HEAD").strip()
        # Always force: a re-flush (crash recovery, update_branch conflict)
        # rewrites the batch branch and must overwrite its own earlier push.
        _git(wt, "push", "--force", "origin", f"HEAD:refs/heads/{branch}")
    except workspaces.WorkspaceError as e:
        log.warning("train batch %s could not be built: %s", batch.id, e)
        _remove_worktree(project_root, wt)
        return

    token = pr_module.read_token(project.pr_token_file or "")
    pr_url: str | None = None
    pr_index: int | None = None
    try:
        html_url, pr_index = pr_module.create_pull(
            base_url=project.pr_api_base,
            repo=project.pr_repo,
            token=token,
            head=branch,
            base_branch=base,
            title=f"merge train {batch.id[:8]} ({len(landed)} stories)",
            body="Fubala merge train batch: " + ", ".join(landed),
        )
        pr_url = pr_module.public_pr_url(
            html_url,
            api_base=project.pr_api_base,
            web_base=project.pr_web_base,
            repo=project.pr_repo,
            index=pr_index,
        )
    except pr_module.PullRequestConflictError:
        # crash between the push and the PR create: adopt the one that exists
        try:
            found = pr_module.find_open_pull(
                base_url=project.pr_api_base,
                repo=project.pr_repo,
                token=token,
                head=branch,
            )
        except pr_module.PullRequestError:
            found = None
        if found is None:
            log.warning("train batch %s: existing PR not adoptable", batch.id)
            _remove_worktree(project_root, wt)
            return
        found_url, pr_index = found
        pr_url = pr_module.public_pr_url(
            found_url,
            api_base=project.pr_api_base,
            web_base=project.pr_web_base,
            repo=project.pr_repo,
            index=pr_index,
        )
    except pr_module.PullRequestError as e:
        log.warning("train batch %s PR create failed: %s", batch.id, e)
        _remove_worktree(project_root, wt)
        return
    finally:
        try:
            _remove_worktree(project_root, wt)
        except Exception:  # noqa: BLE001 — cleanup must never fail the flush
            log.exception("batch worktree cleanup failed for %s", batch.id)

    batch.state = TrainBatchState.OPEN
    batch.story_ids = json.dumps(landed)
    batch.pr_url = pr_url
    batch.pr_index = pr_index
    batch.head_sha = head_sha or None
    batch.base_sha = base_sha or None
    batch.red_runs = red_runs
    batch.ci_status = "pending"
    batch.created_at = train_now()
    db.add(batch)
    for sid in landed:
        story = db.get(Story, sid)
        if story is None:
            continue
        story.train_batch_id = batch.id
        story.pr_url = pr_url
        story.pr_index = pr_index
        story.land_state = None
        db.add(story)
        _record_note(
            db,
            sid,
            {
                "delivery": "push",
                "merge_train": True,
                "train_batch_id": batch.id,
                "pr_url": pr_url,
                "pr_index": pr_index,
                "reason": f"merge train batch {batch.id} opened {pr_url}",
            },
        )
        record_event(db, sid, "note", {"train_batch_opened": batch.id})
        # The member worktree is KEPT (spec step 2): a bounced member must
        # keep its own code for the coder's rework. It is discarded only
        # when that member reaches DONE, exactly like the normal land path.
    db.commit()
    for sid in landed:
        story = db.get(Story, sid)
        if story is not None:
            _publish_story(db, story)


def _drop_departed(db: Session, batch: TrainBatch) -> None:
    """Members rejected while the batch was forming leave the train."""
    members = json.loads(batch.story_ids or "[]")
    if not members:
        return
    keep: list[str] = []
    for sid in members:
        story = db.get(Story, sid)
        if story is not None and story.status == StoryStatus.LANDING:
            keep.append(sid)
        elif story is not None:
            story.train_batch_id = None
            db.add(story)
    if keep != members:
        batch.story_ids = json.dumps(keep)
        db.add(batch)
        db.commit()


# ---- judge (the poller side) ----------------------------------------------


def _refresh_head(project: Project, batch: TrainBatch) -> str | None:
    """Re-read the batch branch head from the remote and STORE it.

    CI verdicts are only ever read from this stored head: a status left on a
    sha the branch update replaced is neither red nor green. This is also
    what protects the children of a bisected batch — their heads differ from
    the parent's, so the parent's stale statuses cannot reach them.
    """
    if not project.pr_api_base or not project.pr_repo:
        return batch.head_sha
    try:
        token = pr_module.read_token(project.pr_token_file or "")
        head = pr_module.branch_head(
            project.pr_api_base,
            project.pr_repo,
            token,
            _batch_branch(batch.id),
        )
    except pr_module.PullRequestError as e:
        log.warning("batch %s head refresh failed: %s", batch.id, e)
        return batch.head_sha
    if head:
        batch.head_sha = head
    return batch.head_sha


# the bounce reason when a member's own commit is missing from the merged
# batch — distinct from "the branch never reached main" so the incident
# shape (branch == base, nothing added) is visible on the board
_LAND_ABSENT = "land failed: story commit absent from merged batch"
_LAND_NOT_IN_MAIN = "land failed: story branch not in main after the batch merge"


def _member_landed(
    project_root: str, story: Story, batch: TrainBatch, base_ref: str
) -> tuple[bool, str]:
    """(landed, bounce message) for one member of a merged batch.

    A member counts as landed only when its OWN commit is in the merged
    main: pushed_sha set, an ancestor of origin/<base>, and NOT an ancestor
    of the batch's flush-time base_sha. A pushed_sha the base already
    carried is the empty-push shape — the branch added nothing, the batch
    merged no story work for it — and must never read as done (an
    ancestor-of-base_sha check also passes through the take_in_main merge
    commit, because the member's own commit postdates the batch base).

    Legacy rows — no pushed_sha recorded, or a batch flushed before
    base_sha existed — fall back to today's member-ref check, so nothing
    already in landing is stranded by the upgrade."""
    pushed = (story.pushed_sha or "").strip()
    base_sha = (batch.base_sha or "").strip()
    if not pushed or not base_sha:
        ok = _is_ancestor(project_root, f"origin/fubala/{story.id}", base_ref)
        return ok, _LAND_NOT_IN_MAIN
    if not _is_ancestor(project_root, pushed, base_ref):
        return False, _LAND_NOT_IN_MAIN
    if _is_ancestor(project_root, pushed, base_sha):
        return False, _LAND_ABSENT
    return True, _LAND_NOT_IN_MAIN


def _land_batch(db: Session, project: Project, batch: TrainBatch) -> None:
    """The batch PR merged: fetch main, land every member that is in it.

    "Merged" and "our fetch of main contains it" are two observations that
    can legitimately disagree for a moment (the merge commit propagating to
    the refs we can see), so a member that is not an ancestor of main is
    never bounced on a single reading: it dwells first
    (LAND_MISSING_MIN_DWELL, the same shape the land poller uses for merge
    conflicts), and only a missing that persists past the dwell bounces it.
    The dwell stamp lives on story.land_state, so it survives a restart.
    """
    from .runner import record_event

    project_root = project.root
    base = project.pr_base_branch or "main"
    base_ref = f"origin/{base}"
    try:
        _fetch_origin(project_root)
    except workspaces.WorkspaceError as e:
        log.warning("batch %s: fetch for the landing judgement failed: %s", batch.id, e)
        return
    batch.state = TrainBatchState.MERGED
    batch.red_runs = 0
    db.add(batch)
    db.commit()
    for sid in json.loads(batch.story_ids or "[]"):
        story = db.get(Story, sid)
        if story is None or story.train_batch_id != batch.id:
            continue
        if story.status not in (StoryStatus.LANDING, StoryStatus.HUMAN_REVIEW):
            continue
        # done gate: the member's OWN commit must be in the merged main —
        # an empty branch (tip == the batch base) is an ancestor of main
        # too, and marking it done would discard real, reviewed work
        in_main, bounce_message = _member_landed(project_root, story, batch, base_ref)
        if not in_main:
            seen = _missing_since(story.land_state)
            if seen is None:
                # first reading: start the dwell clock, judge again later
                story.land_state = f"{_LAND_MISSING}:{as_utc_iso(utcnow())}"
                db.add(story)
                db.commit()
                continue
            if (utcnow() - seen).total_seconds() < LAND_MISSING_MIN_DWELL:
                # the merge verdict and our fetch of main can race: keep
                # waiting, never bounce on a fresh reading
                db.add(story)
                db.commit()
                continue
            _bounce(
                db,
                story,
                bounce_message,
                {
                    "delivery": "push",
                    "merge_train": True,
                    "train_batch_id": batch.id,
                    "pr_url": batch.pr_url,
                    "reason": (
                        "story commit absent from the merged batch "
                        "(pushed_sha is the batch base)"
                        if bounce_message is _LAND_ABSENT
                        else "branch not an ancestor of main after the batch merge"
                    ),
                },
            )
            continue
        prev = story.status
        story.status = StoryStatus.DONE
        story.error = None
        story.land_state = None
        story.train_batch_id = None
        _record_note(
            db,
            sid,
            {
                "delivery": "push",
                "merge_train": True,
                "train_batch_id": batch.id,
                "pr_url": batch.pr_url,
                "merged": True,
                "reason": f"merge train batch {batch.id} merged: {batch.pr_url}",
            },
        )
        record_event(
            db,
            sid,
            "status",
            {
                "from": prev,
                "to": StoryStatus.DONE,
                "landing": True,
                "merge_train": True,
                "merged": True,
                "pr_index": batch.pr_index,
                "pr_url": batch.pr_url,
            },
        )
        _publish_story(db, story)
        # the branch is in main now — the worktree has no purpose left.
        # Discarded ONLY here: while the batch is open/red the member keeps
        # its worktree, so a bisect bounce leaves the coder something to
        # rework (workspaces.create rebuilds from main, not the story's own
        # branch, so a discarded worktree would be an empty one).
        try:
            workspaces.discard(project_root, sid)
        except workspaces.WorkspaceError:
            log.debug("discarding landed train member %s failed", sid)


def _split(
    db: Session,
    project: Project,
    batch: TrainBatch,
    failure: str,
    failed_tests: list[str] | None = None,
    logs: str | None = None,
) -> None:
    """Red (twice, or tests failed in CI): bisect the members into two child batches (flushed by the
    tick, one at a time: a project runs a single train CI).

    Every split strictly halves, so the recursion terminates at a 1-member
    batch — which bounces its story to the coder instead of splitting."""
    members = json.loads(batch.story_ids or "[]")
    batch.state = TrainBatchState.RED
    batch.red_runs = batch.red_runs + 1
    if failed_tests is not None:
        batch.failed_tests = json.dumps(failed_tests)
    db.add(batch)
    if len(members) < 2:
        # a 1-member batch is never split: the story goes back to its coder
        story = db.get(Story, members[0]) if members else None
        if story is not None:
            if failed_tests is not None:
                shown = ", ".join(failed_tests[:10]) or (
                    "test ids not in the log tail"
                )
                message = f"land failed: tests failed in CI: {shown}"
                note = {
                    "delivery": "push",
                    "merge_train": True,
                    "train_batch_id": batch.id,
                    "pr_url": batch.pr_url,
                    "ci_failure": failure,
                    "failed_tests": failed_tests,
                    "log_tail": (logs or "")[-INFRA_LOG_TAIL_CHARS:],
                    "reason": "tests failed in CI",
                }
            else:
                message = f"land failed: {failure}"
                note = {
                    "delivery": "push",
                    "merge_train": True,
                    "train_batch_id": batch.id,
                    "pr_url": batch.pr_url,
                    "ci_failure": failure,
                    "reason": (
                        f"merge train batch {batch.id} failed CI twice: {failure}"
                    ),
                }
            _bounce(db, story, message, note)
        db.commit()
        _retire(
            project,
            batch,
            "red twice, bounced" if failed_tests is None else "tests failed, bounced",
        )
        return
    half = (len(members) + 1) // 2
    halves = [members[:half], members[half:]]
    for child_members in (h for h in halves if h):
        child = TrainBatch(
            project_id=project.id,
            state=TrainBatchState.FORMING,
            story_ids=json.dumps(child_members),
            parent_batch_id=batch.id,
            not_before=train_now(),
            infra_retries=batch.infra_retries,
        )
        db.add(child)
        db.flush()
        for sid in child_members:
            story = db.get(Story, sid)
            if story is not None:
                story.train_batch_id = child.id
                db.add(story)
        db.commit()
        for sid in child_members:
            story = db.get(Story, sid)
            if story is not None:
                what = "red twice" if failed_tests is None else "tests failed"
                note = {
                    "delivery": "push",
                    "merge_train": True,
                    "train_batch_id": child.id,
                    "parent_batch_id": batch.id,
                    "reason": (
                        f"merge train bisect: batch {batch.id} {what}, "
                        f"retried as {child.id}"
                    ),
                }
                if failed_tests is not None:
                    note["failed_tests"] = failed_tests
                _record_note(db, sid, note)
        db.commit()
    db.commit()
    _retire(
        project,
        batch,
        "red twice, bisected" if failed_tests is None else "tests failed, bisected",
    )


def _re_run(db: Session, project: Project, batch: TrainBatch, failure: str) -> None:
    """First confirmed red: one flake re-run, then judge the NEW head."""
    token = pr_module.read_token(project.pr_token_file or "")
    # COMMIT the red count BEFORE firing update_branch: a crash in between
    # then resumes with red_runs=1 already stored, so the re-run is never
    # fired twice (idempotent across crashes, per the flake-path contract).
    batch.red_runs = 1
    db.add(batch)
    db.commit()
    try:
        pr_module.update_branch(
            base_url=project.pr_api_base,
            repo=project.pr_repo,
            token=token,
            index=int(batch.pr_index),
        )
    except pr_module.PullRequestError as e:
        log.warning("batch %s re-run failed: %s", batch.id, e)
        # update_branch could not fix the branch (main moved and the merge
        # conflicts): re-flush from current main, keeping the red count
        members = json.loads(batch.story_ids or "[]")
        batch.state = TrainBatchState.RED
        batch.story_ids = json.dumps([])
        db.add(batch)
        db.commit()
        child = TrainBatch(
            project_id=project.id,
            state=TrainBatchState.FORMING,
            story_ids=json.dumps(members),
            parent_batch_id=batch.parent_batch_id,
        )
        db.add(child)
        db.flush()
        for sid in members:
            story = db.get(Story, sid)
            if story is not None:
                story.train_batch_id = child.id
                db.add(story)
        db.commit()
        _flush(db, project, child, red_runs=1)
        _retire(project, batch, "re-flushed from main")
        return
    _refresh_head(project, batch)
    batch.ci_status = "pending"
    batch.created_at = train_now()
    db.add(batch)
    db.commit()
    for sid in json.loads(batch.story_ids or "[]"):
        story = db.get(Story, sid)
        if story is None:
            continue
        _record_note(
            db,
            sid,
            {
                "delivery": "push",
                "merge_train": True,
                "train_batch_id": batch.id,
                "pr_url": batch.pr_url,
                "re_run": True,
                "ci_failure": failure,
                "reason": _RE_RUN_NOTE,
            },
        )
    db.commit()


def _infra_retry(
    db: Session, project: Project, batch: TrainBatch, failure: str, logs: str
) -> None:
    """A CI pod timeout: retry the same members later, no bisect, no red run.

    After INFRA_RETRY_CAP retries the batch goes back to the human gate."""
    members = json.loads(batch.story_ids or "[]")
    if batch.infra_retries >= INFRA_RETRY_CAP:
        batch.state = TrainBatchState.RED
        db.add(batch)
        db.commit()
        message = (
            "land failed: merge train CI pod timeout, "
            f"infra retry cap ({INFRA_RETRY_CAP}) reached"
        )
        for sid in members:
            story = db.get(Story, sid)
            if story is not None and story.train_batch_id == batch.id:
                _bounce(
                    db,
                    story,
                    message,
                    {
                        "delivery": "push",
                        "merge_train": True,
                        "train_batch_id": batch.id,
                        "pr_url": batch.pr_url,
                        "infra_retry_cap": INFRA_RETRY_CAP,
                        "infra_retries": batch.infra_retries,
                        "ci_failure": failure,
                        "log_tail": logs[-INFRA_LOG_TAIL_CHARS:],
                        "reason": (
                            f"CI pod timeout, infra retry cap ({INFRA_RETRY_CAP}) "
                            "reached"
                        ),
                    },
                )
        log.warning(
            "merge train: batch %s hit the infra retry cap, members bounced",
            batch.id,
        )
        _retire(project, batch, "infra retry cap reached")
        return
    delay = min(INFRA_RETRY_BASE_S * (2**batch.infra_retries), INFRA_RETRY_MAX_S)
    not_before = train_now() + timedelta(seconds=delay)
    batch.state = TrainBatchState.RED
    db.add(batch)
    replacement = TrainBatch(
        project_id=project.id,
        state=TrainBatchState.FORMING,
        story_ids=json.dumps(members),
        parent_batch_id=batch.parent_batch_id,
        red_runs=batch.red_runs,
        infra_retries=batch.infra_retries + 1,
        not_before=not_before,
    )
    db.add(replacement)
    db.flush()
    for sid in members:
        story = db.get(Story, sid)
        if story is not None and story.train_batch_id == batch.id:
            story.train_batch_id = replacement.id
            db.add(story)
        _record_note(
            db,
            sid,
            {
                "delivery": "push",
                "merge_train": True,
                "train_batch_id": replacement.id,
                "previous_batch_id": batch.id,
                "infra_retry": replacement.infra_retries,
                "infra_retry_cap": INFRA_RETRY_CAP,
                "next_attempt_at": as_utc_iso(not_before),
                "reason": (
                    f"merge train: CI pod timeout on batch {batch.id}, "
                    f"retrying as {replacement.id} after {delay}s "
                    f"(infra retry {replacement.infra_retries}/{INFRA_RETRY_CAP}), "
                    f"next attempt at {as_utc_iso(not_before)}"
                ),
            },
        )
    db.commit()
    log.warning(
        "merge train: infra timeout on batch %s, retrying as %s in %ss",
        batch.id,
        replacement.id,
        delay,
    )
    _retire(project, batch, "infra timeout")


def _judge(db: Session, project: Project, batch: TrainBatch) -> None:
    """Advance one open batch: merged / red / re-run / wait.

    A MERGED batch is only revisited while a member is still LANDING under
    it (the crash-window resume): once every member is settled the batch is
    inert, and re-reading its PR every tick would be pure Gitea noise.
    """
    if batch.state == TrainBatchState.MERGED:
        pending = False
        for sid in json.loads(batch.story_ids or "[]"):
            story = db.get(Story, sid)
            if (
                story is not None
                and story.train_batch_id == batch.id
                and story.status == StoryStatus.LANDING
            ):
                pending = True
                break
        if not pending:
            return
    if batch.pr_index is None:
        # crash between the push and the PR create: finish the flush
        _flush(db, project, batch, red_runs=batch.red_runs)
        return
    token = pr_module.read_token(project.pr_token_file or "")
    try:
        info = pr_module.get_pull(
            project.pr_api_base, project.pr_repo, token, int(batch.pr_index)
        )
    except pr_module.PullRequestError as e:
        log.warning("batch %s poll failed: %s", batch.id, e)
        return
    head = _refresh_head(project, batch)
    if info.get("merged"):
        db.add(batch)
        db.commit()
        _land_batch(db, project, batch)
        return
    if info.get("state") == "closed":
        # the batch PR was closed by hand: members go back to the gate
        for sid in json.loads(batch.story_ids or "[]"):
            story = db.get(Story, sid)
            if story is not None and story.train_batch_id == batch.id:
                _bounce(
                    db,
                    story,
                    "land failed: batch pull request closed without merging",
                    {
                        "delivery": "push",
                        "merge_train": True,
                        "train_batch_id": batch.id,
                        "pr_url": batch.pr_url,
                        "reason": "batch pull request closed without merging",
                    },
                )
        batch.state = TrainBatchState.RED
        db.add(batch)
        db.commit()
        _retire(project, batch, "closed by hand", close=False)
        return
    if not head:
        return
    try:
        status = pr_module.combined_status(
            project.pr_api_base, project.pr_repo, token, str(head)
        )
    except pr_module.PullRequestError as e:
        log.warning("batch %s status read failed: %s", batch.id, e)
        return
    if status:
        # a failed/None read leaves the stored value alone (outage = pending)
        batch.ci_status = str(status)
    if status != "failure":
        # pending / success / unknown: nothing confirmed, nothing to do
        db.add(batch)
        db.commit()
        return
    try:
        failure = pr_module.status_description(
            project.pr_api_base, project.pr_repo, token, str(head)
        )
    except pr_module.PullRequestError:
        failure = None
    failure = (failure or "").strip() or "CI status failure"
    logs = pr_module.failed_job_logs(
        project.pr_api_base, project.pr_repo, token, str(head)
    )
    if logs is None:
        log.error(
            "merge train: cannot classify CI failure, job logs API unavailable"
        )
    else:
        ids = _pytest_failures(logs)
        if ids is not None:
            # a real red run: pytest already reran with --reruns 2
            _split(db, project, batch, failure, failed_tests=ids, logs=logs)
            return
        if _INFRA_RE.search(f"{failure}\n{logs}"):
            _infra_retry(db, project, batch, failure, logs)
            return
    if batch.red_runs == 0:
        _re_run(db, project, batch, failure)
    else:
        # second red on the stored head: bisect (or bounce a 1-member batch)
        _split(db, project, batch, failure)


def unland(db: Session, project: Project, story: Story, by: str | None) -> None:
    """Operator exit: pull a landing train story back to the human gate."""
    batch = db.get(TrainBatch, story.train_batch_id) if story.train_batch_id else None
    if batch is None or batch.state == TrainBatchState.MERGED:
        raise ValueError("batch is missing or already merged")
    if batch.state == TrainBatchState.FORMING:
        members = [s for s in json.loads(batch.story_ids or "[]") if s != story.id]
        batch.story_ids = json.dumps(members)
        db.add(batch)
        _bounce(
            db,
            story,
            "land cancelled by operator",
            {
                "delivery": "push",
                "merge_train": True,
                "train_batch_id": batch.id,
                "unland": True,
                "by": by,
                "reason": "land cancelled by operator",
            },
        )
        db.commit()
        return
    batch.state = TrainBatchState.RED
    db.add(batch)
    db.commit()
    for sid in json.loads(batch.story_ids or "[]"):
        member = db.get(Story, sid)
        if member is not None and member.train_batch_id == batch.id:
            _bounce(
                db,
                member,
                "land cancelled by operator",
                {
                    "delivery": "push",
                    "merge_train": True,
                    "train_batch_id": batch.id,
                    "pr_url": batch.pr_url,
                    "unland": True,
                    "by": by,
                    "reason": "land cancelled by operator",
                },
            )
    db.commit()
    _retire(project, batch, "unland", close=True)


# ---- the tick (called from the scheduler) ---------------------------------


# A story in any of these statuses is still working its way towards the code
# gate — it will join this very batch as soon as it is approved, so the train
# holds the departure for it (bounded by TRAIN_MAX_HOLD_S, see below).
_PIPELINE_BUSY = (
    StoryStatus.QUEUED,
    StoryStatus.IN_PROGRESS,
    StoryStatus.EXECUTING,
    StoryStatus.AI_REVIEW,
)

# A pipeline that never drains (rate-limited provider, a story parked at a
# gate) must not stall the train forever: past this many seconds AFTER its
# window expired, a batch departs with whatever has joined — busy pipeline
# or not. ONE bound, not a cap-plus-floor pair: departure happens at
# window + TRAIN_MAX_HOLD_S, never later.
TRAIN_MAX_HOLD_S = 900


def _pipeline_busy(db: Session, project: Project) -> bool:
    row = db.exec(
        select(Story.id).where(
            Story.project_id == project.id,
            Story.status.in_(_PIPELINE_BUSY),
        )
    ).first()
    return row is not None


def _last_activity(db: Session, project: Project, batch: TrainBatch) -> datetime | None:
    """The newest timestamp anything happened on this project (a story joined,
    moved status, was created). The train departs only after a full window of
    this being quiet, so a story created a moment after the last member joined
    still makes the same train."""
    from sqlalchemy import func

    last = db.exec(
        select(func.max(Story.updated_at)).where(Story.project_id == project.id)
    ).first()
    stamps = [_as_utc(value) for value in (last, batch.created_at) if value]
    return max(stamps) if stamps else None


def _tick_project(db: Session, project: Project) -> None:
    db.refresh(project)  # an operator pause/resume may have committed since

    def _batches(project_id: str = project.id) -> list[TrainBatch]:
        return list(
            db.exec(
                select(TrainBatch)
                .where(TrainBatch.project_id == project_id)
                .order_by(TrainBatch.created_at)
            ).all()
        )

    # judge first: an OPEN batch that went red or merged frees the
    # project's single CI slot for the forming batches below
    for batch in _batches():
        if batch.state in (TrainBatchState.OPEN, TrainBatchState.MERGED):
            # OPEN: judge the batch PR. MERGED: a crash between the
            # commit that stamps the batch merged and the member loop
            # leaves members in LANDING with train_batch_id set — the
            # poller skips them (they are judged by their batch) and
            # the member loop in _land_batch is idempotent, so simply
            # run it again until the last member is settled.
            _judge(db, project, batch)
    batches = _batches()
    # one train CI per project at a time, tracked locally in the tick
    slot_busy = any(b.state == TrainBatchState.OPEN for b in batches)
    for batch in batches:
        if batch.state != TrainBatchState.FORMING:
            continue
        _drop_departed(db, batch)
        members = json.loads(batch.story_ids or "[]")
        if not members:
            batch.state = TrainBatchState.RED
            db.add(batch)
            db.commit()
            continue
        if slot_busy:
            continue
        if project.train_paused:
            # operator pause: joining and judging go on (a PR merged by hand
            # still lands), only the departure of a forming batch waits
            continue
        if batch.not_before is not None:
            # a replacement batch (bisect child, infra retry) waits
            # only for its backoff and the free slot
            if train_now() < _as_utc(batch.not_before):
                continue
            _flush(db, project, batch, red_runs=batch.red_runs)
            slot_busy = batch.state == TrainBatchState.OPEN
            continue
        started = _as_utc(batch.created_at)
        last = _last_activity(db, project, batch)
        elapsed = (train_now() - started).total_seconds() if started else 0.0
        quiet = (train_now() - last).total_seconds() if last is not None else 0.0
        # hold the departure while a story is still on its way to
        # the code gate (it would join THIS batch) or the project
        # was active inside the window. The hold is BOUNDED: past
        # window + TRAIN_MAX_HOLD_S the batch departs no matter
        # how busy the pipeline is, so a stuck pipeline can never
        # stall the train forever.
        stalled = elapsed >= train_window_s(project) + TRAIN_MAX_HOLD_S
        # test seam: while the flag is set a forming batch NEVER
        # departs — not on the window and not on train_max — so
        # membership at the departure is exactly what the test
        # observed before releasing the flag, never what a timer
        # or a size threshold happened to allow on a loaded node.
        # The stalled bound above still wins, so the flag cannot
        # stall a train forever.
        if TRAIN_HOLD_FORMING and not stalled:
            continue
        if len(members) >= train_max(project) or (
            elapsed >= train_window_s(project)
            and (
                stalled
                or (
                    quiet >= train_window_s(project)
                    and not _pipeline_busy(db, project)
                )
            )
        ):
            _flush(db, project, batch)
            slot_busy = batch.state == TrainBatchState.OPEN


def train_tick(db: Session) -> None:
    """One scheduler pass: form windows, flush due batches, judge open ones."""
    projects = {
        p.id: p
        for p in db.exec(
            select(Project).where(Project.merge_train == True)  # noqa: E712
        ).all()
    }
    for project in projects.values():
        if not train_ready(project):
            continue
        try:
            with _TRAIN_LOCK:
                _tick_project(db, project)
        except Exception:  # noqa: BLE001 — one project never sinks the tick
            log.exception("merge train tick failed for project %s", project.id)


def _batch_dict(
    db: Session,
    batch: TrainBatch,
    ci_contexts: dict[str, str] | None = None,
    with_contexts: bool = False,
) -> dict[str, Any]:
    members = []
    for sid in json.loads(batch.story_ids or "[]"):
        story = db.get(Story, sid)
        members.append(
            {
                "id": sid,
                "title": (story.title if story else None),
                "status": (story.status if story else None),
            }
        )
    created = _as_utc(batch.created_at)
    out: dict[str, Any] = {
        "id": batch.id,
        "state": batch.state,
        "pr_url": batch.pr_url,
        "pr_index": batch.pr_index,
        "head_sha": batch.head_sha,
        "red_runs": batch.red_runs,
        "infra_retries": batch.infra_retries,
        "failed_tests": json.loads(batch.failed_tests or "[]"),
        "ci_status": batch.ci_status,
        "not_before": as_utc_iso(batch.not_before) if batch.not_before else None,
        "next_attempt_at": (
            as_utc_iso(batch.not_before) if batch.not_before else None
        ),
        "parent_batch_id": batch.parent_batch_id,
        "created_at": as_utc_iso(batch.created_at),
        "age_s": (
            max(0, int((train_now() - created).total_seconds())) if created else None
        ),
        "members": members,
    }
    if with_contexts:
        out["ci_contexts"] = ci_contexts
    return out


def _read_contexts(project: Project, batch: TrainBatch) -> dict[str, str] | None:
    """Latest state per CI context on the batch head; None when Gitea cannot
    answer (the view never raises on it)."""
    if not batch.head_sha or not project.pr_api_base or not project.pr_repo:
        return None
    try:
        token = pr_module.read_token(project.pr_token_file or "")
        statuses = pr_module.all_statuses(
            project.pr_api_base, project.pr_repo, token, str(batch.head_sha)
        )
    except pr_module.PullRequestError:
        return None
    return pr_module.latest_context_states(statuses)


def train_report(
    db: Session,
    project_id: str,
    limit: int | None = None,
    contexts: bool = False,
) -> list[dict[str, Any]]:
    """GET /projects/{id}/train: the batches with their member stories.

    `contexts` adds `ci_contexts` to every OPEN batch (one Gitea read each),
    so it is opt-in: the board polls this route every few seconds."""
    query = (
        select(TrainBatch)
        .where(TrainBatch.project_id == project_id)
        .order_by(TrainBatch.created_at.desc())
    )
    if limit is not None:
        query = query.limit(limit)
    batches = db.exec(query).all()
    project = db.get(Project, project_id) if contexts else None
    out: list[dict[str, Any]] = []
    for batch in batches:
        wanted = contexts and batch.state == TrainBatchState.OPEN
        found = _read_contexts(project, batch) if wanted and project else None
        out.append(_batch_dict(db, batch, found, wanted))
    return out


# ---- operator operations ---------------------------------------------------
#
# Pause/resume, rerun, bounce, land-now and force-merge. Each holds the train
# lock, takes `by`, and writes a note carrying `by` on every affected member.


class TrainOpError(Exception):
    """An operator refusal; `status` is the HTTP code the route answers."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _operator_batch(db: Session, project: Project, batch_id: str) -> TrainBatch:
    batch = db.get(TrainBatch, batch_id)
    if batch is None or batch.project_id != project.id:
        raise TrainOpError(404, "batch not found")
    return batch


def _wired(project: Project) -> tuple[str, str]:
    if not train_ready(project):
        raise TrainOpError(409, "project has no push delivery / pr_* wiring")
    return project.pr_api_base, project.pr_repo


def _note_members(
    db: Session,
    batch: TrainBatch,
    note: dict[str, Any],
    members: list[str] | None = None,
) -> None:
    for sid in members if members is not None else json.loads(batch.story_ids or "[]"):
        _record_note(db, sid, {"merge_train": True, "train_batch_id": batch.id, **note})
    db.commit()


def _set_pause(
    db: Session, project: Project, paused: bool, by: str, reason: str | None
) -> None:
    with _TRAIN_LOCK:
        db.refresh(project)
        project.train_paused = paused
        project.train_paused_by = by if paused else None
        project.train_pause_reason = reason if paused else None
        project.train_paused_at = utcnow() if paused else None
        project.updated_at = utcnow()
        db.add(project)
        db.commit()
        batches = db.exec(
            select(TrainBatch).where(
                TrainBatch.project_id == project.id,
                TrainBatch.state.in_(
                    (TrainBatchState.FORMING, TrainBatchState.OPEN)
                ),
            )
        ).all()
        for batch in batches:
            landing = []
            for sid in json.loads(batch.story_ids or "[]"):
                story = db.get(Story, sid)
                if story is not None and story.status == StoryStatus.LANDING:
                    landing.append(sid)
            _note_members(
                db,
                batch,
                {
                    "train_paused" if paused else "train_resumed": True,
                    "by": by,
                    "reason": reason,
                },
                landing,
            )


def pause_train(db: Session, project: Project, by: str, reason: str) -> None:
    _set_pause(db, project, True, by, reason)


def resume_train(db: Session, project: Project, by: str, reason: str | None) -> None:
    _set_pause(db, project, False, by, reason)


def rerun_batch(
    db: Session, project: Project, batch_id: str, by: str
) -> dict[str, Any]:
    """Re-run CI on the batch's current head: same batch, PR and head."""
    with _TRAIN_LOCK:
        base, repo = _wired(project)
        batch = _operator_batch(db, project, batch_id)
        if batch.state != TrainBatchState.OPEN:
            raise TrainOpError(409, f"batch is {batch.state}, only an open batch can rerun")
        try:
            token = pr_module.read_token(project.pr_token_file or "")
            head = _refresh_head(project, batch)
            run_id = (
                pr_module.find_latest_workflow_run(base, repo, token, str(head))
                if head
                else None
            )
            if run_id is None:
                raise TrainOpError(409, f"no workflow run found for head {head}")
            pr_module.rerun_workflow_run(base, repo, token, run_id)
        except pr_module.PullRequestError as e:
            raise TrainOpError(502, f"rerun failed: {e}") from e
        batch.ci_status = "pending"
        db.add(batch)
        db.commit()
        _note_members(
            db,
            batch,
            {
                "rerun": True,
                "by": by,
                "pr_index": batch.pr_index,
                "head_sha": batch.head_sha,
                "run_id": run_id,
            },
        )
        return _batch_dict(db, batch)


def bounce_member(
    db: Session,
    project: Project,
    batch_id: str,
    story_id: str,
    by: str,
    reason: str | None,
) -> dict[str, Any]:
    """Send one member back to the human gate; the rest keep travelling."""
    with _TRAIN_LOCK:
        batch = _operator_batch(db, project, batch_id)
        members = json.loads(batch.story_ids or "[]")
        story = db.get(Story, story_id)
        if story_id not in members or story is None:
            raise TrainOpError(404, "story is not a member of this batch")
        if batch.state not in (TrainBatchState.FORMING, TrainBatchState.OPEN):
            raise TrainOpError(409, f"batch is {batch.state}, nothing to bounce")
        if story.status != StoryStatus.LANDING or story.train_batch_id != batch.id:
            raise TrainOpError(409, f"story is {story.status}, not landing in this batch")
        message = (reason or "").strip() or "bounced by operator"
        note = {
            "delivery": "push",
            "merge_train": True,
            "train_batch_id": batch.id,
            "pr_url": batch.pr_url,
            "bounced": True,
            "by": by,
            "reason": message,
        }
        if batch.state == TrainBatchState.FORMING:
            batch.story_ids = json.dumps([m for m in members if m != story_id])
            db.add(batch)
            _bounce(db, story, f"bounced by operator: {message}", note)
            db.commit()
            return _batch_dict(db, batch)
        _bounce(db, story, f"bounced by operator: {message}", note)
        batch.state = TrainBatchState.RED
        db.add(batch)
        db.commit()
        rest = []
        for sid in members:
            other = db.get(Story, sid)
            if (
                sid != story_id
                and other is not None
                and other.status == StoryStatus.LANDING
                and other.train_batch_id == batch.id
            ):
                rest.append(sid)
        if rest:
            child = TrainBatch(
                project_id=project.id,
                state=TrainBatchState.FORMING,
                story_ids=json.dumps(rest),
                parent_batch_id=batch.id,
                not_before=train_now(),
                infra_retries=batch.infra_retries,
                red_runs=0,
            )
            db.add(child)
            db.flush()
            for sid in rest:
                other = db.get(Story, sid)
                other.train_batch_id = child.id
                db.add(other)
            db.commit()
            _note_members(
                db,
                child,
                {
                    "parent_batch_id": batch.id,
                    "by": by,
                    "reason": (
                        f"member {story_id} bounced by {by}: batch {batch.id} "
                        f"retried as {child.id}"
                    ),
                },
                rest,
            )
        _retire(project, batch, "member bounced")
        return _batch_dict(db, batch)


def _ci_green(project: Project, batch: TrainBatch) -> str:
    """Raise 409 unless the batch head is green; return the rule applied.

    Required checks (branch protection) when readable: each must be present
    and success. Otherwise EVERY context must be success and at least one
    must exist: the fallback is never weaker than the protection rule."""
    base, repo = _wired(project)
    try:
        token = pr_module.read_token(project.pr_token_file or "")
        head = _refresh_head(project, batch)
        if not head:
            raise TrainOpError(409, "batch has no head sha")
        states = pr_module.latest_context_states(
            pr_module.all_statuses(base, repo, token, str(head))
        )
        required = pr_module.required_contexts(
            base, repo, token, project.pr_base_branch or "main"
        )
    except pr_module.PullRequestError as e:
        raise TrainOpError(502, f"cannot read CI status: {e}") from e
    if required:
        rule = "required checks: " + ", ".join(required)
        bad = []
        for pattern in required:
            matched = [c for c in states if fnmatch.fnmatchcase(c, pattern)]
            if not matched:
                bad.append(f"{pattern} (missing)")
            bad.extend(f"{c} ({states[c]})" for c in matched if states[c] != "success")
    else:
        rule = "all contexts green (no readable required checks)"
        bad = [f"{c} ({state})" for c, state in states.items() if state != "success"]
        if not states:
            bad = ["no CI contexts reported"]
    if bad:
        raise TrainOpError(409, f"batch is not green [{rule}]: " + "; ".join(bad))
    return rule


def _merge_batch(
    db: Session,
    project: Project,
    batch_id: str,
    by: str,
    *,
    force_reason: str | None,
) -> dict[str, Any]:
    forced = force_reason is not None
    with _TRAIN_LOCK:
        base, repo = _wired(project)
        batch = _operator_batch(db, project, batch_id)
        if batch.state != TrainBatchState.OPEN or batch.pr_index is None:
            raise TrainOpError(
                409,
                "batch has no PR/CI yet"
                if batch.state == TrainBatchState.FORMING
                else f"batch is {batch.state}, only an open batch can merge",
            )
        rule = _ci_green(project, batch)
        db.add(batch)
        db.commit()
        token = pr_module.read_token(project.pr_token_file or "")
        try:
            if not forced:
                touched = pr_module.pull_files(base, repo, token, int(batch.pr_index))
                if any(name.startswith(".gitea/") for name in touched):
                    raise TrainOpError(
                        409,
                        "batch touches .gitea/: CI changes are not landed here, "
                        "use force-merge with a reason",
                    )
            pr_module.merge_pull(
                base,
                repo,
                token,
                int(batch.pr_index),
                str(batch.head_sha),
                force=forced,
            )
        except pr_module.ProtectedFilesError as e:
            if not forced:
                raise TrainOpError(502, f"merge failed: {e}") from e
            _note_members(
                db,
                batch,
                {
                    "by": by,
                    "reason": force_reason,
                    "force_merge_refused": True,
                    "error": str(e),
                    "pr_index": batch.pr_index,
                    "head_sha": batch.head_sha,
                },
            )
            raise TrainOpError(
                409,
                "the PR token may not merge protected files: give it that right "
                f"or merge by hand ({e})",
            ) from e
        except pr_module.PullRequestError as e:
            raise TrainOpError(502, f"merge failed: {e}") from e
        note: dict[str, Any] = {
            "by": by,
            "pr_url": batch.pr_url,
            "pr_index": batch.pr_index,
            "head_sha": batch.head_sha,
            "green_rule": rule,
        }
        if forced:
            note.update({"force_merge": True, "reason": force_reason})
        else:
            note["land_now"] = True
        _note_members(db, batch, note)
        return _batch_dict(db, batch)


def land_batch_now(
    db: Session, project: Project, batch_id: str, by: str
) -> dict[str, Any]:
    """Merge a green OPEN batch PR now; the next tick's _judge lands members."""
    return _merge_batch(db, project, batch_id, by, force_reason=None)


def force_merge_batch(
    db: Session, project: Project, batch_id: str, by: str, reason: str
) -> dict[str, Any]:
    """Like land-now but skips the .gitea/ path refusal. Operator only: no
    code path in the pipeline calls this."""
    return _merge_batch(db, project, batch_id, by, force_reason=reason)
