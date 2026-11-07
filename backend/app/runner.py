"""Server-side story scheduler and the spec + AI-review pipeline.

The runner owns queued -> in_progress -> ai_review -> human_review (or back
to queued when the AI reviewer rejects). Humans own queue/approve/reject; done
is terminal. The scheduler lives in the server (never the UI) so the board
works headless — the Auto-Claude lesson.
"""

from __future__ import annotations

import asyncio
import contextlib
import glob
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, or_, select

from . import code_graph, pods, procs, review_evidence, workspaces
from . import headless as headless_exec
from .agent import AgentResult, run_agent, strip_placeholders, tool_log_line
from .agent_tools import ToolBox
from .brief import spec_prompt_block
from .config import get_settings
from .db import session_scope
from .events import bus
from .guardrails import GuardedExecutor, ToolPolicy, redact_secrets
from .models import (
    Project,
    ProjectDelivery,
    ProviderModel,
    Story,
    StoryEvent,
    StoryExecution,
    StoryGate,
    StoryKind,
    StoryNote,
    StoryPriority,
    StoryStatus,
    effective_execution,
    headless_assignment,
    utcnow,
)
from .providers import Delta, GatewayError, ProviderGateway, StreamEvent
from .providers.claude_code import READ_ONLY_CLI_TOOLS
from .registry import ProviderRegistry, UnknownModelError, limit_reset_zone
from .schemas import as_utc_iso
from .settings_store import DEFAULTS, effective_prompt, load_settings
from .verify_counts import count_mismatch, parse_summaries, trim_output


@dataclass(frozen=True)
class RejectFeedback:
    """One reject reason with its source, so prompts can label it."""

    reason: str
    source: str  # "operator" | "ai"


# most full-suite requests (FUBALA_RUN_TESTS) one headless round may make; a
# further one is treated as a finish (the pipeline's own verify follows)
HEADLESS_MAX_TEST_REQUESTS = 3
# the verify output tail handed back to the resumed session
HEADLESS_TEST_RESULT_CAP = 8 * 1024


class _HeadlessRequeued(Exception):
    """Internal control flow: the headless child hit a rate limit and the
    story was already requeued with backoff. The CODE stage must stop here
    without taking any failure path — the retry will start a fresh round."""


class _HeadlessAborted(_HeadlessRequeued):
    """Internal control flow: the worker pod was lost mid-run and the story
    was already dealt with: failed as infrastructure, or (the first loss only,
    see POD_LOST_REQUEUE_CAP) requeued, or left to the cancel endpoint that
    closed the pod. Caught wherever a requeue is, so the CODE stage stops
    without taking any further path."""


class _ReviewPodGone(Exception):
    """Internal control flow: the code review's worker pod was unavailable a
    second time. The review ends `incomplete`; nothing runs locally."""


@dataclass
class _ReviewTurn:
    """One code-review model session: its text and how it ended."""

    text: str = ""
    timed_out: bool = False
    turn_cap: bool = False


class _FixRoundParked(Exception):
    """Internal control flow: a headless verify fix round crashed and the
    story was already parked in human_review. The caller must not finish it."""


log = logging.getLogger(__name__)

# A worker pod lost mid-run requeues its story this many times (per story,
# for its whole life) before the loss fails it as infrastructure.
POD_LOST_REQUEUE_CAP = 1
# the code review's worker pod unavailable: requeue once, then `incomplete`
REVIEW_POD_REQUEUE_CAP = 1
# short fixed wait of a run bounced by the backend-node fallback cap (a
# capacity wait, not a provider error)
FALLBACK_WAIT_SECONDS = 60

RATE_LIMIT_MARKERS = ("rate_limit", "ratelimit", "rate limit", "1302")

# Minimum spacing between land-poll sweeps against Gitea. The scheduler tick
# (story_poll_interval) can be seconds in dev/tests while a real Gitea would
# rather not be hammered: one sweep per tick, but never more often than this.
# Overridable (tests) via the FUBALA_LAND_POLL_MIN_INTERVAL env var.
LAND_POLL_MIN_INTERVAL = float(
    os.environ.get("FUBALA_LAND_POLL_MIN_INTERVAL", "15.0")
)

# Wall-clock caps for the Gitea/git sweeps that run off the event loop. Their
# sum (480 s) stays under SCHEDULER_LIVE_STALL_GRACE_SECONDS (600 s), so a long
# tick can never trip /health/live. A capped sweep is retried on a later tick.
LAND_SWEEP_CAP = 90.0
RECONCILE_SWEEP_CAP = 90.0
TRAIN_SWEEP_CAP = 300.0

# land_state marker for "mergeable was False, first seen at <timestamp>" —
# a conflict is only declared after TWO consecutive False observations that
# are also far enough apart in wall-clock time, because Gitea computes
# mergeable asynchronously. Counting polls alone is not enough: with a fast
# tick (tests, dev) two polls can land 100ms apart and "two consecutive"
# degenerates into "one". The dwell below is only a FLOOR — in production
# LAND_POLL_MIN_INTERVAL spaces the sweeps 15s apart, so the dwell is
# satisfied by the second poll and the effective confirmation time stays
# "two consecutive polls" as specified. Overridable via the
# FUBALA_LAND_CONFLICT_MIN_DWELL env var (tests).
LAND_CONFLICT_MIN_DWELL = float(
    os.environ.get("FUBALA_LAND_CONFLICT_MIN_DWELL", "2.0")
)
_LAND_CONFLICT = "merge_conflict"

# Upper bound on the scheduler's locked-database backoff. The first locked
# tick waits one poll interval, every further consecutive one doubles up to
# this cap — the wait itself must never approach the stall bounds (/health's
# banner flag, /health/live's dead-loop verdict), or a merely contended DB
# would read as a dead scheduler.
SCHEDULER_LOCK_BACKOFF_CAP = 5.0


def _is_locked_db(exc: BaseException) -> bool:
    """True for a SQLAlchemy-wrapped SQLite 'database is locked' error — the
    contention signature the 26 Oct incident died on. The string test is on
    the rendered message because the driver's original error is nested."""
    return isinstance(exc, OperationalError) and "database is locked" in str(exc)

# ---- land race: main moved after a green suite --------------------------------
#
# The land job merges main into the PR branch, runs the suite on the merge
# result and only then merges via the API. When main moves between the green
# suite and the merge, Gitea refuses the merge and the land context fails on
# a sha that ALSO carries an earlier green test context. That pair — a failed
# land context plus a success whose description carries the marker below —
# is the race signature. It is invisible in the combined status (worst state
# wins) and in latest-per-context views: only the FULL status list carries
# the marker, which is why the recovery reads all_statuses, not
# combined_status.
LAND_RACE_MARKER = "suite green on the merge result"
LAND_CONTEXT_SUFFIX = "/ land (pull_request)"
# At most this many automatic branch updates per story; after that the story
# parks at the human gate exactly as a non-race failure does. A human action
# (approve/retry-land) legitimately clears the counter.
LAND_RACE_MAX_ATTEMPTS = 3
_LAND_RACE = "land_race"

# ---- land green but unmerged: the merge was refused and the job went green --
#
# The land job posts its race-marker success status on the tested sha BEFORE
# the merge POST, so a refused merge (405/409) still leaves a sha whose
# combined status is exactly "success" while the PR stays open. The runner
# cannot see the failed job from the statuses alone, so it polls the PR
# itself: open + merged false + combined status "success" (the literal only --
# None/pending/failure never qualify, a story without CI wiring would
# otherwise re-land forever) after the dwell below means the story re-lands
# itself via update_branch, sharing the land-race attempt budget. A CI PR
# (any changed path under .gitea/) is green-and-unmerged ON PURPOSE -- the
# land job exits 0 "waiting for coordinator" and a human merges it -- so
# those never re-land; they get one explanatory note instead.
LAND_GREEN_UNMERGED_MIN_DWELL = float(
    os.environ.get("FUBALA_LAND_GREEN_UNMERGED_MIN_DWELL", "1200.0")
)
_LAND_GREEN_UNMERGED = "land_green"
_CI_PR_NOTE = "CI PR green, waiting for the coordinator's force-merge"


def _land_stamp_since(marker: str, land_state: str | None) -> datetime | None:
    """Parse a "<marker>:<ISO-8601 UTC>" dwell stamp out of land_state.

    Shared by _land_conflict_since and _land_green_unmerged_since. A bare or
    unparsable marker reads as None, restarting the dwell clock — a stamp can
    never manufacture a verdict on its own."""
    if not land_state or not land_state.startswith(marker):
        return None
    stamp = land_state[len(marker) :].lstrip(":")
    try:
        seen = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=UTC)  # SQLite round trips lose tzinfo
    return seen


def _land_race_attempts(land_state: str | None) -> int:
    """How many automatic land-race updates this story has already had.

    land_state holds "<_LAND_RACE>:<n>" (all state lives on the row so a
    restart resumes from the DB). Mirrors _land_conflict_since: a bare or
    unparsable marker reads as 0, never as a verdict on its own."""
    if not land_state or not land_state.startswith(_LAND_RACE):
        return 0
    # "<marker>:<n>" (legacy) or "<marker>:<n>:<stale sha>:<ISO>"
    raw = land_state[len(_LAND_RACE) :].lstrip(":").split(":", 1)[0]
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def _land_race_stamp(attempts: int, stale_sha: str, now: datetime) -> str:
    return f"{_LAND_RACE}:{attempts}:{stale_sha}:{as_utc_iso(now)}"


def _land_race_stale(land_state: str | None) -> tuple[str, datetime] | None:
    """The head sha the last branch update replaced, and when the update was
    made, or None for a legacy/absent stamp. A failed status on that sha is
    from before the update and never decides a re-land."""
    if not land_state or not land_state.startswith(_LAND_RACE):
        return None
    parts = land_state[len(_LAND_RACE) :].lstrip(":").split(":", 2)
    if len(parts) < 3 or not parts[1]:
        return None
    try:
        since = datetime.fromisoformat(parts[2])
    except ValueError:
        return None
    if since.tzinfo is None:
        since = since.replace(tzinfo=UTC)
    return parts[1], since


def _land_conflict_since(land_state: str | None) -> datetime | None:
    """When the current run of consecutive `mergeable: false` observations
    started, or None when land_state carries no conflict stamp.

    land_state holds "<_LAND_CONFLICT>:<ISO-8601 UTC timestamp>" (all state
    lives on the row so a restart resumes from the DB). A bare legacy marker
    without a timestamp reads as None, restarting the dwell clock — it can
    never manufacture a conflict on its own."""
    return _land_stamp_since(_LAND_CONFLICT, land_state)


def _land_green_unmerged_since(land_state: str | None) -> datetime | None:
    """When `green + unmerged` was first observed, or None without the stamp.

    land_state holds "<_LAND_GREEN_UNMERGED>:<ISO-8601 UTC timestamp>",
    mirroring _land_conflict_since so the dwell math is shared. The stamp is
    reset on every non-matching poll (merged/pending/failure/None): a stale
    stamp must never fire a re-land later."""
    if not land_state or not land_state.startswith(_LAND_GREEN_UNMERGED):
        return None
    rest = land_state[len(_LAND_GREEN_UNMERGED) :].lstrip(":")
    # the stamp carries the land-race attempts already spent
    # ("<marker>:<spent>:<ISO>"); the ISO tail is everything after the
    # FIRST colon (ISO timestamps themselves contain colons)
    stamp = rest.split(":", 1)[1] if ":" in rest else rest
    try:
        seen = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=UTC)  # SQLite round trips lose tzinfo
    return seen


def _land_green_unmerged_attempts(land_state: str | None) -> int:
    """Land-race attempts already spent, read from a green-unmerged stamp.

    The stamp keeps them ("<marker>:<spent>:<ISO>") because the re-lands it
    triggers share ONE land-race budget per story. Missing/unparsable is 0."""
    if not land_state or not land_state.startswith(_LAND_GREEN_UNMERGED):
        return 0
    rest = land_state[len(_LAND_GREEN_UNMERGED) :].lstrip(":")
    parts = rest.split(":")
    if len(parts) < 2:
        return 0
    try:
        return max(0, int(parts[0]))
    except ValueError:
        return 0


def _ci_pr_note_exists(db: Session, story_id: str) -> bool:
    """Has the one CI-PR note already been written for this story?

    The .gitea note must be idempotent — exactly one per story no matter how
    many sweeps pass while the coordinator's merge is pending."""
    events = db.exec(
        select(StoryEvent).where(
            StoryEvent.story_id == story_id, StoryEvent.kind == "note"
        )
    ).all()
    for event in events:
        try:
            data = json.loads(event.data or "{}")
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("ci_pr_green"):
            return True
    return False

# Infrastructure failures: the provider/credential layer is broken (missing
# key, auth rejected, endpoint gone). Distinct from TRANSIENT_MARKERS (below):
# these do NOT requeue with backoff — the same model would fail identically
# forever. They fail the story with an "infrastructure: " prefix but KEEP
# story.model: an operator retry at stage=queued re-runs on the same model,
# and the scheduler replaces it only when the registry says it is unusable
# (never with a cheaper skill tier). Matched against the GatewayError string
# only, never against coder output, so the markers stay specific.
INFRA_MARKERS = (
    "missing credentials",
    "authenticationerror",
    "auth_error",
    "401",
    "403",
    "api key",
    "api_base",
    "connection error",
)

# The minimum a generated spec must contain before it may be handed to AI
# review (see prompts/spec_writer.md). Context/Risks may legitimately be thin;
# these three may not. Case-insensitive: models write headings both ways.
REQUIRED_SPEC_SECTIONS = ("## Goal", "## Implementation steps", "## Acceptance checks")


def _missing_spec_section(text: str | None) -> str | None:
    """The first mandatory spec section heading missing from `text`, or None.

    Case-insensitive (models write headings both ways) and substring-based,
    exactly like the section check the spec phase already runs
    (test_deliverable_guard.py pins the helper's behaviour).
    """
    if not text or not text.strip():
        return REQUIRED_SPEC_SECTIONS[0]
    lowered = text.lower()
    for section in REQUIRED_SPEC_SECTIONS:
        if section.lower() not in lowered:
            return section
    return None


def _spec_sections_ok(text: str | None) -> bool:
    """True when the text carries every mandatory spec section."""
    return _missing_spec_section(text) is None


def _deliverable_ok(text: str | None, kind: str) -> bool:
    """The spec phase's deliverable check. Code stories need the mandatory
    sections; an info deliverable (the finished document) or a prep one only
    needs to be non-empty."""
    if kind == StoryKind.CODE:
        return _spec_sections_ok(text)
    return bool(text and text.strip())


def _stamp_brief_version(spec: str, version: int | None) -> str:
    """Make sure a spec written with the project brief records `brief vN`.

    Done in code because a cheap spec writer may drop the instruction. The
    appended line ends with a period: a bare `brief vN` last line would read
    as a truncated spec to the preflight's terminal-line rule."""
    if version is None:
        return spec
    if re.search(rf"\bbrief v{version}\b", spec or "", re.IGNORECASE):
        return spec
    return (spec or "").rstrip() + f"\n\nbrief v{version}.\n"


# ---- deterministic spec preflight (the AI-review gate's local half) -------
#
# Everything below runs before any LLM call: an incomplete, truncated or
# placeholder-only spec must never cost a review round. Kind-aware exactly
# like _deliverable_ok: only code stories owe the mandatory sections.

# characters a complete final line may end with: sentence-final punctuation,
# closing brackets/quotes, and a table row's closing pipe
_TERMINAL_CHARS = tuple(".!?…:;)]}\"'”’|")
_LIST_ITEM_RE = re.compile(r"^(?:[-*+]\s|\d+[.)]\s)")


def _line_is_terminal(raw: str) -> bool:
    """True when a line may legitimately be the last one of a document:
    sentence-final punctuation, a list item/checkbox, or an indented
    continuation (a sub-bullet or a wrapped numbered item)."""
    stripped = raw.strip()
    if not stripped:
        return False
    if raw[:1].isspace():
        return True
    if _LIST_ITEM_RE.match(stripped):
        return True
    return stripped[-1] in _TERMINAL_CHARS


def _truncated_spec(text: str) -> bool:
    """Clear truncation signals only — false rejections burn requeue cycles:
    an odd number of ``` fence lines (an unclosed fence), or a last line that
    is none of the acceptable terminal forms (a bare trailing heading is
    mid-document by definition).

    Backticks are never counted across the whole text: they are only markup
    when they sit on their own fence lines, so an odd number of inline
    backticks is NOT a truncation signal on its own — a lone ` inside a fence
    or in prose with a terminal last line is a complete spec. Odd ∧
    non-terminal is already caught by the terminal-line rule below.
    """
    fence_open = False
    last_line: str | None = None
    for line in text.splitlines():
        if not line.strip():
            continue
        last_line = line
        if line.lstrip().startswith("```"):
            fence_open = not fence_open
    if fence_open:  # odd number of fence lines = an unclosed fence
        return True
    if last_line is None:
        return False
    return not _line_is_terminal(last_line)


def _spec_preflight(spec: str | None, kind: str = StoryKind.CODE) -> str | None:
    """Deterministic gate before the AI spec review. Returns a
    machine-readable rejection reason, or None when the spec may be reviewed.

    Placeholder lines ('[System: …]') are stripped first, so a
    placeholder-only spec is 'empty', not 'missing sections'. Code stories
    additionally owe every mandatory section; info/prep deliverables (a
    document, child stories) do not.
    """
    text = strip_placeholders(spec or "")
    if not text.strip():
        return "spec empty after placeholder stripping"
    if kind == StoryKind.CODE:
        missing = _missing_spec_section(text)
        if missing:
            return f"missing section: {missing.lstrip('#').strip()}"
    if _truncated_spec(text):
        return "spec truncated"
    return None


# ---- acceptance criteria numbering ---------------------------------------

_NUMBERED_AC_RE = re.compile(r"^\s*\d+[.)]\s")
_AC_COVERAGE_RE = re.compile(r"^AC\s+(\d+)\s*:\s*(.*)$", re.IGNORECASE)


def _strip_ac_number(ac: str) -> str:
    """Drop an item's own leading number so renumbering it 1..n never stacks
    ('1. 1. text'). The item's text is what matters to the reviewer."""
    match = _NUMBERED_AC_RE.match(ac)
    return ac[match.end() :].strip() if match else ac


def _split_acceptance(acceptance: str | None) -> list[str]:
    """Split the story's acceptance text into criteria, preserving the
    numbering the reviewer is told to echo back.

    When any line starts with '<n>.' / '<n>)', each such line begins one
    criterion and every following line belongs to it (sub-bullets, wrapped
    text). Without numbered items, each non-empty line is one criterion.
    """
    lines = [ln for ln in (acceptance or "").splitlines() if ln.strip()]
    if not lines:
        return []
    if not any(_NUMBERED_AC_RE.match(ln) for ln in lines):
        return [ln.strip() for ln in lines]
    items: list[str] = []
    for line in lines:
        if _NUMBERED_AC_RE.match(line):
            items.append(line.strip())
        elif items:
            items[-1] += "\n" + line.rstrip()
        else:
            # prose preamble ahead of the first numbered item
            items.append(line.strip())
    return items


def _uncovered_ac(review: str, n: int) -> str | None:
    """The first explicitly uncovered acceptance criterion in a review.

    Only an explicit 'AC <n>: NOT COVERED' line overrides the verdict — a
    reply with no coverage block at all falls back to the verdict line, which
    keeps the spec review fail-open. Returns the rejection reason, or None.
    """
    for raw in review.splitlines():
        match = _AC_COVERAGE_RE.match(raw.strip())
        if not match:
            continue
        number = int(match.group(1))
        if 1 <= number <= n and "not covered" in match.group(2).lower():
            return f"AC {number} not covered by spec"
    return None


_REASON_LINE_RE = re.compile(r"^Reason\s+(\d+)\s*:\s*(.*)$", re.IGNORECASE)


def _code_gate_reasons(db, story_id: str) -> list[str]:
    """Human reject reasons recorded at the CODE gate for this execution
    round, verbatim, deduplicated, in order.

    The cut-off is the newest `spec_approved` marker: approving the spec is
    the moment execution starts, so every operator note after it is an
    objection to reviewed CODE. Notes carrying `gate` (the code-reject
    bookkeeping marker written by POST /reject) have no `reason` key and are
    skipped by construction, as are agent diagnostics (AGENT_AUTHORS) and
    `feedback: False` bookkeeping — same filters as `_reject_feedback`.
    """
    marker = db.exec(
        select(StoryEvent.id)
        .where(StoryEvent.story_id == story_id, StoryEvent.kind == "spec_approved")
        .order_by(StoryEvent.id.desc())
    ).first()
    since = marker or 0
    reasons: list[str] = []
    for event in db.exec(
        select(StoryEvent)
        .where(
            StoryEvent.story_id == story_id,
            StoryEvent.kind == "note",
            StoryEvent.id > since,
        )
        .order_by(StoryEvent.id)
    ).all():
        try:
            data = json.loads(event.data)
        except ValueError:
            continue
        reason = data.get("reason")
        if not reason or data.get("feedback") is False:
            continue
        if data.get("by") in AGENT_AUTHORS:
            continue
        if reason not in reasons:
            reasons.append(reason)
    return reasons


def _coder_feedback(db, story_id: str) -> str | None:
    """The newest rejection reason for the coder's rework prompt: the newest
    note after the last `spec_approved` marker that carries a `reason`, with
    its author. Bookkeeping notes are skipped -- a run's worktree note, the
    code-gate marker (`gate`), `feedback: False` diagnostics -- so they can
    never hide the reason behind them. None when no reason was given since
    the spec was approved (a first run, or a spec-stage gripe)."""
    marker = db.exec(
        select(StoryEvent.id)
        .where(StoryEvent.story_id == story_id, StoryEvent.kind == "spec_approved")
        .order_by(StoryEvent.id.desc())
    ).first()
    for event in db.exec(
        select(StoryEvent)
        .where(
            StoryEvent.story_id == story_id,
            StoryEvent.kind == "note",
            StoryEvent.id > (marker or 0),
        )
        .order_by(StoryEvent.id.desc())
    ).all():
        try:
            data = json.loads(event.data or "{}")
        except ValueError:
            continue
        if not isinstance(data, dict) or data.get("feedback") is False:
            continue
        if data.get("reason"):
            return f"(from {data.get('by', 'reviewer')}): {data['reason']}"
    return None


# a numbered point inside a human reject reason, at a line start OR inline
# ("1. X 2. Y" is two points). Over-splitting prose like "3. the latch" is
# accepted: the split points stay verbatim, only the numbering moves.
_REASON_POINT_SPLIT_RE = re.compile(r"\d+[.)]\s+")

# first-person epistemic hedges that disqualify an otherwise-ADDRESSED
# point: a reviewer who "cannot falsify" or who "assumed" a point has not
# verified it, and doubt beats ADDRESSED (and outranks file:line evidence —
# a citation cannot make an unverified claim verified). Plain descriptions
# ("no evidence", "assumption", "unclear") are NOT doubt: they describe as
# often as they hedge, and an ADDRESSED answer without a citation now
# rejects on the missing file:line evidence instead. Substring match,
# case-insensitive; this list is the authoritative doubt definition.
_DOUBT_MARKERS = (
    "cannot falsify",
    "cannot verify",
    "can't verify",
    "assumed",
    "not verified",
)

# consecutive AI code-review rejections before the story parks at the human
# gate instead of requeueing — the coder/reviewer pair must not ping-pong
# forever over the same unanswered point. Fixed per story, like
# `spec_reject_limit`'s loop bound but hardcoded: a backstop, not a tunable.
CODE_REVIEW_REJECT_LIMIT = 2


def _reason_points(reasons: list[str]) -> list[str]:
    """Split human reject reasons into individually checkable points.

    One human objection can carry several numbered points, written across
    lines ("1. X\\n2. Y") or inline in a single line ("1. X 2. Y"). Every
    numbered marker starts a new point wherever it sits; a reason with no
    numbered marker at all is one point; non-numbered continuation lines
    append to the point above them (wrapped text, sub-bullets). Each point is
    stripped of its own leading number so the renumbered checklist never
    stacks ('1. 1. X')."""
    points: list[str] = []

    def _push(text: str) -> None:
        text = text.strip()
        if text:
            points.append(_strip_ac_number(text))

    for reason in reasons:
        for line in (reason or "").splitlines():
            segments = _REASON_POINT_SPLIT_RE.split(line)
            head = segments[0]
            if head.strip():
                # prose ahead of the first marker on this line: a
                # continuation of the point above, or the reason's own
                # opening when no point exists yet
                if points:
                    points[-1] += "\n" + head.rstrip()
                else:
                    _push(head)
            for segment in segments[1:]:
                _push(segment)
    return points


def _point_head(point: str) -> str:
    """The first line of a point, capped, so a rejection note stays readable
    while still naming the point it is about."""
    first = next((ln.strip() for ln in point.splitlines() if ln.strip()), "")
    return first[:160]


def _reject_reasons_section(reasons: list[str], points: list[str]) -> str:
    """The '## Human reject reasons to verify' block for the code-review
    prompt.

    Two renderings of the same objections: the reasons exactly as the human
    wrote them (quoted, numbered — verbatim containment stays checkable),
    then the split checklist the reviewer must answer point by point. One
    reason can carry several numbered points; the checklist renumbers them
    1..n and demands per-point evidence, because an approving verdict over a
    silently dropped point is exactly the rubber stamp this block exists to
    prevent.

    Every point is judged against the WHOLE diff the prompt carries, never
    against a slice of it: a fix that lives in any part of the diff is
    ADDRESSED with that part's file:line, and "not in this chunk" is not a
    verdict (incident 50a9f97c: a whole-diff fix marked NOT ADDRESSED
    because it sat in another chunk). A point that only frames the others —
    no change of its own to look for — is answered 'framing — see reasons
    n-m' and needs no citation."""
    return "\n".join(
        [
            "## Human reject reasons to verify",
            "A human rejected the previous version of this code; the coder "
            "claims to have addressed their reasons. Verify each one against "
            "the WHOLE diff you are shown — every point separately, never "
            "as a group, and never against one part of it only.",
            "The human's reasons, verbatim:",
            *[f"> {i}. {reason}" for i, reason in enumerate(reasons, 1)],
            "Points to check, one by one:",
            *[f"{i}. {point}" for i, point in enumerate(points, 1)],
            "For each point above, end your reply with one line "
            "'Reason N: ADDRESSED — <file>:<line>' or "
            "'Reason N: NOT ADDRESSED — why'. "
            "Judge each point against the whole diff: a change that lives "
            "in ANY part of it is ADDRESSED, cited with that part's "
            "<file>:<line>. Say NOT ADDRESSED only when NO part of the "
            "whole diff contains the change — 'not in this chunk' is never "
            "a verdict. "
            "An ADDRESSED line without a <file>:<line> citation counts as "
            "NOT ADDRESSED — the citation is what makes the answer "
            "checkable. "
            "If you cannot point at a file and line, or cannot verify a "
            "point, it is NOT ADDRESSED. "
            "A point that only frames the other points (no change of its "
            "own to look for) is answered 'Reason N: framing — see reasons "
            "n-m' and needs no file:line. "
            "Any missing or NOT ADDRESSED point means REJECTED.",
        ]
    )


# StoryNote authors that are the pipeline's own bookkeeping, never the
# operator's requirements: fubala's run summaries and artefact-guard notes.
# Kept SEPARATE from AGENT_AUTHORS on purpose — the operator-note filter
# must exclude the union (AGENT_AUTHORS ∪ fubala), so narrowing
# AGENT_AUTHORS for some other filter can never leak pipeline notes into
# the code-review prompt as operator requirements.
_PIPELINE_NOTE_AUTHORS = frozenset({"fubala"})


def _is_pipeline_note_author(author: str | None) -> bool:
    """True when a note's author speaks for the pipeline, not the operator.

    AGENT_AUTHORS (defined further down, with the lease constants) covers
    every agent author; _PIPELINE_NOTE_AUTHORS adds fubala's work-log notes
    explicitly so the union stays correct even if AGENT_AUTHORS changes.
    """
    return author in AGENT_AUTHORS or author in _PIPELINE_NOTE_AUTHORS


def _operator_notes_since(db, story_id: str) -> list[tuple[str, str, str]]:
    """Operator work-log notes newer than the newest `spec_approved` marker:
    what the operator asked for since execution started, with the author
    labelled. Pipeline authors (AGENT_AUTHORS ∪ fubala) never qualify —
    their notes are diagnostics, not requirements.

    StoryNote rows carry no event id, so the cut-off compares timestamps,
    UTC-normalized on both sides: SQLite round-trips can drop tzinfo and a
    naive/aware mix would raise instead of comparing.

    Returns `(created_at_iso, author, body)` tuples, oldest first — callers
    label each entry themselves (the code-review block keeps the plain
    `(from author):` label, the coder block adds the creation time)."""
    marker = db.exec(
        select(StoryEvent.created_at)
        .where(StoryEvent.story_id == story_id, StoryEvent.kind == "spec_approved")
        .order_by(StoryEvent.id.desc())
    ).first()
    since = _as_utc(marker)
    notes: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for note in db.exec(
        select(StoryNote)
        .where(StoryNote.story_id == story_id)
        .order_by(StoryNote.id)
    ).all():
        body = (note.body or "").strip()
        if not body or _is_pipeline_note_author(note.author):
            continue
        # the cutoff first, dedup second: an older duplicate must never
        # swallow a verbatim repost written after the newest spec approval
        if since is not None:
            at = _as_utc(note.created_at)
            if at is not None and at < since:
                continue
        key = (note.author or "", body)
        if key in seen:
            continue
        seen.add(key)
        notes.append((as_utc_iso(note.created_at), note.author or "", body))
    return notes


def _binding_feedback_notes(db, story_id: str) -> list[tuple[int, str, str, str]]:
    """Feedback-marked notes (`as_feedback`) newer than the newest `spec` event:
    decisions the spec writer never saw. The cutoff is the current spec
    revision, not the last coder round, so a rework round keeps them.

    Returns `(note_id, created_at_iso, author, body)`, oldest first, uncapped."""
    spec_id = db.exec(
        select(StoryEvent.id)
        .where(StoryEvent.story_id == story_id, StoryEvent.kind == "spec")
        .order_by(StoryEvent.id.desc())
    ).first()
    notes: list[tuple[int, str, str, str]] = []
    for event in db.exec(
        select(StoryEvent)
        .where(
            StoryEvent.story_id == story_id,
            StoryEvent.kind == "note",
            StoryEvent.id > (spec_id or 0),
        )
        .order_by(StoryEvent.id)
    ).all():
        try:
            data = json.loads(event.data or "{}")
        except ValueError:
            continue
        if not isinstance(data, dict) or not data.get("as_feedback"):
            continue
        note_id = data.get("note_id")
        body = str(data.get("reason") or "").strip()
        if not isinstance(note_id, int) or not body:
            continue
        notes.append((note_id, as_utc_iso(event.created_at), str(data.get("by") or ""), body))
    return notes


def _binding_notes_section(notes: list[tuple[int, str, str, str]]) -> str:
    """The '## Coordinator decisions (binding)' block: every feedback note the
    spec writer never saw, author- and time-labelled, body verbatim."""
    return "\n".join(
        [
            "## Coordinator decisions (binding)",
            "The coordinator decided these after the spec was written; they "
            "override the spec where they differ:",
            *[f"- (from {author}, {at}): {body}" for _, at, author, body in notes],
        ]
    )


def _without_binding(
    operator_notes: list[tuple[str, str, str]],
    binding: list[tuple[int, str, str, str]],
) -> list[tuple[str, str, str]]:
    """Operator notes minus those already rendered in the binding section."""
    keys = {(author, body) for _, _, author, body in binding}
    return [n for n in operator_notes if (n[1], n[2]) not in keys]


def _operator_notes_section(notes: list[tuple[str, str, str]]) -> str:
    """The sibling '## Operator notes since spec approval' block for the
    code-review prompt: the notes verbatim (author-labelled), so the reviewer
    checks them like spec requirements."""
    return "\n".join(
        [
            "## Operator notes since spec approval",
            "The operator added these notes after the spec was approved; "
            "they carry the same weight as the spec:",
            *[f"- (from {author}): {body}" for _, author, body in notes],
        ]
    )


_CODER_NOTES_CAP = 8000


def _coder_operator_notes_section(notes: list[tuple[str, str, str]]) -> str:
    """The coder prompt's 'Operator notes since the spec approval' block:
    one note per entry, oldest first, each labelled with author and creation
    time, body verbatim. The notes are the operator's steering written after
    the spec was approved — the coder must follow them like the spec.

    Capped at `_CODER_NOTES_CAP` characters (the whole section, header and
    omission marker included), keeping the NEWEST notes: the latest steering
    wins. When any note was dropped, a one-line marker says so at the top of
    the section — and the marker's own cost is reserved in the budget up
    front, so a section that pays for a marker still fits the cap."""
    header = (
        "## Operator notes since the spec approval (follow them)\n"
        "The operator added these after the spec was approved; they steer "
        "this implementation like the spec does:"
    )
    marker = "(older notes omitted)"
    # the marker renders whenever anything is dropped, so its cost (line +
    # newline) is part of the section from the first cut on: reserving it
    # up front keeps len(section) <= cap in every branch
    used = len(header) + 1 + len(marker) + 1
    kept: list[str] = []
    dropped = False
    # walk newest -> oldest so the cap keeps the newest side; the first note
    # that does not fit cuts EVERYTHING older too — keep-the-newest is
    # all-or-nothing per tail, not a greedy per-note skip that could sneak an
    # older, smaller note in under a newer one that was dropped
    for iso, author, body in reversed(notes):
        entry = f"- [{iso}] (from {author}): {body}"
        cost = len(entry) + 1  # + newline (header/entry separators alike)
        if used + cost > _CODER_NOTES_CAP:
            dropped = True
            break
        used += cost
        kept.append(entry)
    kept.reverse()
    lines = [header]
    if dropped:
        lines.append(marker)
    lines.extend(kept)
    return "\n".join(lines)


# file:line evidence on a 'Reason N:' line. Searched across the whole text
# after 'Reason N:', so the em-dash vs hyphen separator before the citation
# never matters. The citation must LOOK like a path: a name with an
# extension before the colon ("app.py:412", "src/ems/app.py:412") or a known
# extensionless file ("Dockerfile:12"). A bare "12:45" (clock time) or
# "3:1" (ratio) is not evidence — reviewers mention quarter times and
# proportions constantly, and counting them as citations would let an
# unevidenced answer pass the gate.
_EVIDENCE_RE = re.compile(
    r"(?:[A-Za-z0-9_][A-Za-z0-9_./-]*\.[A-Za-z0-9]+|Dockerfile|Makefile):\d+",
    re.IGNORECASE,
)


def _unaddressed_reason(review: str, points: list[str]) -> str | None:
    """The first point of the human's rejection a code review leaves
    unanswered, declares NOT ADDRESSED, hedges, or claims ADDRESSED
    without file:line evidence.

    A point is unaddressed when its 'Reason N:' line is missing (the
    reviewer dropped the point), says NOT ADDRESSED, carries a doubt
    marker ('cannot falsify', 'assumed', ... — doubt beats ADDRESSED and
    outranks evidence: a citation cannot make an unverified claim
    verified), or says ADDRESSED without citing at least one <file>:<line>
    — the citation the prompt contract demands is what makes an answer
    checkable, so a bare 'ADDRESSED' is treated as NOT ADDRESSED.

    A 'Reason N: framing — ...' answer passes without evidence: a framing
    point has no change of its own to cite, so demanding a file:line would
    reject every honest answer (checked before the doubt markers, because
    framing prose can legitimately contain words like 'assumed').

    A reply that engages the checklist at all must answer every point. A
    reply with no per-reason lines whatsoever falls back to the verdict
    line (fail-open parity with the spec review's `_uncovered_ac`), so a
    reviewer who never saw the contract cannot be second-guessed word by
    word — the loop guard (`CODE_REVIEW_REJECT_LIMIT`) bounds the rounds
    instead. Returns the rejection reason, or None.
    """
    answers: dict[int, str] = {}
    for raw in review.splitlines():
        match = _REASON_LINE_RE.match(raw.strip())
        if not match:
            continue
        number = int(match.group(1))
        if 1 <= number <= len(points):
            answers[number] = match.group(2)
    if not answers:
        return None
    for number, point in enumerate(points, 1):
        head = _point_head(point)
        rest = answers.get(number)
        if rest is None:
            return (
                f"Reason {number} not answered by the code review "
                f"— point: {head}"
            )
        lowered = rest.lower()
        if lowered.startswith("framing"):
            # a framing point answers where its substance lives — no
            # change of its own exists to cite, so no evidence is required
            continue
        if "not addressed" in lowered:
            why = lowered.split("not addressed", 1)[1].lstrip(" —-:").strip()
            return (
                f"Reason {number} NOT ADDRESSED — {why} (point: {head})"
                if why
                else f"Reason {number} NOT ADDRESSED by the code review — {head}"
            )
        doubt = next((m for m in _DOUBT_MARKERS if m in lowered), None)
        if doubt is not None:
            return (
                f"Reason {number} answered with '{doubt}', which is not a "
                f"verification — point: {head}"
            )
        if not _EVIDENCE_RE.search(rest):
            return (
                f"Reason {number} claims ADDRESSED without file:line "
                f"evidence — point: {head}"
            )
    return None


def _code_review_rejects(db, story_id: str) -> int:
    """Consecutive AI code-review rejections the human has not yet answered:
    note events the code-review reject path flags `code_review_reject`, newer
    than the newer of the last `spec_approved` marker or the last
    `gate: code` note. Either of those is a human touch — approving a spec or
    objecting to code restarts the conversation — so the counter only
    measures coder/reviewer rounds nobody has looked at yet. Other
    `code-reviewer` notes (a no-change requeue diagnostic, a red verify, a
    missing verdict) are not rejections: they park or take their own path and
    must not spend this guard's budget."""
    events = db.exec(
        select(StoryEvent)
        .where(StoryEvent.story_id == story_id, StoryEvent.kind == "note")
        .order_by(StoryEvent.id)
    ).all()
    cutoff = (
        db.exec(
            select(StoryEvent.id)
            .where(
                StoryEvent.story_id == story_id, StoryEvent.kind == "spec_approved"
            )
            .order_by(StoryEvent.id.desc())
        ).first()
        or 0
    )
    for event in events:
        try:
            data = json.loads(event.data or "{}")
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("gate") == "code":
            cutoff = max(cutoff, event.id or 0)
    rejects = 0
    for event in events:
        if (event.id or 0) <= cutoff:
            continue
        try:
            data = json.loads(event.data or "{}")
        except ValueError:
            continue
        if (
            isinstance(data, dict)
            and data.get("code_review_reject") is True
            and data.get("by") == "code-reviewer"
        ):
            rejects += 1
    return rejects


def _phase_budget(settings: dict, key: str) -> int | None:
    """Per-round output budget for a story phase: the phase setting when set,
    else the gateway's own round budget (Settings.max_output_tokens). An unset
    phase setting must never LOWER the budget the gateway already sends."""
    return int(settings.get(key) or 0) or get_settings().max_output_tokens

# Transport-level failures from the gateway: same patient retry policy as rate
# limits, since they are just as likely to clear on their own. Markers are
# lowercase substrings against the flattened error string — `LiteLLMGateway`
# turns every litellm exception into "[<provider>] <ExcName>: <msg>", so
# exception names and message phrases are what we get to match on. No bare
# "500" marker: it would also match counts like "1500" in unrelated text.
TRANSIENT_MARKERS = (
    "midstream",
    "apiconnection",
    "connection timed out",
    "connection closed",
    "timeout on reading data",
    "internalservererror",
    "internal server error",
    "serviceunavailableerror",
    "service unavailable",
    "internal network failure",
    "try again later",
)


# How long an external claim may sit with zero activity before the scheduler
# reclaims it. Fixed per story, not a setting: this is a leak-prevention
# backstop, not a tunable someone will want to fiddle with per deployment.
LEASE_SILENCE = timedelta(hours=2)
LEASE_EXPIRED_NOTE = "lease expired: no activity for 2h, auto-released"
LEASE_RELEASED_BY = "scheduler"
# note authors that are the pipeline itself, never the operator: their
# "reason" notes are diagnostics, not reject feedback for the spec writer
# ("ai-reviewer" is handled separately as the AI source)
AGENT_AUTHORS = frozenset({"code-reviewer", "vendored-guard", LEASE_RELEASED_BY, "fubala"})


def _as_utc(dt: datetime | None) -> datetime | None:
    """Normalize a stored datetime to aware UTC so subtraction can't blow up
    on naive-vs-aware mixes (SQLite round-trips can drop tzinfo)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def release_claim(db: Session, story: Story, *, by: str) -> dict[str, Any]:
    """Drop an external story's claim and put it back to queued — shared
    internals for the manual release endpoint and the scheduler's stale-lease
    sweep. Records the same `status` event shape for both callers.

    Caller contract: pass a fresh Story from the open session (do NOT reuse a
    detached instance); no commit happens here, and nothing is published —
    commit first, publish after (phantom-status bug, see
    routers/stories._commit_and_publish)."""
    prev = story.status
    released = story.claimed_by
    story.status = StoryStatus.QUEUED
    story.claimed_by = None
    story.claimed_at = None
    story.queued_at = utcnow()
    story.updated_at = utcnow()
    db.add(story)
    record_event(
        db,
        story.id,
        "status",
        {"from": prev, "to": story.status, "released": released, "by": by},
    )
    return story_payload(story)


# ---- slot fairness ------------------------------------------------------
#
# Queued stories are not picked in bare updated_at order: a cheap story that
# keeps arriving would starve an expensive one forever. Ranking is
# cost-aware (cheap first) but bounded — anything waiting >= STARVATION_S
# moves to the head of the line regardless of tier.

STARVATION_S = 900  # 15 minutes: max time any queued story waits behind cheaper work

# cheaper tiers win the slot race; unknown/NULL tiers are treated as medium
SKILL_COST_RANK = {"low": 0, "medium": 1, "high": 2}


def fair_order_key(story: Story, now: datetime) -> tuple[int, int, int, float, str]:
    """Sort key for queued stories: the scheduler picks the smallest key.

    The first element is the priority rank: urgent stories (0) go ahead of
    every normal one (1; NULL counts as normal), first-in first-out by wait
    then id, with the skill tier ignored. Urgent deliberately outranks the
    starvation bound, so a steady stream of urgent stories can starve normal
    work. Among normal stories: rank 0 = starved (waited >= STARVATION_S),
    rank 1 = fresh; within a rank the cheaper skill tier goes first, then the
    longer wait, then story id so the order is deterministic. Pure: no DB, no
    clock reads — callers pass `now`, which makes the key directly unit-testable and lets the API
    annotate positions with exactly the order `_tick` would pick up in.
    Legacy rows with NULL queued_at fall back to updated_at."""
    entered = _as_utc(story.queued_at or story.updated_at) or story.updated_at
    wait_s = max((now - entered).total_seconds(), 0.0)
    if story.priority == StoryPriority.URGENT:
        return (0, 0, 0, -wait_s, story.id)
    starved = 0 if wait_s >= STARVATION_S else 1
    cost = SKILL_COST_RANK.get(story.skill or "medium", 1)
    return (1, starved, cost, -wait_s, story.id)


def deps_met(db: Session, story: Story) -> bool:
    """Every dependency exists and is done (module level so the API layer
    computes eligibility with the exact gate the scheduler uses)."""
    for dep_id in json.loads(story.depends_on or "[]"):
        dep = db.get(Story, dep_id)
        if dep is None or dep.status != StoryStatus.DONE:
            return False
    return True


def queued_pickup_order(db: Session, *, now: datetime | None = None) -> list[Story]:
    """Queued internal stories with dependencies met, in pickup order.

    Shared by `_tick` (which sorts its queued candidates with the same
    `fair_order_key`) and the stories API (queue_position), so the position
    the board shows is the position the scheduler honours.

    Known imprecision: the runner's in-memory `_backoff` map and `_running`
    set are invisible here, so a story under rate-limit backoff (or one whose
    task is winding down) may hold a position it cannot take this instant.
    The board refreshes on the next list fetch; the error is transient."""
    now = now or utcnow()
    stories = db.exec(
        select(Story).where(
            Story.status == StoryStatus.QUEUED,
            or_(
                Story.execution.is_(None),
                Story.execution == StoryExecution.INTERNAL,
            ),
        )
    ).all()
    ready = [s for s in stories if deps_met(db, s)]
    return sorted(ready, key=lambda s: fair_order_key(s, now))


# Watchdog: seconds of stream silence (time.monotonic-based) after which an
# in-flight run is considered hung, cancelled and requeued. Module-level so
# tests can monkeypatch it.
WATCHDOG_TIMEOUT = 15 * 60

# Grace added on top of a bounded phase's own timeout when the watchdog is
# floored for a run inside that phase (verify subprocess, guarded tool call,
# headless CODE run — all silent by nature, all carrying their own
# wall-clock bound): the phase gets its full bound plus this margin to
# resolve itself before the silence clock may cancel the run again. The
# 27 Oct incident: prod verifies at 850-900s against a 900s watchdog killed
# ~60 healthy coder rounds. Module-level so tests can monkeypatch it.
WATCHDOG_PHASE_MARGIN = 60.0

# Provider usage-limit holds: a headless CODE run that requeues on a 429
# usage-limit answer also holds the PROVIDER (not just the story) until the
# stated reset time, so sibling stories on the same provider wait instead of
# each burning a run into the same wall. No plausible reset was parsed (or a
# nonsensical one: past, >24h out) -> the fallback; a stated reset is never
# trusted past the provider's quota window cap (a bogus far-future timestamp
# must not park the board for a day). Module-level so tests can monkeypatch.
PROVIDER_HOLD_FALLBACK_SECONDS = 900
PROVIDER_HOLD_MAX_SECONDS = 5 * 3600

# bus kinds that count as "the run is still alive"
WATCHDOG_ACTIVITY_KINDS = ("story_log", "story_progress")


_BARE_429 = re.compile(r"\b429\b")
RATE_LIMIT_CODES = ("1302", "rate_limit_error", "rate_limit_exceeded")
CONTEXT_MARKERS = (
    "contextwindowexceedederror",
    "context_length_exceeded",
    "prompt is too long",
    "maximum context length",
)
TIMEOUT_MARKERS = (
    "timeout",
    "timed out",
    "apiconnection",
    "connection closed",
    "midstream",
)
PROVIDER_MESSAGE_MAX_CHARS = 500


def _is_rate_limit_error(error: str) -> bool:
    lowered = (error or "").lower()
    return any(marker in lowered for marker in RATE_LIMIT_MARKERS) or bool(
        _BARE_429.search(lowered)
    )


def _is_infra_error(error: str) -> bool:
    """True when the provider/credential layer is broken, not the story.

    Rate limits are checked first by callers (and never appear here): a 429
    with an auth flavour is a quota problem, which requeues with backoff."""
    lowered = (error or "").lower()
    return any(marker in lowered for marker in INFRA_MARKERS)


def _is_transient_error(error: str) -> bool:
    """Rate limits AND dropped/timed-out connections both requeue with backoff."""
    lowered = (error or "").lower()
    return _is_rate_limit_error(error) or any(
        marker in lowered for marker in TRANSIENT_MARKERS
    )


def _has_provider_facts(error: object) -> bool:
    return any(
        getattr(error, attr, None) is not None
        for attr in ("http_status", "error_type", "provider_code")
    )


def classify_provider_error(error: object) -> str:
    """One of rate_limit | server_error | timeout | context_too_large | infra |
    hard. Facts-first: a GatewayError carrying http_status / error_type /
    provider_code is classified on those (a bare "rate limit" in a 500's prose
    is NOT a rate limit). Substring markers are the fallback for errors
    without facts (echo/fake gateways, string-only errors)."""
    text = str(error or "")
    lowered = text.lower()
    if not _has_provider_facts(error):
        if _is_rate_limit_error(text):
            return "rate_limit"
        if any(m in lowered for m in CONTEXT_MARKERS):
            return "context_too_large"
        if _is_infra_error(text):
            return "infra"
        if any(m in lowered for m in TRANSIENT_MARKERS):
            return "timeout" if any(m in lowered for m in TIMEOUT_MARKERS) else "server_error"
        return "hard"
    status = getattr(error, "http_status", None)
    code = (getattr(error, "provider_code", None) or "").lower()
    etype = (getattr(error, "error_type", None) or "").lower()
    if status == 429 or code in RATE_LIMIT_CODES or etype == "ratelimiterror":
        return "rate_limit"
    if (
        (isinstance(status, int) and 500 <= status < 600)
        or etype in ("internalservererror", "serviceunavailableerror")
        or (status is None and any(m in lowered for m in TRANSIENT_MARKERS)
            and not any(m in lowered for m in TIMEOUT_MARKERS))
    ):
        return "server_error"
    if "timeout" in etype or (
        status is None and any(m in lowered for m in TIMEOUT_MARKERS)
    ):
        return "timeout"
    if etype == "contextwindowexceedederror" or any(m in lowered for m in CONTEXT_MARKERS):
        return "context_too_large"
    if status in (401, 403) or _is_infra_error(text):
        return "infra"
    return "hard"


def _provider_facts(
    error: object, classification: str, prompt_tokens_cumulative: int | None
) -> dict:
    """The raw provider facts recorded on a status event."""
    facts = {
        "http_status": getattr(error, "http_status", None),
        "error_type": getattr(error, "error_type", None),
        "provider_code": getattr(error, "provider_code", None),
        "message": redact_secrets(str(error or ""))[:PROVIDER_MESSAGE_MAX_CHARS],
        "model": getattr(error, "model", None),
        "prompt_tokens": getattr(error, "prompt_tokens", None),
        "classification": classification,
    }
    if prompt_tokens_cumulative is not None:
        facts["prompt_tokens_cumulative"] = prompt_tokens_cumulative
    return facts


LOCK_STAMP_NAME = ".fubala-lock-stamp"


def _is_npm_project(dir_path: Path) -> bool:
    """A dir where `npm ci` is meaningful: package.json AND its lockfile.

    Requiring both matters: `npm ci` refuses to run without a package.json,
    and repos carry stray committed lockfiles (a coder artifact) that would
    otherwise look like a forever-stale project and disable skipping for the
    whole worktree."""
    return (dir_path / "package.json").is_file() and (
        dir_path / "package-lock.json"
    ).is_file()


def _npm_project_dirs(ws: Path) -> list[Path]:
    """The workspace itself plus its immediate children that look like
    npm projects (a `package-lock.json` pins their dependencies)."""
    dirs = [ws] if _is_npm_project(ws) else []
    try:
        for child in sorted(ws.iterdir()):
            if child.is_dir() and _is_npm_project(child):
                dirs.append(child)
    except OSError:
        pass  # unreadable worktree: fall back to whatever we resolved
    return dirs


def _lock_hash(path: Path) -> str | None:
    """sha256 hex of a package-lock.json, or None when it doesn't exist."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _stamp_path(pkg: Path) -> Path:
    """The stamp records the lockfile hash of the last successful `npm ci`.

    It lives INSIDE node_modules on purpose: node_modules is gitignored
    everywhere and dies with every reinstall, so the stamp can never be
    committed by workspaces.commit or outlive the modules it describes."""
    return pkg / "node_modules" / LOCK_STAMP_NAME


def _stale(pkg: Path) -> bool:
    """True when `npm ci` must actually run in this package dir: no
    node_modules, no stamp, or a lockfile that changed since the stamp.

    Hand-edited node_modules under an unchanged lockfile are the accepted
    blind spot — the stamp only promises npm-ci semantics, not module
    integrity."""
    lock = _lock_hash(pkg / "package-lock.json")
    if lock is None:
        return False  # not an npm project; nothing to skip
    nm = pkg / "node_modules"
    if not nm.is_dir():
        return True
    try:
        stamped = _stamp_path(pkg).read_text().strip()
    except OSError:
        return True
    return stamped != lock


def _write_stamps(ws: Path) -> None:
    """Record the current lockfile hash for every npm dir that has modules.

    Called after a verify round exits 0, so the NEXT round can skip
    `npm ci`. Failures are silently ignored — a missing stamp just means
    the next round installs again."""
    for pkg in _npm_project_dirs(ws):
        lock = _lock_hash(pkg / "package-lock.json")
        if lock is None or not (pkg / "node_modules").is_dir():
            continue
        try:
            _stamp_path(pkg).write_text(f"{lock}\n")
        except OSError as e:
            log.debug("lock stamp for %s not written: %s", pkg, e)


def _prepare_verify(ws: Path, command: str) -> tuple[str, str]:
    """Warm-verify guard: return (command_to_run, skip_note).

    When every npm project dir in the worktree is up to date with its
    lockfile stamp, the exact `npm ci` token is replaced by the shell no-op
    `:` so the rest of the command (flags, `&&` chains) keeps working.
    Anything stale — or no npm projects at all — runs the command verbatim:
    with nothing to key a stamp off, rewriting would silently convert a
    failing `npm ci` (e.g. a missing lockfile) into a green round and change
    the command's exit-code meaning.

    The regex matches the token on word boundaries only, so `npm ci` inside
    a longer word or a quoted string that happens to contain it adjacent to
    other characters is left alone; a genuinely quoted `npm ci` (e.g. inside
    an echo) is still a known, accepted edge — verify commands come from
    the operator's own settings, not from agent output."""
    try:
        pkg_dirs = _npm_project_dirs(ws)
        fresh = bool(pkg_dirs) and all(not _stale(pkg) for pkg in pkg_dirs)
    except OSError:
        fresh = False
    if not fresh:
        return command, ""
    guarded, count = re.subn(r"\bnpm ci\b", ":", command)
    if not count:
        return command, ""
    return guarded, "[verify] npm ci skipped (lockfile unchanged)\n"


def _cwd_in_repo(workspace_path: Path) -> str:
    """The workspace path relative to its git toplevel ('' when it IS the
    toplevel) -- where the pod's repo checkout maps it."""
    res = subprocess.run(
        ["git", "-C", str(workspace_path), "rev-parse", "--show-prefix"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return res.stdout.strip().strip("/") if res.returncode == 0 else ""


def _run_verify_command(
    workspace_path: Path,
    command: str,
    timeout: float,
    project_root: str | None = None,
    story_id: str | None = None,
    executor: procs.Executor | None = None,
) -> tuple[int, str]:
    """Run the operator-configured verify command in the worktree (trusted:
    configured explicitly, unlike agent-requested commands).

    Warm verify: npm and uv share the repo-level cache dirs (see
    workspaces.shared_cache_env), and `npm ci` is skipped when the lockfile
    is unchanged since the last green round. Command text, timeout and the
    rc semantics are otherwise untouched — a stale worktree runs exactly
    what the operator configured.

    Process groups: the command runs in its OWN session (see app.procs), so a
    timeout kills the shell AND its whole tree (pytest's xdist workers, npm's
    helpers) instead of orphaning them. `story_id` additionally registers the
    group so StoryRunner.cancel/stop and the /proc sweep can reach it; it
    defaults to None for call sites with no story in flight — the group is then
    still timeout-killable, which covers the incident class.

    `executor` (worker pods): None means the in-process LocalExecutor, exactly
    the behaviour above. A pod executor ships the worktree into a per-run pod
    and runs there; a fresh pod has no node_modules, so the npm-ci skip and the
    lock stamps are bypassed. Any pod problem (PodUnavailable) reruns THIS
    verify in-process, and the executor stays disabled for the rest of the run.

    Sync signature kept on purpose (test_event_loop_responsiveness pins it):
    the verify stage runs it through asyncio.to_thread.
    """
    if executor is not None and executor.is_pod and not getattr(executor, "disabled", False):
        try:
            executor.prepare()
            executor.sync_workspace(workspace_path)  # type: ignore[attr-defined]
            return executor.run(
                pods.add_maxprocesses(command),
                _cwd_in_repo(workspace_path),
                None,
                timeout,
            )
        except pods.PodUnavailable as e:
            if pods.local_fallback_disabled(executor.settings):
                raise pods.FallbackCapFull("fallback cap 0: no local verify") from e
            # fall through to the in-process path below
        except (OSError, subprocess.SubprocessError) as e:
            executor.close()
            log.warning("worker pod verify failed (%s); running in-process", e)
    guarded, note = (
        _prepare_verify(workspace_path, command) if project_root else (command, "")
    )
    env = (
        workspaces.scrub_child_env(
            {**os.environ, **workspaces.shared_cache_env(project_root)}
        )
        if project_root
        else None
    )
    rc, output = procs.LocalExecutor(workspace_path, story_id).run(
        guarded, "", env, timeout
    )
    if rc == 0:
        _write_stamps(workspace_path)
    return rc, note + output

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
CODER_PROMPT = PROMPTS_DIR / "coder.md"
CODE_REVIEWER_PROMPT = PROMPTS_DIR / "code_reviewer.md"


def resolve_verify_command(db: Session, project_id: str | None) -> tuple[str, float]:
    """The verify command + timeout for a story's project: the app-wide
    `verify_command` setting, overridden by `project.verify_command`.

    Extracted from _verify_and_fix so the coder's run_verify tool resolves
    through the SAME source — the tool and the verify stage cannot drift."""
    settings = load_settings(db.get_bind())
    command = (settings.get("verify_command") or "").strip()
    timeout = float(settings.get("verify_timeout", 600))
    if project_id:
        project = db.get(Project, project_id)
        if project is not None and (project.verify_command or "").strip():
            command = project.verify_command.strip()
    return command, timeout


def resolve_test_command(db: Session, project_id: str | None) -> str:
    """The project's declared TEST command for the coder's run_tests tool.

    Project override ONLY — unlike verify_command there is no app-wide
    default, so an unset project simply gets no tool. The Python version
    lives inside the command text (`uv run --python 3.15 ... pytest -q`):
    one source of truth, no drift with a separate column."""
    if not project_id:
        return ""
    project = db.get(Project, project_id)
    if project is None:
        return ""
    return (project.test_command or "").strip()


def test_command_prompt_line(test_command: str) -> str:
    """The single line appended to the coder prompt when a test command is
    declared — the coder must know the exact command without guessing (and
    never stall on `uv` allowlists / $HOME interpreter installs)."""
    cmd = (test_command or "").strip()
    if not cmd:
        return ""
    return (
        "=== TESTS ===\n"
        f"This project's test command is: `{cmd}`\n"
        "Run it with the run_tests tool (no permission request needed); pass "
        "`paths` (e.g. ['tests/test_x.py']) to run individual files, omit it "
        "for the full suite."
    )


# path segments that mean "the coder vendored a dependency tree instead of
# declaring it": the 16 Oct lesson (node_modules / site-packages / .venv
# dumps reaching review, unflagged, as 10k-file diffs)
VENDORED_SEGMENTS = ("site-packages", "node_modules", ".venv")
# a `.local-` prefix (e.g. `.local-lib`, `.local-share`) is the classic shape
# of a copied venv/user tree renamed to dodge obvious detection
VENDORED_PREFIX = ".local-"
# more than this many added files is a scaffold/dump, not an implementation —
# flagged for judgment, never silently discarded
MAX_ADDED_FILES = 200
# how many unstaged artefact paths the warning StoryNote names before
# switching to "(+N more)" — notes are capped at 8192 bytes
ARTEFACT_NOTE_PATH_CAP = 20


def artefact_note_body(paths: list[str]) -> str:
    """Pure text of the warning note for artefacts kept out of a commit."""
    shown = [p for p in paths][:ARTEFACT_NOTE_PATH_CAP]
    more = len(paths) - len(shown)
    listing = ", ".join(shown) + (f" (+{more} more)" if more > 0 else "")
    return (
        "build artefacts were kept OUT of the coder commit by fubala's "
        "pre-commit guard (unstage of build/, dist/, .venv/ or *.egg-info/): "
        f"{listing}. The files are still on disk in the worktree — declare "
        "dependencies in the project's manifest instead of committing them."
    )


def vendored_guard(paths: list[str]) -> str | None:
    """Pure guard over the coder's added file paths (vs base_ref).

    Returns a violation reason when any added path carries a vendored
    segment (`site-packages`, `node_modules`, `.venv`) or a `.local-`-prefixed
    segment, or when more than MAX_ADDED_FILES files were added. The reason
    samples offending paths so the coder (and the human, on escalation) can
    judge — legit large diffs (scaffolds, snapshots) exist. None = clean.
    """
    offending: list[str] = []
    for path in paths:
        segments = Path(path).parts or (path,)
        if any(
            seg in VENDORED_SEGMENTS or seg.startswith(VENDORED_PREFIX)
            for seg in segments
        ):
            offending.append(path)
    if offending:
        samples = ", ".join(offending[:5])
        return (
            "vendored dependency dump detected: added path(s) "
            f"{samples}"
            f" ({len(offending)} file(s) under vendored segments like "
            f"{', '.join(VENDORED_SEGMENTS)} / {VENDORED_PREFIX}*). Never install or "
            "vendor packages into the worktree — declare dependencies in the "
            "project's manifest instead; remove the vendored files."
        )
    if len(paths) > MAX_ADDED_FILES:
        samples = ", ".join(paths[:5])
        return (
            f"mass file addition detected: {len(paths)} added files (limit "
            f"{MAX_ADDED_FILES}); sample: {samples}. If this diff is legitimate "
            "(scaffold, snapshots, generated data), say so when re-answering — "
            "a repeat offense escalates to human review."
        )
    return None


DIFF_CHUNK_SIZE = 60_000  # chars per diff chunk for code review


def chunk_diff(diff_text: str, max_chars: int = DIFF_CHUNK_SIZE) -> list[str]:
    """Split a unified diff into review chunks, respecting file boundaries where possible.

    If diff_text <= max_chars, returns [diff_text].
    Otherwise, groups complete file diffs ('diff --git ') into chunks up to max_chars.
    If a single file diff exceeds max_chars, splits it by lines so no lines are cut.
    """
    if not diff_text or len(diff_text) <= max_chars:
        return [diff_text] if diff_text else []

    parts = diff_text.split("diff --git ")
    file_diffs: list[str] = []
    if parts[0]:
        file_diffs.append(parts[0])
    for part in parts[1:]:
        file_diffs.append("diff --git " + part)

    chunks: list[str] = []
    current_chunk: list[str] = []
    current_len = 0

    for fd in file_diffs:
        if len(fd) > max_chars:
            if current_chunk:
                chunks.append("".join(current_chunk))
                current_chunk = []
                current_len = 0
            lines = fd.splitlines(keepends=True)
            single_chunk: list[str] = []
            single_len = 0
            for line in lines:
                if single_len + len(line) > max_chars and single_chunk:
                    chunks.append("".join(single_chunk))
                    single_chunk = [line]
                    single_len = len(line)
                else:
                    single_chunk.append(line)
                    single_len += len(line)
            if single_chunk:
                current_chunk = single_chunk
                current_len = single_len
        elif current_len + len(fd) > max_chars:
            if current_chunk:
                chunks.append("".join(current_chunk))
            current_chunk = [fd]
            current_len = len(fd)
        else:
            current_chunk.append(fd)
            current_len += len(fd)

    if current_chunk:
        chunks.append("".join(current_chunk))

    return chunks or [diff_text]

CONTEXT_FILE_CAP = 16_000  # chars per file — instructions, not a knowledge base


def context_files_block(settings: dict[str, Any], roots: list[str | None]) -> str:
    """Assemble the harness-injected project instructions.

    Models don't reliably read convention files on their own, so matching
    files (settings['context_files'], comma-separated names/globs) are
    prepended to the spec-writer and coder system prompts — Claude-Code
    style. First match per glob wins; roots are searched in order.
    """
    patterns = [p.strip() for p in (settings.get("context_files") or "").split(",") if p.strip()]
    if not patterns:
        return ""
    sections: list[str] = []
    seen: set[str] = set()
    for pattern in patterns:
        for root in roots:
            if not root:
                continue
            matches = [
                m
                for m in sorted(glob.glob(str(Path(root) / pattern)))
                if Path(m).is_file() and m not in seen
            ]
            if not matches:
                continue
            path = matches[0]
            seen.add(path)
            try:
                text = Path(path).read_text(errors="replace")[:CONTEXT_FILE_CAP]
            except OSError:
                continue
            sections.append(
                f"=== PROJECT INSTRUCTIONS: {Path(path).name} ===\n{text}"
            )
            break  # first root that has the file wins
    return "\n\n".join(sections)


def story_payload(story: Story, project_executor: str | None = None) -> dict[str, Any]:
    """Snapshot a Story into a JSON-safe dict (call before session commit).

    `execution` is the *effective* value (override, else the project's
    executor, else internal) — that is what the board renders and branches
    on. The raw override rides along as `execution_override` for edit forms.
    Callers without a project handle (the runner's own pipeline) may omit
    `project_executor`: the runner only ever holds effectively-internal
    stories, so the internal fallback is correct there."""
    return {
        "id": story.id,
        "title": story.title,
        "description": story.description,
        "acceptance": story.acceptance,
        "status": story.status,
        "project_id": story.project_id,
        "epic_id": story.epic_id,
        "depends_on": json.loads(story.depends_on or "[]"),
        "model": story.model,
        "skill": story.skill,
        "priority": story.priority or StoryPriority.NORMAL,
        "created_by": story.created_by,
        "execution": effective_execution(story, project_executor),
        "execution_override": story.execution,
        "gate_spec": story.gate_spec or StoryGate.HUMAN,
        "gate_code": story.gate_code or StoryGate.HUMAN,
        "human_rejected": bool(story.human_rejected),
        "kind": story.kind or StoryKind.CODE,
        "claimed_by": story.claimed_by,
        "claimed_at": as_utc_iso(story.claimed_at) if story.claimed_at else None,
        "spec": story.spec,
        "spec_version": story.spec_version,
        "spec_rejects": story.spec_rejects or 0,
        "spec_history": json.loads(story.spec_history or "[]"),
        "base_ref": story.base_ref,
        "max_tool_calls": story.max_tool_calls,
        "verify_result": json.loads(story.verify_result) if story.verify_result else None,
        "ai_review": story.ai_review,
        "ai_verdict": story.ai_verdict,
        "approved_by": story.approved_by,
        "error": story.error,
        # push+PR delivery: the PR facts stamped at approve (None for
        # merge/push-only stories and legacy rows)
        "pr_url": story.pr_url,
        "pr_index": story.pr_index,
        "pushed_sha": story.pushed_sha,
        # in-memory only: working | waiting_tests | waiting_slot | None
        "slot_state": _SLOT_STATE.get(story.id),
        "created_at": as_utc_iso(story.created_at),
        "updated_at": as_utc_iso(story.updated_at),
    }


def record_event(db: Session, story_id: str, kind: str, data: dict[str, Any]) -> None:
    db.add(StoryEvent(story_id=story_id, kind=kind, data=json.dumps(data)))


def apply_ab_headless(db: Session, story: Story, share: float | None = None) -> bool:
    """Queue-time A/B split: stamp `execution=headless` on a story with no
    explicit execution override when the owning project's `ab_headless_share`
    draw hits, and leave the note event.

    Both chokepoints that put a story in front of the scheduler call this —
    POST /stories/{id}/queue and the `_tick` claim — so a story reaching
    `queued` through any path (queue endpoint, reject, re-spec, release,
    rate-limit requeue) is drawn by pickup even when the queue-time draw was
    missed or the share changed meanwhile. The draw is deterministic per
    story id (`headless_assignment`), so it resolves the same way across
    restarts; once materialized nothing re-rolls, while a share change
    re-draws still-NULL stories under the new share (accepted). `share`
    reuses a precomputed per-project share (the tick reads them in bulk);
    None looks the project up. The note keeps `feedback: False` so the
    coder-feedback filter never picks it up. Returns True when stamped.
    """
    if story.execution is not None or not story.project_id:
        return False
    if share is None:
        project = db.get(Project, story.project_id)
        share = float(project.ab_headless_share or 0.0) if project else 0.0
    if share > 0 and headless_assignment(story.id, share):
        story.execution = StoryExecution.HEADLESS
        record_event(
            db,
            story.id,
            "note",
            {
                "ab_assignment": StoryExecution.HEADLESS,
                "share": share,
                "feedback": False,
                "text": f"A/B: headless (share {share})",
            },
        )
        return True
    return False


# ---- machine-checked gates ---------------------------------------------


@dataclass(frozen=True)
class GateCheck:
    """One machine check behind an `auto` gate: name, verdict, why."""

    name: str
    passed: bool
    detail: str = ""


def _glob_list(raw: str | None) -> list[str]:
    """Split a comma-separated glob list into cleaned entries."""
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def glob_matches_any(path: str, globs: list[str]) -> bool:
    """fnmatch-style match of a repo-relative path against glob entries.

    A pattern also matches when it names a DIRECTORY prefix: `backend/`
    matches `backend/app/runner.py` (a trailing slash reads as 'everything
    below'), and `backend` matches `backend/app/runner.py` too, so entries
    behave the way a human types them."""
    if not path or not globs:
        return False
    normalized = path.replace("\\", "/").lstrip("/")
    for pattern in globs:
        p = pattern.replace("\\", "/").rstrip("/")
        if not p:
            continue
        if fnmatch(normalized, p) or fnmatch(normalized, f"{p}/*"):
            return True
    return False


def _diff_line_counts(diff_text: str) -> tuple[int, int]:
    """(added, removed) line counts from a unified diff (excluding +++/---)."""
    added = removed = 0
    for line in diff_text.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return added, removed


def _diff_changed_paths(diff_text: str) -> list[str]:
    """Repo-relative paths a unified diff touches (renames count on both
    sides — each named path is a changed path for the glob checks)."""
    paths: list[str] = []
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            rest = line[len("diff --git ") :]
            _, _, b_side = rest.partition(" b/")
            candidates = [side.strip() for side in rest.split(" b/") if side.strip()]
            # strip the leading "a/" from the first side
            cleaned = []
            for candidate in candidates:
                if candidate.startswith("a/"):
                    candidate = candidate[2:]
                cleaned.append(candidate.strip("\"'"))
            paths.extend(cleaned)
        elif line.startswith("--- ") or line.startswith("+++ "):
            side = line[4:].strip()
            if side and side != "/dev/null" and not side.startswith(
                ("a/", "b/")
            ):
                paths.append(side.strip("\"'"))
    return list(dict.fromkeys(paths))


def evaluate_spec_gate(
    *,
    ai_verdict: str | None,
    human_rejected: bool,
) -> list[GateCheck]:
    """The spec gate's machine checks. Runs only where the story's resolved
    gate is `auto` and an approving AI spec review would otherwise park the
    story at the human gate."""
    checks = [
        GateCheck(
            name="ai_spec_review_approved",
            passed=ai_verdict == "approved",
            detail=f"AI spec review verdict: {ai_verdict or 'none'}",
        ),
        GateCheck(
            name="no_human_rejection",
            passed=not human_rejected,
            detail="a human rejected this story at some gate"
            if human_rejected
            else "no human rejection recorded",
        ),
    ]
    return checks


def evaluate_code_gate(
    *,
    ai_verdict: str | None,
    verify: dict[str, Any] | None,
    diff_text: str,
    human_rejected: bool,
    protected_globs: str | None,
    frozen_globs: str | None,
    auto_max_diff_lines: int,
) -> list[GateCheck]:
    """The code gate's machine checks. `verify` is the story's parsed
    verify_result; an unconfigured verify (None) passes by design."""
    paths = _diff_changed_paths(diff_text)
    protected = _glob_list(protected_globs)
    frozen = _glob_list(frozen_globs)
    hit_protected = [p for p in paths if glob_matches_any(p, protected)]
    hit_frozen = [p for p in paths if glob_matches_any(p, frozen)]
    verify_failed = bool(verify) and verify.get("applicable") is not False and not verify.get(
        "passed", True
    )
    added, removed = _diff_line_counts(diff_text)
    total = added + removed
    cap = int(auto_max_diff_lines or 0)
    return [
        GateCheck(
            name="verify_green",
            passed=not verify_failed,
            detail=(
                f"verify failed after {verify.get('attempts')} attempt(s)"
                + (f" ({verify['mismatch']})" if verify.get("mismatch") else "")
                if verify_failed
                else "verify green"
                if verify
                else "no verify configured (passes)"
            ),
        ),
        GateCheck(
            name="ai_code_review_approved",
            passed=ai_verdict == "approved",
            detail=f"AI code review verdict: {ai_verdict or 'none'}",
        ),
        GateCheck(
            name="no_protected_paths",
            passed=not hit_protected,
            detail=(
                "diff touches protected paths: " + ", ".join(hit_protected[:8])
                if hit_protected
                else "no protected path touched"
            ),
        ),
        GateCheck(
            name="no_frozen_paths",
            passed=not hit_frozen,
            detail=(
                "diff touches frozen paths: " + ", ".join(hit_frozen[:8])
                if hit_frozen
                else "no frozen path touched"
            ),
        ),
        GateCheck(
            name="diff_within_max",
            passed=(cap <= 0) or (total <= cap),
            detail=f"diff is {total} lines (+{added}/-{removed}); cap {cap}",
        ),
        GateCheck(
            name="no_human_rejection",
            passed=not human_rejected,
            detail="a human rejected this story at some gate"
            if human_rejected
            else "no human rejection recorded",
        ),
    ]


def gate_checks_payload(gate: str, checks: list[GateCheck]) -> dict[str, Any]:
    """The `gate_auto` event payload: every check with its verdict."""
    return {
        "gate": gate,
        "checks": [
            {"name": c.name, "passed": c.passed, "detail": c.detail} for c in checks
        ],
        "approved": all(c.passed for c in checks),
    }


def gate_note_body(gate: str, checks: list[GateCheck]) -> str:
    """The StoryNote body written when an auto gate parks a story: names the
    failed checks so the human at the gate sees why, verbatim check names
    (the acceptance tests match on them)."""
    failed = [c for c in checks if not c.passed]
    names = ", ".join(c.name for c in failed) or "none"
    lines = [
        f"auto {gate} gate held the story at the human gate:",
        f"failed checks: {names}",
    ]
    for check in failed:
        if check.detail:
            lines.append(f"- {check.name}: {check.detail}")
    return redact_secrets("\n".join(lines)).strip()


def _gate_project(db: Session, story: Story) -> Project | None:
    """The story's project, if any — gate checks read its glob lists."""
    if story.project_id is None:
        return None
    return db.get(Project, story.project_id)


def _write_gate_note(db: Session, story: Story, gate: str, checks: list[GateCheck]) -> None:
    """Persist the parking note (author fubala, redacted, capped)."""
    body = gate_note_body(gate, checks)
    if body:
        db.add(
            StoryNote(story_id=story.id, body=body[:8192], author="fubala")
        )


def _story_project_root_for_diff(db: Session, story: Story) -> str | None:
    """The git root the story's worktree lives in, for the diff the gate
    checks read (same resolution as _run_code_review)."""
    settings_root = load_settings(db.get_bind()).get("project_root")
    if story.project_id:
        project = db.get(Project, story.project_id)
        if project is not None and project.root:
            return project.root
    return settings_root


def _evaluate_and_record(
    db: Session,
    story: Story,
    gate: str,
    checks: list[GateCheck],
) -> bool:
    """Record the gate_auto event, write the parking note when any check
    failed, and return whether the gate passed."""
    payload = gate_checks_payload(gate, checks)
    record_event(db, story.id, "gate_auto", payload)
    if not payload["approved"]:
        _write_gate_note(db, story, gate, checks)
    return payload["approved"]



def _artefact_note_writer(runner: StoryRunner, story_id: str) -> Callable[[list[str]], None]:
    """Callback for workspaces.commit's artefact guard (runner-side).

    Writes an append-only `StoryNote` (author "fubala") naming what the
    guard unstaged, redacted + capped like every other note body, so the
    human at the gate sees WHY a build tree is absent from the diff and
    the next coder/spec round can pick it up as feedback. Never raises:
    workspaces.commit already swallows callback errors, but a broken DB
    write must not take the commit down with it."""
    def _write(paths: list[str]) -> None:
        body = redact_secrets(artefact_note_body(paths)).strip()
        if not body:
            return
        try:
            with session_scope(runner.engine) as db:
                db.add(StoryNote(story_id=story_id, body=body[:8192], author="fubala"))
                record_event(
                    db,
                    story_id,
                    "note",
                    {"author": "fubala", "preview": body[:120], "artefact_guard": True},
                )
                bus.publish(
                    "story_log",
                    {"story_id": story_id, "text": f"[coder] {body[:200]}\n"},
                )
        except Exception:  # noqa: BLE001 — the commit has already happened
            log.exception("artefact warning note for %s failed", story_id)

    return _write


# ---- run transparency: activity + budget events ------------------------
#
# One StoryEvent row per round / tool call / tool result. Volume is bounded
# by max_tool_calls (default 12), previews are capped so SQLite rows stay
# tiny, and the same payloads ride the bus as `story_progress` for the live
# activity feed and budget meters.

ACTIVITY_PREVIEW = 80  # chars of tool arguments kept in the DB
TOOL_RESULT_CAP = 20_000  # mirrors the cap applied to role:tool messages


def tool_call_activity(name: str, arguments: str) -> dict[str, Any]:
    """Persistable description of a tool call (name + short arg preview)."""
    preview = arguments
    if len(preview) > ACTIVITY_PREVIEW:
        preview = preview[: ACTIVITY_PREVIEW - 1] + "…"
    data: dict = {"name": name, "args": preview}
    try:
        parsed = json.loads(arguments)
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, dict):
        for key in ("path", "command", "query", "story_id"):
            value = parsed.get(key)
            if isinstance(value, str) and value:
                data[key] = value[:ACTIVITY_PREVIEW]
    return data


def tool_result_activity(result: str) -> dict[str, Any]:
    """ok / error / truncated — display-only heuristic, never gates anything."""
    try:
        parsed = json.loads(result)
    except (TypeError, ValueError):
        parsed = None
    status = "ok"
    if isinstance(parsed, dict) and "error" in parsed:
        status = "error"
    elif len(result) > TOOL_RESULT_CAP:
        status = "truncated"
    data = {"status": status, "chars": len(result)}
    if isinstance(parsed, dict):
        error = parsed.get("error")
        if isinstance(error, str) and error:
            data["error"] = error[:ACTIVITY_PREVIEW]
    return data


def config_data_dir() -> Path:
    """The app's data dir: the SQLite file's parent, so artifacts follow the
    database (tmp DBs in tests, the volume in the pod)."""
    from .config import get_settings

    return Path(get_settings().database_path).parent


def data_dir_for_artifacts() -> Path:
    """Artifact root for kept headless transcripts (see config_data_dir)."""
    return config_data_dir()


def publish_activity(
    engine: Engine, story_id: str, kind: str, data: dict[str, Any]
) -> None:
    """Persist an activity event and fan the same payload out over SSE."""
    with session_scope(engine) as db:
        record_event(db, story_id, kind, data)
    bus.publish("story_progress", {"story_id": story_id, "kind": kind, "data": data})


def activity_tool_hook(
    engine: Engine, story_id: str
) -> Callable[[str, str, str], None]:
    """on_tool adapter: keep the free-text story_log AND record the tool_call
    + tool_result activity events for the live/replayed activity feed."""

    def hook(name: str, arguments: str, result: str) -> None:
        bus.publish(
            "story_log", {"story_id": story_id, "text": tool_log_line(name, arguments, result)}
        )
        publish_activity(engine, story_id, "tool_call", tool_call_activity(name, arguments))
        publish_activity(engine, story_id, "tool_result", tool_result_activity(result))

    return hook


def activity_tool_span_hooks(
    engine: Engine,
    story_id: str,
    tools_timeout_s: float,
    verify_timeout_s: float,
) -> tuple[Callable[[str, str, str], None], Callable[[str], None]]:
    """(on_tool_start, on_tool_end): tool_started / tool_finished events so the
    board can show a running tool call with its elapsed time."""

    def start(call_id: str, name: str, arguments: str) -> None:
        timeout = verify_timeout_s if name in ("run_verify", "run_tests") else tools_timeout_s
        publish_activity(
            engine,
            story_id,
            "tool_started",
            {
                **tool_call_activity(name, arguments),
                "call_id": call_id,
                "started_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                "timeout_s": timeout,
            },
        )

    def end(call_id: str) -> None:
        publish_activity(engine, story_id, "tool_finished", {"call_id": call_id})

    return start, end


ACTIVITY_KINDS = ("round", "tool_call", "tool_result")


def parse_verdict(review: str) -> tuple[str | None, str | None]:
    """Extract the verdict line the ai_reviewer prompt demands.

    Returns (verdict, reason): verdict is "approved" | "rejected" | None when
    no parsable verdict was found. None is NOT an approval: the code-review
    path gets one corrective retry and then lands at the human gate rejected;
    only the spec review treats it as fail-open (the human gate decides).
    """
    for raw in reversed([line.strip() for line in review.strip().splitlines() if line.strip()]):
        upper = raw.upper()
        if not upper.startswith("VERDICT:"):
            continue
        body = raw[len("VERDICT:") :].strip()
        if body.upper().startswith("APPROVED"):
            return "approved", None
        if body.upper().startswith("REJECTED"):
            reason = body[len("REJECTED") :].lstrip("-: ").strip() or None
            return "rejected", reason
    return None, None


def _first_failure_line(output: str) -> str:
    """Pick the most telling line from a failed verify output for the note.

    Prefers the first pytest-style FAILED/ERROR line, falls back to the last
    non-empty output line, and is truncated so the note stays one readable
    line. Never returns an empty string.
    """
    lines = [ln.strip() for ln in (output or "").splitlines() if ln.strip()]
    chosen = next(
        (ln for ln in lines if "FAILED" in ln.upper() or "ERROR" in ln.upper()),
        (lines[-1] if lines else ""),
    )
    chosen = chosen.strip()
    if len(chosen) > 200:
        chosen = chosen[:197] + "..."
    return chosen or "verify failed with no output"


def _land_project(db: Session, story: Story) -> Project | None:
    """The project whose pr_* settings opened (and can update) this PR."""
    if story.project_id is None:
        return None
    return db.get(Project, story.project_id)


def _landing_publish(
    db: Session, story: Story, _prev: str | None, data: dict[str, Any]
) -> None:
    """Commit a landing transition, then publish it (commit first — the
    phantom-status rule from routers/stories._commit_and_publish).

    `data["from"]` carries the previous status; the extra `_prev` argument
    is accepted (and ignored) because the call sites read better passing
    it positionally next to the payload."""
    record_event(db, story.id, "status", data)
    story.updated_at = utcnow()
    db.add(story)
    db.commit()
    payload = story_payload(story)
    bus.publish("story", {"story": payload})


def _land_race_detected(
    project: Project, token: str, head_sha: Any, info: dict
) -> bool:
    """Is this failed CI verdict a land race (main moved after a green suite)?

    Three conditions, ALL required (a miss on any is "not a race" and the
    story takes today's bounce — the safe default):
      1. the head sha's full status list carries a FAILED entry whose
         context ends with the land-job context suffix;
      2. the same list carries a SUCCESS whose description holds the
         marker the land job stamps on its green merge-result run;
      3. the PR is provably behind its base: the merge base of base...head
         differs from the base branch's own head (the PR payload's
         merge_base, else the compare API's). Unprovable (no base ref,
         unknown branch, lookup error) is NOT behind.

    Costs two extra GETs, and only ever runs on an already-failed land
    status — never on the happy path.
    """
    from . import pr as pr_module

    if not head_sha:
        log.debug("land race: no head sha to inspect for story base %s", project.id)
        return False
    try:
        entries = pr_module.all_statuses(
            project.pr_api_base, project.pr_repo, token, str(head_sha)
        )
    except pr_module.PullRequestError as e:
        log.debug("land race: status list unreadable for %s: %s", head_sha, e)
        return False
    land_failed = any(
        str(entry.get("context") or "").endswith(LAND_CONTEXT_SUFFIX)
        and entry.get("status") == "failure"
        for entry in entries
    )
    green_suite = any(
        entry.get("status") == "success"
        and LAND_RACE_MARKER in str(entry.get("description") or "")
        for entry in entries
    )
    if not (land_failed and green_suite):
        log.debug(
            "land race: signature absent on %s (land context failed=%s, "
            "green marker=%s)",
            head_sha,
            land_failed,
            green_suite,
        )
        return False
    base_ref = info.get("base_ref") or project.pr_base_branch
    if not base_ref:
        log.debug("land race: no base ref to compare against")
        return False
    try:
        base_head = pr_module.branch_head(
            project.pr_api_base, project.pr_repo, token, str(base_ref)
        )
    except pr_module.PullRequestError as e:
        log.debug("land race: base head lookup failed for %s: %s", base_ref, e)
        return False
    if not base_head:
        log.debug("land race: base branch %s has no known head", base_ref)
        return False
    # the PR payload carries merge_base on real Gitea; the compare payload
    # probably does not, so it is only the fallback
    merge_base = info.get("merge_base") or pr_module.compare_merge_base(
        project.pr_api_base, project.pr_repo, token, str(base_ref), str(head_sha)
    )
    behind = bool(merge_base) and merge_base != base_head
    if not behind:
        log.debug(
            "land race: %s not behind %s (merge base %s, base head %s)",
            head_sha,
            base_ref,
            merge_base,
            base_head,
        )
    return behind


def _watch_one_landing(db: Session, story: Story) -> None:
    """Advance one story in `landing` from its Gitea PR state.

    merged -> done (error cleared, checked FIRST — a merge outranks every
    other signal, including a stale CI verdict on a sha the branch update
    replaced). closed-without-merge -> back to the human gate. mergeable
    False on two consecutive polls -> merge conflict at the gate (Gitea
    computes mergeable async; one False is never a verdict). A failed
    combined commit status on the PR's CURRENT head sha (fallback
    pushed_sha) -> gate with the status description. Anything else leaves
    the story landing.
    """
    from . import pr as pr_module

    project = _land_project(db, story)
    if project is None or not project.pr_api_base or not project.pr_repo:
        # no PR config to poll (a push-only project never lands here, but a
        # project could have been reconfigured mid-flight): park at the gate
        # rather than spin against a Gitea we cannot reach
        prev = story.status
        story.status = StoryStatus.HUMAN_REVIEW
        story.error = "land failed: pull request configuration missing"
        story.land_state = None
        _landing_publish(
            db,
            story,
            prev,
            {
                "from": prev,
                "to": StoryStatus.HUMAN_REVIEW,
                "landing": True,
                "error": story.error,
                "pr_index": story.pr_index,
                "pr_url": story.pr_url,
            },
        )
        return

    try:
        token = pr_module.read_token(project.pr_token_file or "")
        info = pr_module.get_pull(
            project.pr_api_base, project.pr_repo, token, int(story.pr_index)
        )
        # CI is judged on the PR's CURRENT head: retry-land's branch update
        # moves the head, so story.pushed_sha goes stale and a failure left
        # on it (incident f9014e83) would bounce a perfectly good landing.
        # The pushed_sha fallback keeps older Gitea fakes (no `head` in the
        # payload) working.
        head_sha = info.get("head_sha") or story.pushed_sha
        status = (
            pr_module.combined_status(
                project.pr_api_base, project.pr_repo, token, str(head_sha)
            )
            if head_sha
            else None
        )
        description = (
            pr_module.status_description(
                project.pr_api_base, project.pr_repo, token, str(head_sha)
            )
            if head_sha and status == "failure"
            else None
        )
    except pr_module.PullRequestError as e:
        # a transient network/HTTP failure must not bounce a story that is
        # landing fine; log it and retry on the next poll
        log.warning("land poll for story %s failed: %s", story.id, e)
        return
    # wall clock, not poll count: see LAND_CONFLICT_MIN_DWELL
    now = utcnow()

    if info.get("merged"):
        prev = story.status
        story.error = None
        story.land_state = None
        story.status = StoryStatus.DONE
        _landing_publish(
            db,
            story,
            prev,
            {
                "from": prev,
                "to": StoryStatus.DONE,
                "landing": True,
                "merged": True,
                "pr_index": story.pr_index,
                "pr_url": story.pr_url,
            },
        )
        return

    if info.get("state") == "closed":
        prev = story.status
        story.status = StoryStatus.HUMAN_REVIEW
        story.error = "land failed: pull request closed without merging"
        story.land_state = None
        _landing_publish(
            db,
            story,
            prev,
            {
                "from": prev,
                "to": StoryStatus.HUMAN_REVIEW,
                "landing": True,
                "error": story.error,
                "pr_index": story.pr_index,
                "pr_url": story.pr_url,
            },
        )
        return

    if status == "failure":
        # A merge race (main moved between the green suite and the merge)
        # is recoverable without a human: update the PR branch with main
        # and let the new CI run land it — at most LAND_RACE_MAX_ATTEMPTS
        # times. Every detection miss falls through to today's bounce.
        attempts = _land_race_attempts(story.land_state)
        stale = _land_race_stale(story.land_state)
        if stale is not None and head_sha == stale[0]:
            # the failed status is from before our branch update: not a
            # verdict. Wait for the head to move; if it never does, the
            # update did nothing and a human has to look.
            if (now - stale[1]).total_seconds() < LAND_GREEN_UNMERGED_MIN_DWELL:
                story.updated_at = now
                db.add(story)
                db.commit()
                return
            prev = story.status
            story.status = StoryStatus.HUMAN_REVIEW
            story.error = (
                "land failed: main moved during land; "
                "branch update did not move the PR head"
            )
            story.land_state = None
            _landing_publish(
                db,
                story,
                prev,
                {
                    "from": prev,
                    "to": StoryStatus.HUMAN_REVIEW,
                    "landing": True,
                    "error": story.error,
                    "re_land_race": True,
                    "pr_index": story.pr_index,
                    "pr_url": story.pr_url,
                },
            )
            return
        if _land_race_detected(project, token, head_sha, info):
            if attempts >= LAND_RACE_MAX_ATTEMPTS:
                prev = story.status
                story.status = StoryStatus.HUMAN_REVIEW
                story.error = (
                    "land failed: main moved during land "
                    f"({LAND_RACE_MAX_ATTEMPTS} re-land attempts used)"
                )
                story.land_state = None
                _landing_publish(
                    db,
                    story,
                    prev,
                    {
                        "from": prev,
                        "to": StoryStatus.HUMAN_REVIEW,
                        "landing": True,
                        "error": story.error,
                        "re_land_race": True,
                        "pr_index": story.pr_index,
                        "pr_url": story.pr_url,
                    },
                )
                return
            race = attempts + 1
            try:
                pr_module.update_branch(
                    project.pr_api_base, project.pr_repo, token,
                    int(story.pr_index),
                )
            except pr_module.PullRequestError as e:
                # the update can race a just-merged PR: re-read the PR
                # (the retry-land pattern) — merged means done, not failure
                post = None
                try:
                    post = pr_module.get_pull(
                        project.pr_api_base, project.pr_repo, token,
                        int(story.pr_index),
                    )
                except pr_module.PullRequestError:
                    post = None
                if post is not None and post.get("merged"):
                    prev = story.status
                    story.error = None
                    story.land_state = None
                    story.status = StoryStatus.DONE
                    _landing_publish(
                        db,
                        story,
                        prev,
                        {
                            "from": prev,
                            "to": StoryStatus.DONE,
                            "landing": True,
                            "merged": True,
                            "re_land_race": True,
                            "pr_index": story.pr_index,
                            "pr_url": story.pr_url,
                        },
                    )
                    return
                # anything else parks the story at the gate with the real
                # update error — same shape as a non-race failure
                prev = story.status
                story.status = StoryStatus.HUMAN_REVIEW
                story.error = f"land failed: {e}"
                story.land_state = None
                _landing_publish(
                    db,
                    story,
                    prev,
                    {
                        "from": prev,
                        "to": StoryStatus.HUMAN_REVIEW,
                        "landing": True,
                        "error": story.error,
                        "re_land_race": True,
                        "pr_index": story.pr_index,
                        "pr_url": story.pr_url,
                    },
                )
                return
            # update accepted: the story stays landing, the counter advances
            # and the new CI run on the moved head decides the outcome
            prev = story.status
            story.land_state = _land_race_stamp(race, str(head_sha), now)
            record_event(
                db,
                story.id,
                "note",
                {
                    "delivery": "push",
                    "re_land_race": True,
                    "attempt": race,
                    "reason": (
                        "main moved during land, relanding "
                        f"(attempt {race}/{LAND_RACE_MAX_ATTEMPTS})"
                    ),
                    "pr_index": story.pr_index,
                    "pr_url": story.pr_url,
                },
            )
            _landing_publish(
                db,
                story,
                prev,
                {
                    "from": prev,
                    "to": story.status,
                    "landing": True,
                    "re_land_race": True,
                    "attempt": race,
                    "pr_index": story.pr_index,
                    "pr_url": story.pr_url,
                },
            )
            return
        prev = story.status
        story.status = StoryStatus.HUMAN_REVIEW
        story.error = f"land failed: {description or 'CI status failure'}"
        story.land_state = None
        _landing_publish(
            db,
            story,
            prev,
            {
                "from": prev,
                "to": StoryStatus.HUMAN_REVIEW,
                "landing": True,
                "error": story.error,
                "pr_index": story.pr_index,
                "pr_url": story.pr_url,
            },
        )
        return

    if status == "success":
        # Green suite on the head sha but the PR is still open: the land
        # job posts its race-marker success BEFORE the merge POST, so a
        # refused merge (405/409) leaves exactly this signature (incident
        # run 3053 / PR 70, parked in `landing` for 7h). Dwell first —
        # the human merge of a green PR may still be in flight — then act
        # on the PR's changed files.
        try:
            files = pr_module.pull_files(
                project.pr_api_base,
                project.pr_repo,
                token,
                int(story.pr_index),
            )
        except pr_module.PullRequestError as e:
            # same hygiene as the poll itself: a blip must not bounce the
            # story, the next sweep re-reads the files
            log.warning("land poll file list for story %s failed: %s", story.id, e)
            return
        seen = _land_green_unmerged_since(story.land_state)
        if seen is None:
            # first observation: stamp the dwell clock, preserving the
            # land-race attempts already spent (one budget per story)
            spent = _land_race_attempts(story.land_state)
            story.land_state = (
                f"{_LAND_GREEN_UNMERGED}:{spent}:{as_utc_iso(now)}"
            )
            story.updated_at = now
            db.add(story)
            db.commit()
            return
        if any(f == ".gitea" or f.startswith(".gitea/") for f in files):
            # A CI PR is green-and-unmerged on purpose: the land job exits
            # 0 "waiting for coordinator" and a human merges it. No stamp
            # drives a re-land here — after the dwell, ONE note explains
            # the wait (idempotent: never re-noted on later polls).
            if (
                now - seen
            ).total_seconds() >= LAND_GREEN_UNMERGED_MIN_DWELL and not _ci_pr_note_exists(
                db, story.id
            ):
                record_event(
                    db,
                    story.id,
                    "note",
                    {
                        "delivery": "push",
                        "ci_pr_green": True,
                        "reason": _CI_PR_NOTE,
                        "pr_index": story.pr_index,
                        "pr_url": story.pr_url,
                    },
                )
                story.updated_at = now
                db.add(story)
                db.commit()
            return
        if (now - seen).total_seconds() < LAND_GREEN_UNMERGED_MIN_DWELL:
            # still inside the dwell: the merge may still land on its own
            story.updated_at = now
            db.add(story)
            db.commit()
            return
        # dwell elapsed: re-land through the existing branch-update path,
        # sharing the land-race budget; past the budget, the human gate
        attempts = _land_green_unmerged_attempts(story.land_state)
        if attempts >= LAND_RACE_MAX_ATTEMPTS:
            prev = story.status
            story.status = StoryStatus.HUMAN_REVIEW
            story.error = (
                "land failed: land run green but the PR stayed unmerged "
                f"after {LAND_RACE_MAX_ATTEMPTS} re-land attempts"
            )
            story.land_state = None
            _landing_publish(
                db,
                story,
                prev,
                {
                    "from": prev,
                    "to": StoryStatus.HUMAN_REVIEW,
                    "landing": True,
                    "error": story.error,
                    "re_land_green": True,
                    "pr_index": story.pr_index,
                    "pr_url": story.pr_url,
                },
            )
            return
        race = attempts + 1
        try:
            pr_module.update_branch(
                project.pr_api_base,
                project.pr_repo,
                token,
                int(story.pr_index),
            )
        except pr_module.PullRequestError as e:
            # the update can race a just-merged PR: re-read it — merged
            # means done, everything else parks at the gate with the error
            post = None
            try:
                post = pr_module.get_pull(
                    project.pr_api_base, project.pr_repo, token,
                    int(story.pr_index),
                )
            except pr_module.PullRequestError:
                post = None
            if post is not None and post.get("merged"):
                prev = story.status
                story.error = None
                story.land_state = None
                story.status = StoryStatus.DONE
                _landing_publish(
                    db,
                    story,
                    prev,
                    {
                        "from": prev,
                        "to": StoryStatus.DONE,
                        "landing": True,
                        "merged": True,
                        "re_land_green": True,
                        "pr_index": story.pr_index,
                        "pr_url": story.pr_url,
                    },
                )
                return
            if (
                isinstance(e, pr_module.PullRequestUpToDateError)
                and info.get("mergeable") is not False
                and head_sha
            ):
                # the head is already current and carries the green status:
                # merge it ourselves, pinned to that sha
                try:
                    pr_module.merge_pull(
                        project.pr_api_base,
                        project.pr_repo,
                        token,
                        int(story.pr_index),
                        str(head_sha),
                    )
                except pr_module.PullRequestError as merge_err:
                    e = merge_err
                else:
                    prev = story.status
                    story.error = None
                    story.land_state = None
                    story.status = StoryStatus.DONE
                    flags = {
                        "delivery": "push",
                        "re_land_green": True,
                        "up_to_date": True,
                        "merged": True,
                        "pr_index": story.pr_index,
                        "pr_url": story.pr_url,
                    }
                    record_event(db, story.id, "note", dict(flags))
                    _landing_publish(
                        db,
                        story,
                        prev,
                        {
                            "from": prev,
                            "to": StoryStatus.DONE,
                            "landing": True,
                            **flags,
                        },
                    )
                    return
            prev = story.status
            story.status = StoryStatus.HUMAN_REVIEW
            story.error = f"land failed: {e}"
            story.land_state = None
            _landing_publish(
                db,
                story,
                prev,
                {
                    "from": prev,
                    "to": StoryStatus.HUMAN_REVIEW,
                    "landing": True,
                    "error": story.error,
                    "re_land_green": True,
                    "pr_index": story.pr_index,
                    "pr_url": story.pr_url,
                },
            )
            return
        # update accepted: stay landing, the counter advances and the CI
        # run on the moved head decides the outcome
        prev = story.status
        story.land_state = f"{_LAND_RACE}:{race}"
        record_event(
            db,
            story.id,
            "note",
            {
                "delivery": "push",
                "re_land_race": True,
                "re_land_green": True,
                "attempt": race,
                "reason": (
                    "land run green but the PR is not merged "
                    f"(attempt {race}/{LAND_RACE_MAX_ATTEMPTS})"
                ),
                "pr_index": story.pr_index,
                "pr_url": story.pr_url,
            },
        )
        _landing_publish(
            db,
            story,
            prev,
            {
                "from": prev,
                "to": story.status,
                "landing": True,
                "re_land_green": True,
                "attempt": race,
                "pr_index": story.pr_index,
                "pr_url": story.pr_url,
            },
        )
        return

    if info.get("mergeable") is False:
        seen = _land_conflict_since(story.land_state)
        if (
            seen is not None
            and (now - seen).total_seconds() >= LAND_CONFLICT_MIN_DWELL
        ):
            # two consecutive False observations, far enough apart that
            # Gitea had time to compute mergeable: a real conflict
            prev = story.status
            story.status = StoryStatus.HUMAN_REVIEW
            story.error = "land failed: merge conflict"
            story.land_state = None
            _landing_publish(
                db,
                story,
                prev,
                {
                    "from": prev,
                    "to": StoryStatus.HUMAN_REVIEW,
                    "landing": True,
                    "error": story.error,
                    "pr_index": story.pr_index,
                    "pr_url": story.pr_url,
                },
            )
            return
        # first False (or not yet far enough apart): stamp the run's start
        # and decide on a later poll
        story.land_state = (
            f"{_LAND_CONFLICT}:{as_utc_iso(now)}" if seen is None else story.land_state
        )
        story.updated_at = now
        db.add(story)
        db.commit()
        return

    if story.land_state:
        # only transient stamps are forgotten here; a land-race attempt
        # counter must survive a healthy poll (the race recovery keeps
        # counting across them), so it is never cleared in this branch
        if (
            _land_conflict_since(story.land_state) is not None
            or _land_green_unmerged_since(story.land_state) is not None
        ):
            story.land_state = None
            story.updated_at = now
            db.add(story)
            db.commit()


def _watch_one_gate_landing(db: Session, story: Story) -> None:
    """Close a gate story out when its PR merged anyway (incident 6a438b3d).

    A story can sit at human_review with a `land failed:` error while its PR
    merges in the background (someone clicked merge in Gitea, or a retry
    landed after all). The landing poller never saw those rows — this
    merged-only check does. Everything else is untouched: a `land failed:`
    story stays at the gate until a human (or retry-land) acts, and stories
    failed for non-PR reasons never carry the marker, so they are never
    even looked up.
    """
    from . import pr as pr_module

    project = _land_project(db, story)
    if project is None or not project.pr_api_base or not project.pr_repo:
        return
    try:
        token = pr_module.read_token(project.pr_token_file or "")
        info = pr_module.get_pull(
            project.pr_api_base, project.pr_repo, token, int(story.pr_index)
        )
    except pr_module.PullRequestError as e:
        log.warning("gate land poll for story %s failed: %s", story.id, e)
        return
    if not info.get("merged"):
        return
    prev = story.status
    story.error = None
    story.land_state = None
    story.status = StoryStatus.DONE
    _landing_publish(
        db,
        story,
        prev,
        {
            "from": prev,
            "to": StoryStatus.DONE,
            "landing": True,
            "merged": True,
            "pr_index": story.pr_index,
            "pr_url": story.pr_url,
        },
    )


# ---- reconcile: a failed story whose branch actually merged ----------------

# A push+PR failure note event carries this delivery marker; the reconcile
# sweep reads it to prove the failure happened in the LANDING step (not in the
# coder/verify phases, whose failures must never reconcile to done).
_PUSH_DELIVERY = "push"


def _landing_failure_proven(db: Session, story: Story) -> bool:
    """Was this failed story's failure a LANDING failure (push / PR)?

    Provenance gate for the reconcile sweep. True when ANY of:
      - the story carries a pull request (pr_index) — only the landing step
        opens one;
      - story.error starts `land failed:` — the land poller's / retry-land's
        failure prefix;
      - the story's LATEST `delivery: "push"` note event carries an error
        (the _approve_push failure branch writes exactly that shape).
    A coder/verify failure ("coder produced no changes", a failing verify
    command) matches none of these, so it never reconciles.
    """
    if story.pr_index is not None:
        return True
    error = (story.error or "").strip()
    if error.startswith("land failed:"):
        return True
    latest = db.exec(
        select(StoryEvent)
        .where(StoryEvent.story_id == story.id, StoryEvent.kind == "note")
        .order_by(StoryEvent.id.desc())
    ).first()
    if latest is not None:
        try:
            data = json.loads(latest.data or "{}")
        except ValueError:
            data = {}
        if isinstance(data, dict) and data.get("delivery") == _PUSH_DELIVERY:
            if data.get("error"):
                return True
    return False


def _reconcile_one_failed(db: Session, story: Story) -> None:
    """Self-heal a failed push+PR story whose branch is already in the target.

    Landing is the only step that pushes a branch and opens a PR, so a FAILED
    story from that step whose head is contained in the target base was
    actually landed — the failure was an artefact (the transient blip that
    killed the approve, a PR merged by hand right after the status flipped).
    Flip it to done, clear the error and name the merge commit in an event.

    Conservative by construction: every condition below must hold, and
    anything unknowable (no sha, a deleted branch, a 404 compare, a project
    that no longer carries PR config, an external/merge-mode story) means
    SKIP — the story stays failed for a human to look at. Never raises into
    the sweep: failures are logged here, never status-flipping.
    """
    from . import pr as pr_module

    project = _land_project(db, story)
    if project is None or project.delivery != ProjectDelivery.PUSH:
        return
    # a complete PR config is required to ask Gitea anything at all
    pr_fields = (project.pr_api_base, project.pr_repo, project.pr_base_branch)
    if not all(pr_fields) or not project.pr_token_file:
        return
    if not _landing_failure_proven(db, story):
        return
    # an external story's branch belongs to whoever claimed it, not to the
    # landing step — never reconcile it (resolved against the project's
    # executor, because NULL on the row means "inherit the project")
    if (
        effective_execution(story, project.executor)
        == StoryExecution.EXTERNAL
    ):
        return

    try:
        token = pr_module.read_token(project.pr_token_file or "")
    except pr_module.PullRequestError as e:
        log.info("reconcile: token read failed for story %s: %s", story.id, e)
        return

    branch = f"fubala/{story.id}"
    head = story.pushed_sha or None
    try:
        if head is None:
            # fallback for old rows whose pushed_sha was never persisted
            head = pr_module.branch_head(
                project.pr_api_base, project.pr_repo, token, branch
            )
            if head is None:
                return
        elif pr_module.branch_head(
            project.pr_api_base, project.pr_repo, token, branch
        ) is None:
            # (a)-hardening: pushed_sha alone can name a branch the remote no
            # longer has (hand-deleted after a merge, or a hand-edited row).
            # Without the branch there is nothing to compare, so this is
            # "cannot prove" — skip, exactly like the fallback path above.
            return
    except pr_module.PullRequestError as e:
        log.info(
            "reconcile: branch head lookup failed for story %s: %s", story.id, e
        )
        return
    if not story.base_ref or head == story.base_ref:
        # (a) the story must have produced at least one commit — a branch
        # whose head IS the fork point carries no work at all
        return

    try:
        contained, merge_sha = pr_module.branch_merged(
            project.pr_api_base,
            project.pr_repo,
            token,
            head,
            project.pr_base_branch,
            pr_index=story.pr_index,
        )
    except pr_module.PullRequestError as e:
        log.info(
            "reconcile: compare failed for story %s: %s", story.id, e
        )
        return
    if not contained:
        return

    # The verdict above was computed from a row snapshot that may be seconds
    # old: the Gitea probes are network I/O, and an operator retry, a
    # re-approve or a direct row edit (tests stage failure shapes that way)
    # can land between the sweep's SELECT and this flip — the verdict then
    # rides facts that no longer hold. Re-read the row (ending the open read
    # transaction first: SQLite's WAL would otherwise serve the refresh the
    # SAME snapshot) and require the story to be exactly the one the verdict
    # was computed for; anything moved means "not my story anymore" — skip
    # and let the next sweep, which sees the fresh row, decide.
    seen = (story.status, story.pushed_sha, story.base_ref, story.pr_index)
    try:
        db.commit()
        db.refresh(story)
    except Exception:  # noqa: BLE001 — a deleted row is "not mine" too
        log.info("reconcile: story %s vanished under the sweep — skip", story.id)
        return
    if (
        story.status,
        story.pushed_sha,
        story.base_ref,
        story.pr_index,
    ) != seen or story.status != StoryStatus.FAILED:
        log.info("reconcile: story %s changed under the sweep — skip", story.id)
        return
    # a moved pushed_sha would make `contained` a verdict about a head the
    # row no longer names; base_ref moving would break the fork-point proof
    if story.pushed_sha != head:
        log.info("reconcile: story %s grew a different head — skip", story.id)
        return

    # the work provably landed: done, error cleared, merge commit named
    story.pushed_sha = head
    prev = story.status
    story.status = StoryStatus.DONE
    story.error = None
    story.land_state = None
    record_event(
        db,
        story.id,
        "land_reconciled",
        {
            "from": prev,
            "to": StoryStatus.DONE,
            "merge_sha": merge_sha,
            "pushed_sha": head,
            "base_ref": story.base_ref,
            "pr_index": story.pr_index,
            "pr_url": story.pr_url,
            "reconciled": True,
        },
    )
    story.updated_at = utcnow()
    db.add(story)
    db.commit()
    payload = story_payload(story)
    bus.publish("story", {"story": payload})
    log.info(
        "reconcile: story %s self-healed to done (merge commit %s)",
        story.id,
        merge_sha or "unknown",
    )


# slot_state per story id, in memory only (no DB column): what the story is
# doing in the slot pools right now. Read by story_payload, so the API and
# every SSE story event carry it; a restart starts with empty pools.
SLOT_WORKING = "working"
SLOT_WAITING_TESTS = "waiting_tests"
SLOT_WAITING_SLOT = "waiting_slot"
_SLOT_STATE: dict[str, str] = {}


def slot_state(story_id: str) -> str | None:
    return _SLOT_STATE.get(story_id)


class SlotPool:
    """A counted pool of concurrency slots with strict FIFO grants.

    Unlike asyncio.Semaphore, waiters sit in an explicit deque of futures,
    so the order is exactly the order of `acquire()` calls: a request queues
    at the moment it asks, and that is the only ordering rule. The pool
    records which story holds each slot. `resize()` (called every tick) never
    interrupts a holder; a smaller size only blocks new grants."""

    def __init__(
        self, name: str, size: int, on_change: Callable[[], None] | None = None
    ) -> None:
        self.name = name
        self.size = max(1, int(size))
        self.on_change = on_change
        self._holders: list[str] = []
        self._waiters: deque[tuple[str, asyncio.Future[None]]] = deque()

    def holders(self) -> list[str]:
        return list(self._holders)

    def waiting(self) -> list[str]:
        return [sid for sid, fut in self._waiters if not fut.done()]

    def resize(self, size: int) -> None:
        size = max(1, int(size))
        if size != self.size:
            self.size = size
            self._grant()

    async def acquire(self, story_id: str) -> None:
        if not self._waiters and len(self._holders) < self.size:
            self._holders.append(story_id)
            self._changed()
            return
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters.append((story_id, fut))
        self._changed()
        try:
            await fut
        except asyncio.CancelledError:
            if fut.done() and not fut.cancelled():
                # granted in the same step as the cancel: hand it back
                self.release(story_id)
            else:
                fut.cancel()
                self._waiters = deque(w for w in self._waiters if w[1] is not fut)
                self._grant()
            raise

    def release(self, story_id: str) -> None:
        with contextlib.suppress(ValueError):
            self._holders.remove(story_id)
        self._grant()

    def _grant(self) -> None:
        while self._waiters and len(self._holders) < self.size:
            story_id, fut = self._waiters.popleft()
            if fut.done():
                continue  # cancelled while queued
            self._holders.append(story_id)
            fut.set_result(None)
        self._changed()

    def _changed(self) -> None:
        if self.on_change is not None:
            try:
                self.on_change()
            except Exception:  # noqa: BLE001 - a listener never breaks a grant
                log.exception("slot pool %s change listener failed", self.name)


class StoryRunner:
    def __init__(
        self,
        engine: Engine,
        gateway: ProviderGateway,
        *,
        registry: ProviderRegistry,
    ) -> None:
        self.engine = engine
        self.gateway = gateway
        self.registry = registry
        self._running: set[str] = set()
        self._task: asyncio.Task | None = None
        # in-flight pipeline task per story id — the handles the scheduler
        # drops on the floor today; cancellation (and clean shutdowns) need
        # them, _running alone only knows the id
        self._tasks: dict[str, asyncio.Task] = {}
        # patient rate-limit retries: story id -> (not_before, attempt)
        self._backoff: dict[str, tuple[float, int]] = {}
        # consecutive provider-error attempts per story id — kept apart from
        # `_backoff` because _tick pops the backoff slot at pickup, and the
        # attempt count must survive that to make max_provider_retries bind
        self._attempts: dict[str, int] = {}
        # last cumulative prompt_tokens per running story (provider-error facts)
        self._prompt_tokens: dict[str, int] = {}
        # watchdog: story id -> time.monotonic() of the last sign of life on
        # the run's event stream (or of its launch, so a story that hasn't
        # produced its first event yet is never falsely stale)
        self._last_event: dict[str, float] = {}
        # watchdog phase floors: story id -> time.monotonic() until which the
        # sweep must spare the run, because it is inside a phase with its own
        # wall-clock bound (verify subprocess, guarded tool call, headless
        # CODE run). Every stamp site clears in finally, and _discard_run /
        # cancel pop the entry — a leaked floor would immunize a hung run
        # forever, the one failure mode this mechanism must not have.
        self._watchdog_floor: dict[str, float] = {}
        # provider usage-limit holds: provider id -> (until_utc, reason).
        # In-memory like `_backoff`: a restart forgets them, and the next
        # run into the same wall re-detects and re-holds (self-healing).
        self._provider_holds: dict[str, tuple[datetime, str]] = {}
        self._watchdog_q: asyncio.Queue[str] | None = None
        # next time.monotonic() the land poller may hit Gitea (0 = now);
        # spacing only — all land state lives on the Story row, so a
        # restart resumes from the DB, never from this attribute
        self._next_land_poll: float = 0.0
        # separate throttle for the failed-reconcile sweep: sharing
        # _next_land_poll would let whichever sweep runs first starve the
        # other for the life of the process
        self._next_reconcile_sweep: float = 0.0
        # dedicated threads for the blocking Gitea/git sweeps: a stuck call
        # must never starve the default executor that verify and git use
        self._gitea_pool = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="fubala-gitea"
        )
        # sweep name -> (future, monotonic start) of its in-flight run
        self._sweeps_inflight: dict[str, tuple[asyncio.Future[Any], float]] = {}
        # next monotonic time the orphaned-process-group sweep may run
        # (FUBALA_PROC_SWEEP_INTERVAL, default 600s); `start()` primes it to 0
        # so the first tick after a restart sweeps immediately
        self._next_proc_sweep: float = 0.0
        # heartbeat for /health: when the last tick COMPLETED and the poll
        # interval the loop last observed. `start()` stamps immediately (a
        # fresh process is alive by definition, so post-restart probes answer
        # 200); the loop re-stamps only after a completed tick — a loop alive
        # but stuck retrying a locked database must read as stalled, not
        # healthy. In-memory on purpose: health must answer while the DB is
        # locked, and a restart (which resets it) is exactly what the probe
        # exists to trigger.
        self._last_tick_at: datetime | None = None
        self._tick_interval: float = float(DEFAULTS["story_poll_interval"])
        # train CI hold log de-dup (in memory: one line per batch per process)
        self._train_ci_hold_logged: set[str] = set()
        self._train_ci_expired_logged: set[str] = set()
        # worker pods: one verify-only executor per coder run (run_executor
        # verify|pod); absent = the run verifies in-process
        self._executors: dict[str, pods.PodExecutor] = {}
        # next monotonic time the live worker pods get their heartbeat touch
        self._next_pod_beat: float = 0.0
        # concurrency is slots, not stories: any number of stories is in
        # flight, a story holds a model slot only while a model call runs and
        # a test slot only while its verify runs. Sizes are re-read every
        # tick (model_slots, test_slots).
        self._model_pool = SlotPool(
            "model", int(DEFAULTS["model_slots"]), self._publish_slot_states
        )
        self._test_pool = SlotPool(
            "test", int(DEFAULTS["test_slots"]), self._publish_slot_states
        )
        self._slot_states: dict[str, str] = {}
        # coder runs whose worker pod / backend-node claim was set up inside
        # their first granted model slot (never at pickup)
        self._gated: set[str] = set()

    def is_running(self, story_id: str) -> bool:
        return story_id in self._running

    # -- slots -----------------------------------------------------------

    def _publish_slot_states(self) -> None:
        """Recompute every story's slot_state from the two pools and publish
        the ones that changed (SSE `story_slot`)."""
        states: dict[str, str] = {}
        for sid in self._model_pool.waiting():
            states[sid] = SLOT_WAITING_SLOT
        for sid in self._test_pool.waiting() + self._test_pool.holders():
            states[sid] = SLOT_WAITING_TESTS
        for sid in self._model_pool.holders():
            states[sid] = SLOT_WORKING
        changed = [
            sid
            for sid in set(self._slot_states) | set(states)
            if self._slot_states.get(sid) != states.get(sid)
        ]
        for sid in changed:
            if sid in states:
                _SLOT_STATE[sid] = states[sid]
            else:
                _SLOT_STATE.pop(sid, None)
        self._slot_states = states
        for sid in changed:
            bus.publish("story_slot", {"story_id": sid, "slot_state": states.get(sid)})

    @contextlib.asynccontextmanager
    async def _model_slot(self, story_id: str) -> AsyncIterator[None]:
        """Held around one LLM call (spec writer, AI reviewer, coder round,
        code reviewer) and nothing else. Released on normal exit and on any
        exception, a 429 included: provider holds, verify, review queues and
        human gates all wait outside it."""
        await self._model_pool.acquire(story_id)
        try:
            if story_id in self._running:
                # the watchdog measures silence from the grant, not the queue
                self._last_event[story_id] = time.monotonic()
            yield
        finally:
            self._model_pool.release(story_id)

    @contextlib.asynccontextmanager
    async def _test_slot(self, story_id: str) -> AsyncIterator[None]:
        """Held for one whole verify run (in-process or in the worker pod).
        Never taken while the story holds a model slot."""
        await self._test_pool.acquire(story_id)
        try:
            if story_id in self._running:
                self._last_event[story_id] = time.monotonic()
            yield
        finally:
            self._test_pool.release(story_id)

    @contextlib.asynccontextmanager
    async def _coder_slot(self, story_id: str) -> AsyncIterator[None]:
        """The model slot of one coder round. The run's worker pod (or its
        backend-node fallback claim) is set up inside the run's FIRST granted
        slot, never at pickup: a story queued for a slot holds no pod, and
        queue time never counts against the pod's deadline. A full fallback
        cap requeues the story (the slot is released on the way out)."""
        async with self._model_slot(story_id):
            if story_id not in self._gated:
                if not await self._fallback_gate(story_id):
                    raise _HeadlessRequeued()
                self._gated.add(story_id)
            yield

    def _stamp_slot_waiters(self) -> None:
        """A run queued for a slot is silent on the bus by design; keep it
        fresh for the watchdog while it waits."""
        now = time.monotonic()
        for sid in self._model_pool.waiting() + self._test_pool.waiting():
            if sid in self._running:
                self._last_event[sid] = now

    def _report_proc_cleanup(
        self, story_id: str, reason: str, entries: list[dict[str, Any]]
    ) -> None:
        """procs reporter: one `proc_cleanup` note per process killed when a
        run ended. Own session (called from worker threads); the caller
        (procs.report) swallows failures."""
        with Session(self.engine) as db:
            for entry in entries:
                record_event(
                    db,
                    story_id,
                    "note",
                    {
                        "kind": "proc_cleanup",
                        "reason": reason,
                        "command": entry.get("command"),
                        "pid": entry.get("pid"),
                        "age_s": entry.get("age_s"),
                    },
                )
            db.commit()

    async def start(self) -> None:
        procs.set_reporter(self._report_proc_cleanup)
        # the watchdog taps the existing bus instead of every call site:
        # story_log / story_progress already carry story_id on every round,
        # tool call and delta
        self._watchdog_q = await bus.subscribe()
        # startup sweep: a server that died hard (SIGKILL, OOM) left its
        # subprocess trees marked in /proc but nobody waiting — kill them
        # before the first tick, so they cannot burn CPU alongside the new
        # run. Runs in a thread: /proc scanning is sync and non-trivial.
        self._next_proc_sweep = 0.0
        try:
            await asyncio.to_thread(procs.sweep, set(self._running))
        except Exception:  # noqa: BLE001 - a sweep failure must not stop startup
            log.exception("startup proc sweep failed")
        # worker pods of a previous backend process are orphans too (retry is
        # the strategy): delete every managed pod before the first tick,
        # bounded so an unreachable API server cannot hold startup
        try:
            await asyncio.wait_for(asyncio.to_thread(pods.sweep_pods, set()), 30.0)
        except Exception:  # noqa: BLE001 - a pod sweep failure must not stop startup
            log.exception("startup worker pod sweep failed")
        # heartbeat primed BEFORE the task exists: probes during the first
        # tick window answer 200, never "stalled"
        self._last_tick_at = utcnow()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                # cancellation is the expected exit path for the runner loop
                log.debug("runner loop cancelled during shutdown")
        # a sweep stuck in a Gitea call must not hold shutdown
        self._gitea_pool.shutdown(wait=False, cancel_futures=True)
        # in-flight pipelines should not outlive the server either
        for task in list(self._tasks.values()):
            task.cancel()
        # every subprocess tree this server spawned dies with it — a task
        # cancel only interrupts the coroutine; the blocked worker thread and
        # its child process group keep running without this
        try:
            procs.kill_all()
        except Exception:  # noqa: BLE001 - shutdown must always proceed
            log.exception("killing tracked process groups failed")
        # kill_all already reached the pods through the procs hook; this also
        # covers executors whose pod was never registered as live
        for executor in list(self._executors.values()):
            self._close_executor(executor)
        self._executors.clear()
        for task in list(self._tasks.values()):
            try:
                await asyncio.wait([task], timeout=2.0)
            except Exception as e:  # pragma: no cover - defensive
                log.debug("waiting for pipeline task during shutdown failed: %s", e)
        self._tasks.clear()
        if procs._REPORTER == self._report_proc_cleanup:
            procs.set_reporter(None)
        if self._watchdog_q is not None:
            bus.unsubscribe(self._watchdog_q)
            self._watchdog_q = None

    async def cancel(self, story_id: str) -> bool:
        """Abort an in-flight pipeline task. Returns True when a task was
        found and cancelled, False when there was nothing to cancel (the
        story was an orphan — e.g. resumed after a restart — or already done).

        The bounded wait matters: the verify command and the git calls run
        synchronously, so the task can only observe the interrupt at its next
        await point. Killing the story's process groups FIRST is what makes
        that wait short: SIGTERM is non-blocking, so a verify that is parked in
        `communicate()` sees the child die immediately and the task finishes
        inside the 5s window instead of riding out its full timeout. We give
        up after 5s and let the pipeline's own guards (`_finish` no-ops unless
        the story is still in flight) make any late writes harmless; the wip
        commit that follows in the endpoint is best-effort (a lock held by the
        dying task must not fail the cancel)."""
        task = self._tasks.get(story_id)
        self._running.discard(story_id)
        self._tasks.pop(story_id, None)
        self._attempts.pop(story_id, None)
        self._watchdog_floor.pop(story_id, None)
        self._prompt_tokens.pop(story_id, None)
        self._gated.discard(story_id)
        executor = self._executors.pop(story_id, None)
        if executor is not None:
            self._close_executor(executor)
        # the story's subprocess trees (verify, run_command, headless) start
        # dying NOW — before the task cancel, so the blocked worker thread can
        # observe the death instead of waiting out the verify timeout
        try:
            killed = procs.kill_story(story_id)
        except Exception:  # noqa: BLE001 - cancel must never fail on cleanup
            killed = 0
            log.exception("killing process groups for story %s failed", story_id)
        if killed:
            log.info("cancel: killed %d process group(s) for story %s", killed, story_id)
        if task is None or task.done():
            return False
        task.cancel()
        try:
            # never a bare await: a stuck subprocess would hang the endpoint
            done, _pending = await asyncio.wait([task], timeout=5.0)
            _ = done
        except asyncio.CancelledError:
            # the endpoint itself was cancelled — the task still got the
            # interrupt, and its finally blocks clean up _running/_tasks
            raise
        return True

    def tick_liveness(self) -> tuple[datetime | None, float]:
        """(time of the last COMPLETED tick, observed poll interval) — the
        raw material for /health's stall bound. Pure in-memory: the route must
        never touch the DB, or health would hang exactly when the DB is
        locked, which is the situation it exists to expose."""
        return self._last_tick_at, self._tick_interval

    async def _loop(self) -> None:
        """The scheduler heartbeat. Nothing in this coroutine may escape:
        the task created in start() is never recreated, so one unhandled
        exception kills scheduling for the life of the process while HTTP
        keeps answering (the 26 Oct incident: load_settings sat OUTSIDE the
        try, a 'database is locked' from it escaped, and the board froze for
        three hours).

        Every iteration — settings load, poll sleep, tick — happens inside
        the guard. A locked database retries with backoff (one poll interval,
        doubling, capped) instead of dying; any other exception is logged and
        the loop continues. load_settings stays INSIDE the loop on purpose:
        settings are live-tunable without a restart.

        The heartbeat stamp is written only after a tick COMPLETES — backoff
        and failed ticks deliberately do not stamp, so a loop that is alive
        but not making progress reads as stalled (that is the signal /health
        and the board banner carry), never as healthy."""
        lock_backoff = 0.0  # 0 = healthy cadence; else seconds until retry
        while True:
            ticked = False
            try:
                if lock_backoff > 0.0:
                    await asyncio.sleep(lock_backoff)
                settings = load_settings(self.engine)
                interval = float(settings["story_poll_interval"])
                # the observed interval feeds the stall bounds: 3x it (2-min
                # floor) on /health's banner flag, 10x it (10-min floor) on
                # /health/live's dead-loop verdict
                self._tick_interval = interval
                if lock_backoff == 0.0:
                    await asyncio.sleep(interval)
                self._test_pool.resize(int(settings["test_slots"]))
                await self._tick(int(settings["model_slots"]))
                ticked = True
            except Exception as exc:  # scheduler must never die
                if _is_locked_db(exc):
                    # one poll interval first, doubling, capped — never fatal
                    lock_backoff = (
                        min(self._tick_interval, SCHEDULER_LOCK_BACKOFF_CAP)
                        if lock_backoff == 0.0
                        else min(lock_backoff * 2.0, SCHEDULER_LOCK_BACKOFF_CAP)
                    )
                    log.warning(
                        "scheduler tick hit a locked database; "
                        "retrying in %.2fs",
                        lock_backoff,
                    )
                else:
                    log.exception("story scheduler tick failed")
            if ticked:
                lock_backoff = 0.0
                self._last_tick_at = utcnow()

    async def _reconcile_failed(self) -> None:
        """Self-heal FAILED stories whose branch actually merged (sweep).

        Runs on every scheduler tick alongside _poll_landing, with the same
        LAND_POLL_MIN_INTERVAL spacing of Gitea calls per process. Only push
        projects with complete PR config are swept; _reconcile_one_failed
        carries every gate and never raises. A story the sweep cannot prove
        stays failed — that is the contract, not an error.
        """
        now = time.monotonic()
        if self._next_reconcile_sweep and now < self._next_reconcile_sweep:
            return
        self._next_reconcile_sweep = now + LAND_POLL_MIN_INTERVAL
        await self._sweep_off_loop(
            "reconcile", self._reconcile_failed_sync, RECONCILE_SWEEP_CAP
        )

    def _reconcile_failed_sync(self) -> None:
        with session_scope(self.engine) as db:
            failed = db.exec(
                select(Story).where(Story.status == StoryStatus.FAILED)
            ).all()
            for story in failed:
                try:
                    _reconcile_one_failed(db, story)
                except Exception:  # noqa: BLE001 - one story never sinks the sweep
                    log.exception("reconcile sweep failed for story %s", story.id)

    async def _sweep_off_loop(
        self, name: str, fn: Callable[[], None], cap: float
    ) -> None:
        """Run a blocking sweep (sync urllib / git) on the dedicated pool under
        a wall-clock cap, so a hanging Gitea can never block the event loop.

        While the previous run of the same sweep is still alive (a thread
        stuck in DNS cannot be killed) the new one is skipped. On the cap the
        caller just returns; the next tick retries."""
        prev = self._sweeps_inflight.get(name)
        if prev is not None and not prev[0].done():
            log.warning(
                "%s sweep still running since %.0fs ago",
                name,
                time.monotonic() - prev[1],
            )
            return
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(self._gitea_pool, fn)
        self._sweeps_inflight[name] = (fut, time.monotonic())

        def _retrieve(f: asyncio.Future[Any]) -> None:
            # a sweep that outlives its cap must not log "never retrieved"
            if not f.cancelled():
                f.exception()

        fut.add_done_callback(_retrieve)
        try:
            # shield: the cap abandons the wait, not the tracked future
            await asyncio.wait_for(asyncio.shield(fut), cap)
        except TimeoutError:
            log.warning("%s sweep exceeded %.0fs; retrying on a later tick", name, cap)

    async def _proc_sweep(self) -> None:
        """Kill orphaned Fubala subprocess groups (see app.procs).

        Gated by a monotonic timer (FUBALA_PROC_SWEEP_INTERVAL, default 600s)
        because a /proc scan of every process on the host is not free, and the
        sweep must not run on every tick. Runs in a thread — it is pure sync
        /proc walking plus signals, and the loop has other work. Marked groups
        owned by a story currently running on this server are spared; that
        running set is snapshotted BEFORE the scan so a story picked up during
        the scan keeps its own children alive.
        """
        now = time.monotonic()
        if self._next_proc_sweep and now < self._next_proc_sweep:
            return
        self._next_proc_sweep = now + procs.sweep_interval()
        try:
            await asyncio.to_thread(procs.sweep, set(self._running))
        except Exception:  # noqa: BLE001 - the sweep must never kill the tick
            log.exception("orphaned-process sweep failed")
        try:
            # Succeeded/Failed pods left by the watchdog or the deadline too
            await asyncio.wait_for(
                asyncio.to_thread(pods.sweep_pods, set(self._running)), 30.0
            )
        except Exception:  # noqa: BLE001
            log.exception("worker pod sweep failed")

    async def _pod_heartbeat(self) -> None:
        """Touch the heartbeat of every live worker pod (their PID 1 exits
        when it goes 900s stale, so a dead backend cannot strand a pod)."""
        now = time.monotonic()
        if now < self._next_pod_beat:
            return
        self._next_pod_beat = now + pods.HEARTBEAT_EVERY
        if not self._executors:
            return
        try:
            await asyncio.to_thread(pods.heartbeat_all)
        except Exception:  # noqa: BLE001
            log.exception("worker pod heartbeat failed")

    async def _poll_landing(self) -> None:
        """Watch push+PR stories in `landing` until the PR actually merges.

        Runs on every scheduler tick (story_poll_interval) with a minimum
        spacing of LAND_POLL_MIN_INTERVAL between Gitea calls per process.
        All state lives on the Story row (pr_index/pushed_sha/land_state),
        so a restart resumes exactly where it left off.

        merged            -> done (error cleared)
        state closed, not merged -> back to the human gate with the reason
        mergeable False twice (consecutive polls) -> merge conflict at gate
        failed combined status on the PR head sha -> gate with the description

        Also sweeps gate rows parked there by a `land failed:` bounce: when
        their PR merged anyway (someone clicked merge in Gitea), the story
        closes as done — incident 6a438b3d. Non-merged rows are untouched.

        Every transition is a status event + bus publish, mirroring
        routers/stories._commit_and_publish (commit first, publish after).
        """
        now = time.monotonic()
        if self._next_land_poll and now < self._next_land_poll:
            return
        self._next_land_poll = now + LAND_POLL_MIN_INTERVAL
        await self._sweep_off_loop("land", self._poll_landing_sync, LAND_SWEEP_CAP)

    def _poll_landing_sync(self) -> None:
        with session_scope(self.engine) as db:
            landing = db.exec(
                select(Story).where(
                    Story.status == StoryStatus.LANDING,
                    Story.pr_index.is_not(None),
                    # merge-train members are judged on their BATCH by the
                    # train tick, never on their own (batch) PR fields here
                    Story.train_batch_id.is_(None),
                )
            ).all()
            for story in landing:
                try:
                    _watch_one_landing(db, story)
                except Exception:  # noqa: BLE001 - one story never sinks the sweep
                    log.exception("landing poll failed for story %s", story.id)
            gated = db.exec(
                select(Story).where(
                    Story.status == StoryStatus.HUMAN_REVIEW,
                    Story.pr_index.is_not(None),
                    Story.error.startswith("land failed:"),
                )
            ).all()
            for story in gated:
                try:
                    _watch_one_gate_landing(db, story)
                except Exception:  # noqa: BLE001 - one story never sinks the sweep
                    log.exception("gate land poll failed for story %s", story.id)

    def _release_stale_claims(self) -> None:
        """Auto-release external claims that went silent past LEASE_SILENCE.

        "Last activity" is the story's newest event, falling back to
        claimed_at for legacy rows with no events. Fail open: when neither is
        known we skip the story rather than release on missing data. Events
        and status change commit inside one session_scope; the bus publish
        happens only after the session closes (never before commit —
        phantom-status bug, see routers/stories._commit_and_publish).
        """
        now = utcnow()
        released: list[dict[str, Any]] = []
        with session_scope(self.engine) as db:
            claimed = db.exec(
                select(Story).where(
                    Story.execution == StoryExecution.EXTERNAL,
                    Story.status == StoryStatus.IN_PROGRESS,
                    Story.claimed_by.is_not(None),
                )
            ).all()
            for story in claimed:
                last_event = db.exec(
                    select(StoryEvent.created_at)
                    .where(StoryEvent.story_id == story.id)
                    .order_by(StoryEvent.created_at.desc())
                ).first()
                last = _as_utc(last_event) or _as_utc(story.claimed_at)
                if last is None or now - last <= LEASE_SILENCE:
                    continue
                claimer = story.claimed_by
                payload = release_claim(db, story, by=LEASE_RELEASED_BY)
                record_event(
                    db,
                    story.id,
                    "note",
                    {"reason": LEASE_EXPIRED_NOTE, "by": LEASE_RELEASED_BY, "feedback": False},
                )
                released.append(payload)
                log.info(
                    "auto-released stale external claim on story %s "
                    "(silent since %s, claimer %r)",
                    story.id,
                    as_utc_iso(last),
                    claimer,
                )
        for payload in released:
            bus.publish("story", {"story": payload})

    def _train_ci_gate(self) -> bool:
        """True while a merge-train batch has CI pending: no pickups. Logs
        once per batch on hold and once per batch when the cap lifts it."""
        from . import train as train_module

        try:
            with session_scope(self.engine) as db:
                held = train_module.train_ci_hold(db)
                expired = [
                    (b.id, b.pr_index)
                    for b in train_module.train_ci_hold_expired(db)
                ]
                held_info = (held.id, held.pr_index) if held is not None else None
        except Exception:  # noqa: BLE001 - the hold must never kill the tick
            log.exception("train CI hold check failed")
            return False
        for batch_id, pr_index in expired:
            if batch_id not in self._train_ci_expired_logged:
                self._train_ci_expired_logged.add(batch_id)
                log.warning(
                    "train CI hold lifted after %ss: batch %s PR #%s never reported",
                    train_module.TRAIN_CI_HOLD_CAP_S,
                    batch_id,
                    pr_index,
                )
        if held_info is None:
            return False
        if held_info[0] not in self._train_ci_hold_logged:
            self._train_ci_hold_logged.add(held_info[0])
            log.info(
                "holding pickups: train CI pending on batch %s PR #%s",
                held_info[0],
                held_info[1],
            )
        return True

    async def _wait_for_train_ci(self, story_id: str) -> None:
        """Block a pipeline verify while a train batch's CI runs."""
        from . import train as train_module

        announced = False
        try:
            while True:
                with session_scope(self.engine) as db:
                    held = train_module.train_ci_hold(db)
                    info = (held.id, held.pr_index) if held is not None else None
                if info is None:
                    return
                if not announced:
                    announced = True
                    bus.publish(
                        "story_log",
                        {
                            "story_id": story_id,
                            "text": f"[verify] waiting for train CI batch "
                            f"{info[0][:8]} PR #{info[1]}\n",
                        },
                    )
                    with session_scope(self.engine) as db:
                        record_event(
                            db,
                            story_id,
                            "note",
                            {"waiting_for_train_ci": True, "pr_index": info[1]},
                        )
                self._set_watchdog_floor(story_id, train_module.TRAIN_CI_HOLD_CAP_S)
                await asyncio.sleep(self._tick_interval)
        finally:
            if announced:
                self._clear_watchdog_floor(story_id)

    async def _tick(self, model_slots: int) -> None:
        # the pool size is re-read every tick; shrinking never interrupts a
        # holder, it only blocks new grants
        self._model_pool.resize(model_slots)
        try:
            # Reclaim leases before looking for work: the train CI hold
            # below must not keep stale claims from healing.
            self._release_stale_claims()
        except Exception:  # noqa: BLE001 - a sweep failure must not abort candidates
            log.exception("stale-claim sweep failed")

        # Push+PR stories in `landing` are watched BEFORE pickup: they hold
        # no slot, and a busy board must still land merged PRs.
        try:
            await self._poll_landing()
        except Exception:  # noqa: BLE001 - the poller must never kill the tick
            log.exception("landing poller failed")

        # Failed stories whose branch actually merged self-heal here, under
        # the same conditions as the landing poller.
        try:
            await self._reconcile_failed()
        except Exception:  # noqa: BLE001 - the sweep must never kill the tick
            log.exception("failed-reconcile sweep failed")

        # Merge trains: flush forming batches past their window and judge the
        # open batch PRs. Like the poller, this runs before pickup — a batch
        # PR merging must still land its members.
        try:
            from . import train as train_module

            def _train_sync() -> None:
                with session_scope(self.engine) as db:
                    train_module.train_tick(db)

            await self._sweep_off_loop("train", _train_sync, TRAIN_SWEEP_CAP)
        except Exception:  # noqa: BLE001 - the train must never kill the tick
            log.exception("merge train tick failed")

        self._stamp_slot_waiters()
        self._watchdog_drain()
        await self._watchdog_sweep()
        await self._proc_sweep()
        await self._pod_heartbeat()
        if self._train_ci_gate():
            return
        with session_scope(self.engine) as db:
            # executing stories (base_ref set) continue their coder pipeline;
            # queued stories start the spec pipeline (or re-execute if a
            # previous run got as far as creating the worktree). in_progress/
            # ai_review are normally owned by an in-flight task (excluded via
            # _running) — but after a restart they are orphans and get resumed:
            # the stage guards inside the pipeline route them to the right step.
            # external stories are owned by whoever claimed them: the
            # scheduler never picks them up, not even as restart orphans
            # (in_progress external stories sit in this very candidate set).
            # NULL-safe on purpose — rows written before the execution column
            # existed must keep behaving as internal.
            candidates = db.exec(
                select(Story)
                .where(
                    Story.status.in_(
                        [
                            StoryStatus.QUEUED,
                            StoryStatus.IN_PROGRESS,
                            StoryStatus.EXECUTING,
                            StoryStatus.AI_REVIEW,
                        ]
                    ),
                    or_(
                        Story.execution.is_(None),
                        Story.execution == StoryExecution.INTERNAL,
                        # headless stories are server-run too: same scheduler,
                        # same slots — only the CODE stage differs
                        Story.execution == StoryExecution.HEADLESS,
                    ),
                )
                .order_by(Story.updated_at)
            ).all()
            # effective-executor routing: a story with no override inherits
            # Project.executor, so a queued story in an external project
            # must wait for a claim instead of being picked up here. Resolved
            # read-time on every tick — toggling the project takes effect
            # immediately, including for stories queued before the flip.
            projects = db.exec(select(Project)).all()
            executors = {
                p.id: (p.executor or StoryExecution.INTERNAL) for p in projects
            }
            # the A/B share per project, read on the same tick: the pickup
            # draw below materializes it on still-NULL stories
            ab_shares = {p.id: float(p.ab_headless_share or 0.0) for p in projects}
            candidates = [
                s
                for s in candidates
                if effective_execution(s, executors.get(s.project_id))
                in (StoryExecution.INTERNAL, StoryExecution.HEADLESS)
            ]
            now = time.monotonic()
            self._backoff = {
                sid: slot for sid, slot in self._backoff.items() if slot[0] > now
            }
            self._prune_provider_holds()
            eligible = [
                s
                for s in candidates
                if s.id not in self._running
                and self._deps_met(db, s)
                and (s.id not in self._backoff or self._backoff[s.id][0] <= now)
            ]
            # fairness split: non-queued candidates are restart orphans
            # (in_progress/ai_review/executing not in _running) — they keep
            # priority and resume promptly in updated_at order, exactly as
            # before. Queued stories are ranked by fair_order_key (cost-aware,
            # starvation-bounded) so a steady stream of cheap stories cannot
            # starve an expensive one. Orphans always outrank fresh work.
            orphans = [
                s for s in eligible if s.status != StoryStatus.QUEUED
            ]
            waiting = [
                s for s in eligible if s.status == StoryStatus.QUEUED
            ]
            ordered = sorted(orphans, key=lambda s: s.updated_at) + sorted(
                waiting, key=lambda s: fair_order_key(s, utcnow())
            )
            # every eligible story starts a task, in this order; the tasks
            # then queue on the slot pools (a request queues the moment it
            # asks, so this order is also the first-slot order)
            launches: list[tuple[dict[str, Any], bool]] = []
            for story in ordered:
                self._backoff.pop(story.id, None)
                prev = story.status
                if prev == StoryStatus.QUEUED:
                    # A/B split at pickup — the single chokepoint every path
                    # into `queued` funnels through (the queue endpoint draws
                    # earlier; reject, re-spec, release and rate-limit
                    # requeues only pass here). Fires for NULL execution
                    # only: a pinned override or an already-drawn story is
                    # never re-rolled, inside this very claim transaction.
                    apply_ab_headless(
                        db, story, ab_shares.get(story.project_id, 0.0)
                    )
                is_coder = story.base_ref is not None or prev == StoryStatus.EXECUTING
                if prev == StoryStatus.AI_REVIEW:
                    # orphan resumed mid-review: keep the status so the stage
                    # guards skip finished work (coder -> code review, spec ->
                    # ai review) instead of re-running from the top
                    log.debug("resuming orphan story %s already in ai_review", story.id)
                else:
                    story.status = (
                        StoryStatus.EXECUTING if is_coder else StoryStatus.IN_PROGRESS
                    )
                routed_note: dict[str, Any] | None = None
                if story.model is None:
                    # cost-aware skill routing: cheapest usable model that
                    # meets the requested skill tier. No usable model at all
                    # is a routing problem, not a story failure: revert to
                    # the previous status, leave it queued with the reason on
                    # story.error (written only when the text changes, so
                    # ticks don't spam events) and try the NEXT candidate —
                    # one un-routable skill tier must never block the board.
                    try:
                        resolved = self.registry.resolve_for_skill(
                            story.skill or "medium"
                        )
                    except UnknownModelError as e:
                        message = str(e)
                        story.status = prev
                        if story.error != message:
                            story.error = message
                            story.updated_at = utcnow()
                            db.add(story)
                            record_event(
                                db,
                                story.id,
                                "note",
                                {
                                    "routing_error": message,
                                    "skill": story.skill or "medium",
                                },
                            )
                            bus.publish("story", {"story": story_payload(story)})
                        log.warning(
                            "story %s stays queued: %s", story.id, message
                        )
                        continue
                    story.model = resolved.canonical
                    record_event(
                        db,
                        story.id,
                        "note",
                        {"routed": resolved.canonical, "skill": story.skill or "medium"},
                    )
                else:
                    # the story carries a model (routed earlier or pinned by an
                    # operator): KEEP it unless it is unusable right now. This
                    # is what makes an infrastructure failure non-destructive —
                    # the retry reruns on the same model instead of being
                    # silently re-routed to whatever is cheapest.
                    resolved, reason = self.registry.resolve_usable(story.model)
                    if reason is None:
                        pass  # usable: run on it as-is, no note, no re-route
                    else:
                        # Unusable. Re-route with a floor at the OLD model's
                        # skill tier so a replacement is never cheaper. When
                        # the row is gone (model/provider deleted) its tier is
                        # gone with it and we fall back to the story's own
                        # skill — an operator-pinned high model on a medium
                        # story can then re-route to medium; unavoidable
                        # without storing the tier on the story.
                        floor = (
                            resolved.skill
                            if resolved is not None
                            else (story.skill or "medium")
                        )
                        try:
                            replacement = self.registry.resolve_for_skill(floor)
                        except UnknownModelError as e:
                            message = str(e)
                            story.status = prev
                            if story.error != message:
                                story.error = message
                                story.updated_at = utcnow()
                                db.add(story)
                                record_event(
                                    db,
                                    story.id,
                                    "note",
                                    {
                                        "routing_error": message,
                                        "model": story.model,
                                        "reason": reason,
                                        "skill": floor,
                                    },
                                )
                                bus.publish("story", {"story": story_payload(story)})
                            log.warning(
                                "story %s stays queued: model %s unusable (%s): %s",
                                story.id,
                                story.model,
                                reason,
                                message,
                            )
                            continue
                        old_model = story.model
                        story.model = replacement.canonical
                        # the note is the only place the old name survives:
                        # story.model is overwritten (the replacement sticks)
                        routed_note = {
                            "routed": replacement.canonical,
                            "replaced": old_model,
                            "reason": reason,
                            "skill": floor,
                        }
                        log.warning(
                            "story %s model %s unusable (%s): rerouted to %s "
                            "(skill floor %s)",
                            story.id,
                            old_model,
                            reason,
                            replacement.canonical,
                            floor,
                        )
                if routed_note is not None:
                    record_event(db, story.id, "note", routed_note)
                # provider usage-limit hold: an effective HEADLESS story whose
                # provider is held waits in its previous status (same revert
                # pattern as the routing problems above). Headless only —
                # internal stories talk to the pump, not the CLI, and keep
                # running through the hold.
                if (
                    effective_execution(story, executors.get(story.project_id))
                    == StoryExecution.HEADLESS
                ):
                    provider_id = (
                        story.model.split("/", 1)[0]
                        if story.model and "/" in story.model
                        else ""
                    )
                    hold = self._provider_holds.get(provider_id) if provider_id else None
                    if hold is not None and hold[0] > utcnow():
                        message = (
                            f"provider {provider_id} limited until "
                            f"{hold[0].strftime('%Y-%m-%d %H:%M UTC')}"
                        )
                        story.status = prev
                        if story.error != message:
                            story.error = message
                            story.updated_at = utcnow()
                            db.add(story)
                            record_event(
                                db,
                                story.id,
                                "note",
                                {
                                    "provider_hold": message,
                                    "provider_id": provider_id,
                                    "until": hold[0].isoformat(),
                                },
                            )
                            bus.publish("story", {"story": story_payload(story)})
                        log.info("story %s stays queued: %s", story.id, message)
                        continue
                story.error = None
                story.updated_at = utcnow()
                db.add(story)
                record_event(db, story.id, "status", {"from": prev, "to": story.status})
                launches.append((story_payload(story), is_coder))
            if not launches:
                return
        for payload, is_coder_run in launches:
            self._launch(payload, is_coder_run)

    def _launch(self, payload: dict[str, Any], is_coder_run: bool) -> None:
        bus.publish("story", {"story": payload})
        self._running.add(payload["id"])
        if is_coder_run:
            # the executor object only: its pod is created inside the run's
            # first granted model slot (_coder_slot), never here
            executor = self._make_executor(payload["id"], asyncio.get_running_loop())
            if executor is not None:
                self._executors[payload["id"]] = executor
        # stamp at launch so a run that hasn't emitted its first event yet is
        # never falsely stale (the watchdog measures silence from here)
        self._last_event[payload["id"]] = time.monotonic()
        if is_coder_run:
            task = asyncio.create_task(self._run_coder(payload["id"]))
        else:
            task = asyncio.create_task(self._run_story(payload["id"]))
        # keep the handle: cancel() needs it, and a fire-and-forget task that
        # raises would otherwise surface only as an unretrieved exception
        self._tasks[payload["id"]] = task

    # -- pipeline ------------------------------------------------------

    @staticmethod
    def _close_executor(executor: pods.PodExecutor) -> None:
        """Delete the run's pod off-thread: the API call may block and the
        callers are async stop paths or a finally block."""
        threading.Thread(target=executor.close, daemon=True).start()

    def _make_executor(
        self, story_id: str, loop: asyncio.AbstractEventLoop
    ) -> pods.PodExecutor | None:
        """The verify executor for a run that was just picked up (None =
        in-process). Never raises: a pod problem must not stop the pickup.

        Verify runs in a worker thread and `bus.publish` is not thread-safe
        (asyncio.Queue.put_nowait), so everything the executor emits goes
        through `loop.call_soon_threadsafe`."""
        try:
            settings = load_settings(self.engine)
            mode = str(settings.get("run_executor") or "in_process")
            if mode not in ("verify", "pod"):
                return None

            def story_log(text: str) -> None:
                loop.call_soon_threadsafe(
                    bus.publish, "story_log", {"story_id": story_id, "text": text}
                )

            def progress(chunk: str) -> None:
                loop.call_soon_threadsafe(
                    bus.publish,
                    "story_progress",
                    {"story_id": story_id, "kind": "verify_output", "data": {"text": chunk}},
                )

            return pods.build_executor(
                mode, story_id, settings, story_log=story_log, on_output=progress
            )
        except Exception:  # noqa: BLE001 - fall back to in-process
            log.exception("building the worker pod executor for %s failed", story_id)
            return None

    def _discard_run(self, story_id: str) -> None:
        """Idempotent teardown of one run's bookkeeping. Guarded by task
        identity: a cancelled run that lingers past its 5s bounded wait must
        not clobber the registration of a run that was relaunched in the
        meantime (that would hide it from `_running` and let the next tick
        re-enter it — duplicate coders)."""
        owner = self._tasks.get(story_id)
        if owner is not None and owner is not asyncio.current_task():
            return  # a newer run owns this story id now
        self._tasks.pop(story_id, None)
        pods.release_fallback(story_id)
        self._gated.discard(story_id)
        self._running.discard(story_id)
        self._last_event.pop(story_id, None)
        self._watchdog_floor.pop(story_id, None)
        executor = self._executors.pop(story_id, None)
        if executor is not None:
            self._close_executor(executor)
        # terminal exits only: a requeue (attempt>0 in _backoff) keeps its
        # count so max_provider_retries can bind; a finished/failed/cancelled
        # story drops it so a later manual re-queue starts at attempt 1
        if story_id not in self._backoff:
            self._attempts.pop(story_id, None)

    def _handle_gateway_error(self, story_id: str, e: GatewayError) -> None:
        """Route a provider failure by its classification: rate limits requeue
        unbounded, server errors / timeouts requeue capped, context-too-large
        fails at once (the identical request would fail identically), infra
        fails with its prefix, anything else is a plain failure. The raw
        provider facts ride on the resulting status event."""
        error = str(e)
        kind = classify_provider_error(e)
        facts = _provider_facts(e, kind, self._prompt_tokens.get(story_id))
        if kind == "infra":
            self._fail_infrastructure(story_id, error, facts=facts)
        elif kind == "context_too_large":
            self._fail(story_id, f"context too large: {error}", facts=facts)
        elif kind == "hard":
            self._fail(story_id, error, facts=facts)
        else:
            if kind == "rate_limit":
                reason = "rate limit"
            elif not _has_provider_facts(e):
                reason = "error"  # legacy string-only wording
            else:
                reason = "server error" if kind == "server_error" else "timeout"
            self._requeue_for_rate_limit(
                story_id, error, reason=reason, classification=kind, facts=facts
            )

    def _progress_hook(self, story_id: str, phase: str):
        """on_progress callback: publishes the round activity row AND keeps
        the cumulative prompt tokens for provider-failure facts."""

        def hook(p: dict) -> None:
            tokens = p.get("prompt_tokens")
            if isinstance(tokens, int):
                self._prompt_tokens[story_id] = tokens
            publish_activity(self.engine, story_id, "round", {**p, "phase": phase})

        return hook

    async def _run_story(self, story_id: str) -> None:
        try:
            await self._generate_spec(story_id)
            await self._run_ai_review(story_id)
        except _HeadlessRequeued:
            # a spec session in the worker pod: already requeued (fallback cap
            # full, pod lost) or failed as infrastructure; nothing further runs
            return
        except GatewayError as e:
            self._handle_gateway_error(story_id, e)
        except Exception as e:  # noqa: BLE001 - story must land in failed, not vanish
            log.exception("story %s crashed the runner", story_id)
            self._fail(story_id, f"{type(e).__name__}: {e}")
        finally:
            self._discard_run(story_id)

    async def _fallback_gate(self, story_id: str) -> bool:
        """False when the run was bounced back to the queue because the
        backend node already carries worker_fallback_parallel runs. Only
        applies in-cluster (NODE_NAME set)."""
        if not pods.backend_node():
            return True
        cap = pods.fallback_cap(load_settings(self.engine))
        executor = self._executors.get(story_id)
        kube = None
        if executor is not None:
            try:
                await asyncio.to_thread(executor.prepare)
                return True  # placed on a dev node, or claimed on the backend node
            except pods.FallbackCapFull:
                self._requeue_fallback_wait(story_id, cap)
                return False
            except pods.PodUnavailable:
                pass  # in-process from here on
            kube = executor._kube
        if await asyncio.to_thread(pods.try_claim_fallback, story_id, cap, kube):
            return True
        self._requeue_fallback_wait(story_id, cap)
        return False

    def _requeue_fallback_wait(self, story_id: str, cap: int) -> None:
        """Back to `queued` with a fixed short backoff; never counts as a
        provider attempt or a pod loss."""
        entry_statuses = (
            StoryStatus.IN_PROGRESS,
            StoryStatus.AI_REVIEW,
            StoryStatus.EXECUTING,
        )
        note = f"waiting: dev node full or absent, fallback cap {cap}"
        with session_scope(self.engine) as db:
            story = db.get(Story, story_id)
            if story is None or story.status not in entry_statuses:
                return
            # the newest status event is this run's own pickup (queued ->
            # in flight); the one before it is what put the story back
            recent = db.exec(
                select(StoryEvent.data)
                .where(StoryEvent.story_id == story_id, StoryEvent.kind == "status")
                .order_by(StoryEvent.id.desc())
                .limit(2)
            ).all()
            repeat = False
            for raw in recent:
                try:
                    data = json.loads(raw or "{}")
                except ValueError:
                    continue
                if isinstance(data, dict) and data.get("fallback_cap_wait") is True:
                    repeat = True
            self._backoff[story_id] = (
                time.monotonic() + FALLBACK_WAIT_SECONDS,
                self._attempts.get(story_id, 0),
            )
            prev = story.status
            story.status = StoryStatus.QUEUED
            story.error = None
            story.queued_at = utcnow()
            story.updated_at = utcnow()
            db.add(story)
            record_event(
                db,
                story_id,
                "status",
                {"from": prev, "to": StoryStatus.QUEUED, "note": note, "fallback_cap_wait": True},
            )
            if not repeat:
                db.add(StoryNote(story_id=story_id, body=note, author="fubala"))
            payload = story_payload(story)
        bus.publish("story", {"story": payload})
        log.info("story %s waits for the backend-node fallback cap (%d)", story_id, cap)

    async def _run_coder(self, story_id: str) -> None:
        try:
            # the fallback gate (worker pod / backend-node claim) runs inside
            # the first coder round's model slot: see _coder_slot
            await self._execute_story(story_id)
            await self._run_code_review(story_id)
        except _HeadlessRequeued:
            # already requeued with backoff by the headless path; nothing
            # further may run for this attempt
            return
        except workspaces.WorkspaceError as e:
            self._fail(story_id, str(e))
        except GatewayError as e:
            self._handle_gateway_error(story_id, e)
        except Exception as e:  # noqa: BLE001
            log.exception("story %s crashed the coder", story_id)
            self._fail(story_id, f"{type(e).__name__}: {e}")
        finally:
            self._discard_run(story_id)

    def _watchdog_drain(self) -> None:
        """Non-blocking drain of the watchdog's bus subscription: every
        story_log / story_progress event refreshes that run's timestamp.
        (Drain-time stamping underestimates freshness by at most one poll
        interval — negligible against 15 minutes.)"""
        q = self._watchdog_q
        if q is None:
            return
        while True:
            try:
                raw = q.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                msg = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if msg.get("kind") not in WATCHDOG_ACTIVITY_KINDS:
                continue
            data = msg.get("data") or {}
            story_id = data.get("story_id")
            # only in-flight runs matter: stamping a story that already
            # finished (its last events can still be sitting in the queue at
            # drain time) would leave an entry nothing ever pops
            if (
                isinstance(story_id, str)
                and story_id
                and story_id in self._running
            ):
                self._last_event[story_id] = time.monotonic()

    def _set_watchdog_floor(self, story_id: str, bound_s: float) -> None:
        """Spare a run from the watchdog until bound_s + WATCHDOG_PHASE_MARGIN.

        For phases that are silent on the bus BY DESIGN but bounded on the
        wall clock (the verify subprocess, tool-invoked verifies, guarded
        tool calls, the headless CODE run): the phase surfaces its own
        outcome — rc, timeout, tool error — before this floor expires, so a
        cancellation here would kill a healthy run (the 27 Oct incident:
        850-900s verifies vs a 900s watchdog). The effective limit becomes
        max(WATCHDOG_TIMEOUT, bound + margin). Callers MUST clear in a
        finally; _discard_run and cancel pop leftovers as belt and braces."""
        self._watchdog_floor[story_id] = time.monotonic() + bound_s + WATCHDOG_PHASE_MARGIN

    def _clear_watchdog_floor(self, story_id: str) -> None:
        self._watchdog_floor.pop(story_id, None)

    def _watchdog_floor_stamp(
        self, story_id: str
    ) -> Callable[[float | None], None]:
        """The closure handed to ToolBox / GuardedExecutor (they live in
        other modules and must not know the runner's internals): called with
        a bound in seconds it floors the run, called with None it clears."""
        def stamp(bound_s: float | None) -> None:
            if bound_s is None:
                self._clear_watchdog_floor(story_id)
            else:
                self._set_watchdog_floor(story_id, bound_s)

        return stamp

    async def _watchdog_sweep(self) -> None:
        """Cancel in-flight runs whose event stream has been silent for
        WATCHDOG_TIMEOUT and requeue them. Runs before the max_parallel early
        return so a hung run occupying a slot is checked even at full
        parallelism — otherwise the board would deadlock behind it.

        A run floored by _set_watchdog_floor (inside a phase with its own
        wall-clock bound) is skipped until the floor expires; after that the
        plain silence check applies unchanged. Unguarded phases (gateway
        streaming, agent rounds) are never floored — killing a hung stream
        there is the watchdog's primary job."""
        now = time.monotonic()
        for story_id in list(self._running):
            last = self._last_event.get(story_id)
            if last is None or now - last < WATCHDOG_TIMEOUT:
                continue
            if now < self._watchdog_floor.get(story_id, 0):
                continue
            log.warning(
                "watchdog: story %s stream silent for %.0fs — cancelling run",
                story_id,
                now - last,
            )
            await self.cancel(story_id)
            self._requeue_hung(story_id)

    def _requeue_hung(self, story_id: str) -> None:
        """Watchdog counterpart of _requeue_for_rate_limit, minus backoff: the
        run was hung, not throttled, so the next tick picks it straight back
        up."""
        entry_statuses = (
            StoryStatus.IN_PROGRESS,
            StoryStatus.AI_REVIEW,
            StoryStatus.EXECUTING,
        )
        with session_scope(self.engine) as db:
            story = db.get(Story, story_id)
            if story is None or story.status not in entry_statuses:
                self._last_event.pop(story_id, None)
                return
            prev = story.status
            story.status = StoryStatus.QUEUED
            story.error = None
            story.queued_at = utcnow()
            story.updated_at = utcnow()
            db.add(story)
            record_event(
                db,
                story_id,
                "status",
                {
                    "from": prev,
                    "to": StoryStatus.QUEUED,
                    "note": "watchdog: stream hung, requeued",
                },
            )
            payload = story_payload(story)
        bus.publish("story", {"story": payload})
        self._last_event.pop(story_id, None)
        log.info("watchdog: story %s requeued after hung stream", story_id)

    def _still_this_run(self, story_id: str, executor: pods.PodExecutor) -> bool:
        """False once cancel() took the story over: it drops the executor and
        the running mark before it closes the pod."""
        return self._executors.get(story_id) is executor and story_id in self._running

    def _pod_lost_requeues(self, story_id: str) -> int:
        """Pod-loss requeues so far, counted from the status events so the
        count survives a restart."""
        return self._status_flag_count(story_id, "pod_lost_requeue")

    def _status_flag_count(self, story_id: str, flag: str) -> int:
        with session_scope(self.engine) as db:
            rows = db.exec(
                select(StoryEvent.data).where(
                    StoryEvent.story_id == story_id, StoryEvent.kind == "status"
                )
            ).all()
        count = 0
        for raw in rows:
            try:
                data = json.loads(raw or "{}")
            except ValueError:
                continue
            if isinstance(data, dict) and data.get(flag) is True:
                count += 1
        return count

    def _requeue_pod_lost(
        self, story_id: str, cause: str, executor: pods.PodExecutor
    ) -> bool:
        """Put a story whose worker pod was lost mid-run back in `queued`,
        once (POD_LOST_REQUEUE_CAP). False, with nothing changed, when the
        story is no longer this run's (a cancel closes the pod, which raises
        the same PodRunLost) or the cap is used."""
        entry_statuses = (
            StoryStatus.IN_PROGRESS,
            StoryStatus.AI_REVIEW,
            StoryStatus.EXECUTING,
        )
        if not self._still_this_run(story_id, executor):
            return False
        attempt = self._pod_lost_requeues(story_id) + 1
        if attempt > POD_LOST_REQUEUE_CAP:
            return False
        note = redact_secrets(
            f"worker pod lost mid-run: {cause}; requeued {attempt}/{POD_LOST_REQUEUE_CAP}"
        )
        with session_scope(self.engine) as db:
            story = db.get(Story, story_id)
            if story is None or story.status not in entry_statuses:
                return False
            prev = story.status
            story.status = StoryStatus.QUEUED
            story.error = None
            story.queued_at = utcnow()
            story.updated_at = utcnow()
            db.add(story)
            record_event(
                db,
                story_id,
                "status",
                {
                    "from": prev,
                    "to": StoryStatus.QUEUED,
                    "note": note,
                    "pod_lost_requeue": True,
                    "attempt": attempt,
                },
            )
            db.add(StoryNote(story_id=story_id, body=note[:8192], author="fubala"))
            payload = story_payload(story)
        bus.publish("story", {"story": payload})
        log.info("story %s requeued after worker pod loss (%d)", story_id, attempt)
        return True

    # -- provider usage-limit holds -------------------------------------

    def _prune_provider_holds(self) -> None:
        """Drop expired holds; cheap enough to call from every tick."""
        now = utcnow()
        for pid, (until, _reason) in list(self._provider_holds.items()):
            if until <= now:
                del self._provider_holds[pid]

    def set_provider_hold(
        self, provider_id: str, reset_at: datetime | None, reason: str
    ) -> datetime:
        """Hold the provider until the stated usage-limit reset time.

        A stated reset is only trusted when plausible (in the future, at most
        24h out) and is capped at the provider's quota window; anything else
        falls back to PROVIDER_HOLD_FALLBACK_SECONDS. Returns the hold's
        expiry (UTC) — the caller reports it in the story note.
        """
        now = utcnow()
        until = now + timedelta(seconds=PROVIDER_HOLD_FALLBACK_SECONDS)
        if reset_at is not None and now < reset_at <= now + timedelta(hours=24):
            until = min(reset_at, now + timedelta(seconds=PROVIDER_HOLD_MAX_SECONDS))
        self._provider_holds[provider_id] = (until, reason)
        return until

    def provider_holds(self) -> list[dict[str, Any]]:
        """Active holds, oldest first (GET /providers/limits)."""
        self._prune_provider_holds()
        return [
            {
                "provider_id": pid,
                "until": until.isoformat(),
                "reason": reason,
            }
            for pid, (until, reason) in sorted(
                self._provider_holds.items(), key=lambda kv: kv[1][0]
            )
        ]

    def clear_provider_hold(self, provider_id: str) -> bool:
        """Operator escape hatch: forget the hold so pickup resumes at the
        next tick. False when no hold was active."""
        return self._provider_holds.pop(provider_id, None) is not None

    def _requeue_for_rate_limit(
        self,
        story_id: str,
        error: str,
        reason: str = "rate limit",
        *,
        classification: str | None = None,
        facts: dict | None = None,
    ) -> None:
        """Provider rate limit or transient gateway error: don't fail the story
        — requeue with backoff (5 min doubling to 30 max) and let it finish when
        the window opens. Bounded: past `max_provider_retries` attempts the
        story fails with the last error, cascading to dependents like any
        permanent failure."""
        if classification is None:
            classification = (
                "rate_limit" if _is_rate_limit_error(error) else "server_error"
            )
        attempt = self._attempts.get(story_id, 0) + 1
        max_retries = load_settings(self.engine).get("max_provider_retries")
        try:
            max_retries = int(max_retries)
        except (TypeError, ValueError):
            max_retries = 5
        entry_statuses = (
            StoryStatus.IN_PROGRESS,
            StoryStatus.AI_REVIEW,
            StoryStatus.EXECUTING,
        )
        give_up = False
        with session_scope(self.engine) as db:
            story = db.get(Story, story_id)
            # the early-return guard runs before the cap-fail too: a story
            # that is no longer in flight is never requeued nor failed here
            if story is None or story.status not in entry_statuses:
                return
            # rate limits (provider quota windows can last hours) keep the
            # patient unbounded requeue; the cap binds transport/5xx only
            if attempt > max_retries and classification != "rate_limit":
                # give up: drop the backoff slot but KEEP the attempt counter
                # — _fail's cascade must not requeue this story if a late
                # transient error lands after the give-up (it would restart
                # the count and loop forever past the cap)
                self._backoff.pop(story_id, None)
                give_up = True
            else:
                self._attempts[story_id] = attempt
                delay = min(300 * 2 ** (attempt - 1), 1800)
                self._backoff[story_id] = (time.monotonic() + delay, attempt)
                prev = story.status
                story.status = StoryStatus.QUEUED
                story.error = None
                story.queued_at = utcnow()
                story.updated_at = utcnow()
                db.add(story)
                record_event(
                    db,
                    story_id,
                    "status",
                    {
                        "from": prev,
                        "to": StoryStatus.QUEUED,
                        "note": f"provider {reason}; retry in ~{delay // 60} min "
                        f"(attempt {attempt})",
                        "attempt": attempt,
                        **({"provider": facts} if facts else {}),
                    },
                )
                payload = story_payload(story)
        if give_up:
            self._fail(story_id, error, facts=facts)
            return
        bus.publish("story", {"story": payload})
        log.info(
            "story %s %s; backoff %ss (attempt %s)", story_id, reason, delay, attempt
        )

    def _fail(self, story_id: str, error: str, *, facts: dict | None = None) -> None:
        """Failure can strike in any stage, so accept any in-flight status."""
        self._finish(
            story_id,
            StoryStatus.FAILED,
            (
                StoryStatus.IN_PROGRESS,
                StoryStatus.AI_REVIEW,
                StoryStatus.EXECUTING,
            ),
            error=error,
            provider=facts,
        )
        self._cascade_failure(story_id, error)

    def _fail_infrastructure(
        self, story_id: str, error: str, *, facts: dict | None = None
    ) -> None:
        """A provider/credential failure (missing key, auth rejected, dead
        endpoint): fail with the "infrastructure: " prefix and tag the run
        with an infra-marked note event carrying the model.

        story.model is KEPT: an operator retry at stage=queued re-runs on the
        same model (the scheduler only replaces it when the registry says it
        is unusable, and then never with a cheaper skill tier). Clearing it
        here used to make every retry silently downgrade to whatever was
        cheapest. The spec itself is untouched; dependencies cascade like any
        other failure."""
        model: str | None = None
        with session_scope(self.engine) as db:
            story = db.get(Story, story_id)
            if story is None:
                return
            model = story.model
        prefixed = f"infrastructure: {error}"
        self._finish(
            story_id,
            StoryStatus.FAILED,
            (
                StoryStatus.IN_PROGRESS,
                StoryStatus.AI_REVIEW,
                StoryStatus.EXECUTING,
            ),
            error=prefixed,
            provider=facts,
        )
        with session_scope(self.engine) as db:
            story = db.get(Story, story_id)
            if story is not None:
                record_event(
                    db,
                    story_id,
                    "note",
                    {
                        "infra": True,
                        "model": model,
                        "error": error,
                        **({"provider": facts} if facts else {}),
                    },
                )
                payload = story_payload(story)
                bus.publish("story", {"story": payload})
        self._cascade_failure(story_id, prefixed)
        log.warning(
            "story %s failed on infrastructure error (model %s): %s",
            story_id,
            model,
            error,
        )

    def _cascade_failure(self, story_id: str, error: str) -> None:
        """A failed prerequisite fails its waiting dependents; stories already
        in flight keep running (they started with the dep done). Processes
        breadth-first, one session per level, to avoid nested transactions."""
        frontier = [story_id]
        seen: set[str] = set()
        while frontier:
            current_id = frontier.pop(0)
            payloads = []
            with session_scope(self.engine) as db:
                failed = db.get(Story, current_id)
                if failed is None:
                    continue
                waiting = db.exec(
                    select(Story).where(
                        Story.status.in_([StoryStatus.BACKLOG, StoryStatus.QUEUED])
                    )
                ).all()
                for candidate in waiting:
                    if candidate.id in seen:
                        continue
                    if current_id not in json.loads(candidate.depends_on or "[]"):
                        continue
                    seen.add(candidate.id)
                    prev = candidate.status
                    candidate.status = StoryStatus.FAILED
                    candidate.error = f"dependency failed: {failed.title}"
                    candidate.updated_at = utcnow()
                    db.add(candidate)
                    record_event(
                        db,
                        candidate.id,
                        "status",
                        {"from": prev, "to": candidate.status, "error": candidate.error},
                    )
                    payloads.append(story_payload(candidate))
                    frontier.append(candidate.id)
            for payload in payloads:
                bus.publish("story", {"story": payload})

    async def _generate_spec(self, story_id: str) -> None:
        with session_scope(self.engine) as db:
            story = db.get(Story, story_id)
            if story is None or story.status != StoryStatus.IN_PROGRESS:
                return
            feedback = self._reject_feedback(db, story_id)
            settings = load_settings(self.engine)
            proj_root, _, proj_id = self._project_roots(story.project_id)
            spec_root = proj_root or settings.get("project_root")
            # push delivery: the spec writer reads this root, so it must be
            # current with the remote default branch first
            await self._sync_push_root_async(proj_id, story_id, spec_root)
            context = context_files_block(settings, [spec_root])
            # project brief opt-in: ONLY Owner rules + Non-goals + the epic
            # description; "" (prompt byte-identical) when the flag is off
            brief_block, brief_version = spec_prompt_block(db, proj_id, story.epic_id)
            system_prompt = "\n\n".join(
                p
                for p in (
                    effective_prompt(self.engine, "spec_writer_prompt"),
                    context,
                    brief_block,
                )
                if p
            )
            user_prompt = _build_user_prompt(
                story,
                [f"{item.reason} (from {label})" for item, label in _labels(feedback)],
                last_spec=story.spec,
            )
            story_model = story.model
            # captured while the session is open: the instance is detached
            # by the time the (long) agent call below returns
            story_kind = story.kind or StoryKind.CODE

        # the spec phase has its own model setting; empty = the story's model
        model, phase_error = self._phase_model(settings, "spec_model", story_model)
        if phase_error is not None or model is None:
            self._fail(story_id, phase_error or "spec_model: no model")
            return
        bus.publish(
            "story_log", {"story_id": story_id, "text": f"[spec] using model {model}\n"}
        )

        # the spec writer is a grounded agent: it may inspect the STORY'S
        # project and the board (read-only tools) before writing the spec —
        # never the global project root, which is a different repo
        settings = load_settings(self.engine)
        policy = ToolPolicy.from_settings(settings)
        toolbox = ToolBox(
            engine=self.engine,
            project_root=spec_root,
            deny_globs=policy.denied_patterns(),
            prep_parent_id=story_id if story_kind == StoryKind.PREP else None,
        )
        schemas = toolbox.schemas() if policy.spec_tools_enabled else []
        guarded = GuardedExecutor(
            toolbox, policy, watchdog_floor=self._watchdog_floor_stamp(story_id)
        )
        spec_messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        cli_target = self._claude_code_target(model) if schemas else None
        spec_effort = self._cli_effort(model, settings.get("spec_effort"))
        self._record_phase(story_id, "spec", model, spec_effort)
        # one model slot per spec call; a pod spec session builds its pod
        # (and graph) only inside it
        async with self._model_slot(story_id):
            if cli_target is not None:
                # Claude Max: the spec writer is one Claude Code session in the
                # project root with read-only built-ins (Read, Grep, Glob), not
                # Fubala's tool loop (the OAuth token only works via the CLI)
                result = await self._run_spec_cli(
                    story_id, cli_target, spec_messages, spec_root, settings, policy,
                    spec_effort,
                )
            else:
                result = await self._run_spec_agent(
                    story_id, model, spec_messages, schemas, guarded, settings, policy,
                    spec_effort,
                )

        # Deliverable guard: a thinking-only phase must not pass stale tool-
        # round narration off as the spec. Incomplete runs and texts missing
        # the mandatory sections get ONE corrective nudge; still bad → the
        # story fails and never reaches AI review.
        spec_text = result.text
        if result.complete and _deliverable_ok(spec_text, story_kind):
            spec_text = self._with_child_section(story_id, story_kind, spec_text)
            spec_text = _stamp_brief_version(spec_text, brief_version)
            self._finish(
                story_id,
                StoryStatus.AI_REVIEW,
                StoryStatus.IN_PROGRESS,
                spec=spec_text,
                verify_result=(
                    # info/prep stories have no verify stage — stamping n/a here
                    # is atomic with the AI_REVIEW transition, so the outcome
                    # panel never shows a phantom "verify failed" for a story
                    # that was never meant to run code. `applicable: false` (no
                    # `passed` key) is checked BEFORE any !passed logic.
                    json.dumps({"applicable": False})
                    if story_kind != StoryKind.CODE
                    else None
                ),
            )
            return

        corrective = (
            "[system] Your previous output was not a usable spec: it was "
            "empty, truncated by the output limit, or missing mandatory "
            "sections. Write the full spec now, with sections "
            + ", ".join(REQUIRED_SPEC_SECTIONS)
            + " as markdown headings. Do not narrate; emit the spec itself."
        )
        bus.publish(
            "story_log",
            {"story_id": story_id, "text": "[spec] incomplete spec; nudging once\n"},
        )
        self._record_phase(story_id, "spec_nudge", model, spec_effort)
        nudge_messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
            {"role": "assistant", "content": spec_text or "(no output)"},
            {"role": "user", "content": corrective},
        ]
        async with self._model_slot(story_id):
            if cli_target is not None and self._spec_pod(story_id, spec_root) is not None:
                # the main session ran in the worker pod: so does the nudge
                text, usage = await self._spec_cli_session(
                    story_id, cli_target, nudge_messages, spec_root, settings,
                    cli_tools="", max_turns=None,
                    timeout=float(policy.timeout_seconds or 600),
                    effort=spec_effort, spec_tag=f"{story_id}-spec",
                )
                second = AgentResult(
                    text=text,
                    usage=usage,
                    rounds=1,
                    tool_calls=0,
                    complete=(usage.finish_reason or "") != "length",
                )
            else:
                second = await run_agent(
                    self.gateway,
                    model=model,
                    messages=nudge_messages,
                    tools=None,
                    max_output_tokens=_phase_budget(settings, "spec_max_output_tokens"),
                    max_output_tokens_cap=int(
                        settings.get("agent_max_output_tokens_cap", 32768) or 0
                    )
                    or None,
                    effort=spec_effort,
                )
        if second.complete and _deliverable_ok(second.text, story_kind):
            self._finish(
                story_id,
                StoryStatus.AI_REVIEW,
                StoryStatus.IN_PROGRESS,
                spec=_stamp_brief_version(
                    self._with_child_section(story_id, story_kind, second.text),
                    brief_version,
                ),
                verify_result=(
                    json.dumps({"applicable": False})
                    if story_kind != StoryKind.CODE
                    else None
                ),
            )
            return

        bus.publish(
            "story_log",
            {
                "story_id": story_id,
                "text": "[spec] no usable spec after corrective nudge; failing\n",
            },
        )
        self._fail(story_id, "spec phase produced no spec")

    def _with_child_section(self, story_id: str, kind: str, text: str) -> str:
        """Prep runs: append (or replace) a deterministic `## Child stories`
        section listing every story whose depends_on contains this id."""
        if kind != StoryKind.PREP:
            return text
        with session_scope(self.engine) as db:
            children = [
                (c.id, c.title)
                for c in db.exec(select(Story)).all()
                if story_id in json.loads(c.depends_on or "[]")
            ]
        body = text.rstrip()
        match = re.search(r"^## Child stories[ \t]*$", body, flags=re.MULTILINE)
        if match:
            rest = re.search(r"^## ", body[match.end():], flags=re.MULTILINE)
            tail = body[match.end() + rest.start():] if rest else ""
            body = body[: match.start()].rstrip() + ("\n\n" + tail.strip() if tail else "")
        if not children:
            return body
        lines = [f"- {cid}: {title}" for cid, title in children]
        return body + "\n\n## Child stories\n" + "\n".join(lines) + "\n"

    def _claude_code_target(self, model: str | None):
        """(resolved model, OAuth token) when `model` belongs to a claude_code
        (Claude Max) provider, else None: those providers cannot run
        Fubala's tool loop and use the CLI instead."""
        try:
            resolved = self.registry.resolve(model or "")
        except UnknownModelError:
            return None
        if resolved.provider.kind != "claude_code":
            return None
        return resolved, self.registry.effective_key(resolved.provider)

    def _phase_model(
        self, settings: dict, key: str, story_model: str | None
    ) -> tuple[str | None, str | None]:
        """(model, error) for a phase with its own model setting (`spec_model`,
        `review_model`). An empty setting means the story's model. A set one
        must be usable right now; otherwise the error names the setting, the
        model and the reason, and the caller fails the story. Never a silent
        fallback to another model."""
        wanted = (settings.get(key) or "").strip()
        if not wanted:
            return story_model, None
        _, reason = self.registry.resolve_usable(wanted)
        if reason is not None:
            return None, f"{key} {wanted} is not usable: {reason}"
        return wanted, None

    def _cli_effort(self, model: str | None, effort: str | None) -> str | None:
        """The --effort value to hand a phase, or None: empty settings and
        every non-claude_code model send nothing (their argv and gateway calls
        stay untouched)."""
        effort = (effort or "").strip()
        if not effort or self._claude_code_target(model) is None:
            return None
        return effort

    def _record_phase(
        self, story_id: str, phase: str, model: str | None, effort: str | None
    ) -> None:
        """One note per phase run naming the model and the effort sent."""
        with session_scope(self.engine) as db:
            record_event(
                db,
                story_id,
                "note",
                {"phase": phase, "model": model, "effort": effort},
            )

    async def _run_spec_cli(
        self, story_id, target, messages, spec_root, settings, policy, effort=None
    ) -> AgentResult:
        spec_tag = f"{story_id}-spec"
        if spec_root and str(settings.get("run_executor") or "") == "pod":
            # the session runs in a worker pod placed like the coder's, built
            # lazily here so specs that are no CLI session never get a pod
            if story_id not in self._executors:
                executor = self._make_executor(story_id, asyncio.get_running_loop())
                if executor is not None:
                    self._executors[story_id] = executor
            if not await self._fallback_gate(story_id):
                raise _HeadlessRequeued()  # requeued: the dev node is full
        try:
            text, usage = await self._spec_cli_session(
                story_id, target, messages, spec_root, settings,
                cli_tools=READ_ONLY_CLI_TOOLS,
                max_turns=(policy.max_tool_calls or 40) + 1,
                timeout=float(policy.timeout_seconds or 600),
                effort=effort,
                spec_tag=spec_tag,
            )
        finally:
            code_graph.cleanup(spec_tag)
        bus.publish("story_log", {"story_id": story_id, "text": text})
        return AgentResult(
            text=text,
            usage=usage,
            rounds=1,
            tool_calls=0,
            complete=(usage.finish_reason or "") != "length",
        )

    def _spec_pod(self, story_id: str, spec_root) -> pods.PodExecutor | None:
        """The run's executor when a spec CLI session may run in its pod."""
        executor = self._executors.get(story_id)
        if executor is not None and executor.mode == "pod" and not executor.disabled and spec_root:
            return executor
        return None

    async def _spec_cli_session(
        self, story_id, target, messages, spec_root, settings, *,
        cli_tools, max_turns, timeout, effort, spec_tag,
    ) -> tuple[str, Any]:
        """One spec-phase `claude -p` session: in the run's worker pod when
        there is one, else (no pod, or PodUnavailable before the exec started)
        as a local child process. PodRunLost after it started is never rerun
        locally: the first loss requeues the story, a second fails it."""
        from .providers import claude_code as cc

        resolved, key = target
        max_output_tokens = _phase_budget(settings, "spec_max_output_tokens")
        pod_ex = self._spec_pod(story_id, spec_root)
        if pod_ex is not None:
            graph_on = bool(cli_tools) and self._code_graph_project(story_id)[1]
            argv, env, prompt = cc.build_cli_session(
                resolved, key, messages, cli_tools=cli_tools, max_turns=max_turns,
                max_output_tokens=max_output_tokens, effort=effort,
                # the pod builds its own graph under /tmp
                mcp_config=code_graph.pod_mcp_config_json(pods.REPO_DIR) if graph_on else None,
            )
            # the exec is bounded by timeout + margin, plus the snapshot sync
            self._set_watchdog_floor(story_id, timeout + pods.EXEC_MARGIN + 300.0)
            try:
                rc, out, err = await asyncio.to_thread(
                    pod_ex.run_cli_session, argv, env, prompt, timeout, Path(spec_root)
                )
            except pods.PodUnavailable:
                # before the exec started: run locally (logged once by the pod),
                # unless the fallback cap is 0
                if pods.local_fallback_disabled(settings):
                    self._requeue_fallback_wait(story_id, 0)
                    raise _HeadlessRequeued() from None
            except pods.PodRunLost as e:
                if self._requeue_pod_lost(story_id, str(e), pod_ex):
                    raise _HeadlessRequeued() from None
                if self._still_this_run(story_id, pod_ex):
                    self._fail_infrastructure(story_id, f"worker pod lost mid-run: {e}")
                raise _HeadlessAborted() from None
            else:
                if rc == 124:
                    raise GatewayError(
                        f"[{resolved.provider.id}] claude CLI timed out after {timeout:.0f}s"
                    )
                return cc.parse_cli_result(resolved.provider.id, key, rc, out, err)
            finally:
                self._clear_watchdog_floor(story_id)
        mcp_config = (
            await self._graph_mcp(story_id, spec_root, spec_tag)
            if spec_root and cli_tools
            else None
        )
        return await cc.run_cli_agent(
            resolved, key, messages, cwd=spec_root, cli_tools=cli_tools,
            max_turns=max_turns, max_output_tokens=max_output_tokens,
            timeout=timeout, effort=effort, mcp_config=mcp_config,
        )

    async def _run_spec_agent(
        self, story_id, model, spec_messages, schemas, guarded, settings, policy,
        effort=None,
    ) -> AgentResult:
        return await run_agent(
            self.gateway,
            model=model,
            messages=spec_messages,
            tools=schemas or None,
            execute=guarded,
            on_delta=lambda text: bus.publish(
                "story_log", {"story_id": story_id, "text": text}
            ),
            on_tool=activity_tool_hook(self.engine, story_id),
            on_progress=self._progress_hook(story_id, "spec"),
            max_tool_calls=policy.max_tool_calls,
            timeout_seconds=policy.timeout_seconds,
            max_output_tokens=_phase_budget(settings, "spec_max_output_tokens"),
            max_output_tokens_cap=int(
                settings.get("agent_max_output_tokens_cap", 32768) or 0
            )
            or None,
            effort=effort,
        )

    @staticmethod
    def _deps_met(db: Session, story: Story) -> bool:
        for dep_id in json.loads(story.depends_on or "[]"):
            dep = db.get(Story, dep_id)
            if dep is None or dep.status != StoryStatus.DONE:
                return False
        return True

    def _project_roots(self, project_id: str | None) -> tuple[str | None, list[str], str | None]:
        """(root, extra_roots, project_id) for a story's project."""
        if not project_id:
            return None, [], None
        with Session(self.engine) as db:
            project = db.get(Project, project_id)
            if project is None:
                return None, [], None
            extras = json.loads(project.extra_paths or "[]")
            return project.root, extras, project.id

    def _code_graph_project(self, story_id: str) -> tuple[str | None, bool]:
        """(project id, code_graph_enabled) of a story's project; NULL = off."""
        with Session(self.engine) as db:
            story = db.get(Story, story_id)
            project_id = story.project_id if story else None
            project = db.get(Project, project_id) if project_id else None
            return project_id, bool(project and project.code_graph_enabled)

    async def _graph_mcp(
        self, story_id: str, worktree: Path | str, tag: str | None = None
    ) -> str | None:
        """Inline --mcp-config JSON for a CLI session in `worktree`, or None
        (setting off, or the bounded graph build failed: the session runs
        without MCP)."""
        _, enabled = self._code_graph_project(story_id)
        if not enabled:
            return None
        tree = Path(worktree)
        out = await asyncio.to_thread(code_graph.build, tree, code_graph.graph_dir(tag or story_id))
        if out is None:
            bus.publish(
                "story_log",
                {"story_id": story_id, "text": "[code-graph] build failed; running without MCP\n"},
            )
            return None
        return code_graph.mcp_config_json(tree, out)

    def _flash_model_for_provider(self, resolved) -> str | None:
        """The provider's cheapest (low-skill) model id, for the headless
        CLI's ANTHROPIC_SMALL_FAST_MODEL. Canonical form when found, else
        None (the headless module then falls back to `<model>-flash`)."""
        try:
            from .registry import TIERS  # noqa: F401  (tier names, unused)

            with Session(self.engine) as db:
                rows = db.exec(
                    select(ProviderModel).where(
                        ProviderModel.provider_id == resolved.provider.id,
                        ProviderModel.skill == "low",
                    )
                ).all()
            for row in rows:
                return f"{resolved.provider.id}/{row.model_id}"
        except Exception:  # noqa: BLE001 - a hint, never a hard dependency
            return None
        return None

    # ---- push delivery: keep the project root current with the remote ------
    #
    # A push project's stories are specced, coded and verified against the
    # project ROOT checkout, but the truth lives on the remote default branch
    # (root branch may be fubala/integration while origin/main advances). A
    # root left behind makes every story stale before it starts, so the runner
    # re-syncs it at the three points where staleness would leak in: spec
    # entry, worktree creation, and take-in-before-verify. Best-effort by
    # contract: a root that cannot be brought current is warned about (a note
    # authored "fubala", excluded from reject feedback) and the story runs on
    # local HEAD anyway — a remote problem never fails a story.

    def _sync_push_root(self, project_id: str | None, story_id: str, project_root: str) -> None:
        """Fetch + fast-forward the root, but only for push projects.

        merge-delivery projects never touch the network here: their root is
        the workspace of record, and an unexpected fetch/ff would be a
        behaviour change nobody asked for."""
        if not project_id or not project_root:
            return
        with Session(self.engine) as db:
            project = db.get(Project, project_id)
            if project is None or (
                project.delivery or ProjectDelivery.MERGE
            ) != ProjectDelivery.PUSH:
                return
        outcome = None
        try:
            outcome = workspaces.sync_root(project_root)
        except Exception as e:  # sync_root never raises, but never say never
            outcome = workspaces.SyncOutcome("fetch_failed", f"sync crashed: {e}")
        if outcome.kind in ("ff", "up_to_date"):
            if outcome.kind == "ff":
                bus.publish(
                    "story_log",
                    {
                        "story_id": story_id,
                        "text": f"[push-sync] root fast-forwarded to {outcome.new[:12]}\n",
                    },
                )
            return
        message = f"push-sync: {outcome.message or outcome.kind}"
        with session_scope(self.engine) as db:
            record_event(db, story_id, "note", {"warning": message, "by": "fubala"})
        bus.publish(
            "story_log", {"story_id": story_id, "text": f"[push-sync] {message}\n"}
        )

    async def _sync_push_root_async(
        self, project_id: str | None, story_id: str, project_root: str
    ) -> None:
        """Off-the-event-loop wrapper: sync is git I/O (fetch can take the
        full 60s _git timeout on a slow remote)."""
        await asyncio.to_thread(self._sync_push_root, project_id, story_id, project_root)

    async def _execute_story(self, story_id: str) -> None:
        """The coder agent: spec in, working tree changes out (in a worktree)."""
        settings = load_settings(self.engine)
        policy = ToolPolicy.from_settings(settings)
        project_root = settings.get("project_root")
        if not policy.write_tools_enabled:
            raise workspaces.WorkspaceError(
                "execution is disabled — enable write tools in Settings → Application"
            )
        if not project_root:
            raise workspaces.WorkspaceError(
                "no project root configured — set one in Settings → Application"
            )

        with session_scope(self.engine) as db:
            story = db.get(Story, story_id)
            if story is None or story.status != StoryStatus.EXECUTING or not story.spec:
                return
            # per-story budget override (set at the spec gate): a big story's
            # coder may need more tool rounds than the app default. In-place,
            # before the toolbox/guarded below read it; None = inherit the
            # app's tools_max_calls setting. Spec/chat runs are untouched.
            if story.max_tool_calls is not None:
                policy.max_tool_calls = max(1, story.max_tool_calls)
            proj_root, proj_extras, proj_id = self._project_roots(story.project_id)
            project_root = proj_root or project_root
            # push delivery: bring the root current BEFORE forking a worktree
            # off it, or the story starts on stale code
            await self._sync_push_root_async(proj_id, story_id, project_root)
            # git worktree add/del is seconds-slow: off the loop (thread), see
            # test_event_loop_responsiveness.py
            workspace_path, base, created_new = await asyncio.to_thread(
                workspaces.create, project_root, story_id
            )
            if created_new or not story.base_ref:
                # pin the fork point on first attempt — and re-pin whenever
                # the worktree was rebuilt (legacy cleanup, manual removal):
                # a stale pin would show already-merged main commits as the
                # coder's output (fubala-prompts/005)
                story.base_ref = base
            # a code-gate reject pins the branch head it rejected: the rerun
            # must prove it reacted (>=1 new non-merge commit) or park. Read
            # before any mutation of this round's history.
            code_reject_head = story.code_reject_head
            story.updated_at = utcnow()
            db.add(story)
            # the latest rejection reason (code reviewer or human), so a
            # retried coder run starts from the feedback instead of repeating
            # the same mistake. Read BEFORE this run records its own worktree
            # note: that note is the newest one once the session flushes, and
            # reading after it hid every rejection from the coder (23 Oct,
            # ems c6548d63 / 6c937c2e: three reworks ignored their feedback).
            prev_feedback = _coder_feedback(db, story_id)
            binding_notes = _binding_feedback_notes(db, story_id)
            if binding_notes:
                record_event(
                    db, story_id, "note",
                    {"coder_saw_feedback": [n[0] for n in binding_notes], "feedback": False},
                )
            record_event(db, story_id, "note", {"worktree": str(workspace_path), "base": base})
            context = context_files_block(
                settings, [str(workspace_path), project_root]
            )
            system_prompt = "\n\n".join(p for p in (CODER_PROMPT.read_text(), context) if p)
            story_title, spec = story.title, story.spec
            if prev_feedback is None:
                # operator feedback may also be a plain note (as_feedback on
                # POST /notes): newest local note only — agent STATE logs
                # would flood the prompt
                local_note = db.exec(
                    select(StoryNote)
                    .where(StoryNote.story_id == story_id, StoryNote.author == "local")
                    .order_by(StoryNote.id.desc())
                ).first()
                if local_note is not None:
                    prev_feedback = f"(from {local_note.author}): {local_note.body}"
            # the human's code-gate objections, for the no-op retry round and
            # (later) the code review's per-point checklist
            reject_reasons = _code_gate_reasons(db, story_id) if code_reject_head else []
            prompt_parts = [
                "=== CODE REQUEST ===",
                f"# Story: {story.title}",
                "## Spec to implement",
                story.spec,
            ]
            if binding_notes:
                prompt_parts.append(_binding_notes_section(binding_notes))
            # operator steering written after the spec was approved, verbatim
            # and author/time-labelled: a coordinator can redirect the coder
            # mid-flight without rejecting (which would reset the pipeline).
            # Primary steering, so it sits right under the spec, before any
            # rework feedback. Capped at 8000 chars keeping the newest notes.
            operator_notes = _without_binding(
                _operator_notes_since(db, story_id), binding_notes
            )
            if operator_notes:
                prompt_parts.append(_coder_operator_notes_section(operator_notes))
            if prev_feedback:
                prompt_parts += [
                    "## Previous attempt feedback (fix this too)",
                    prev_feedback,
                ]
            # every human code-gate objection since the spec was approved,
            # verbatim: a later one-line AI rejection must not hide the
            # human's numbered points
            human_reasons = [r for r in _code_gate_reasons(db, story_id)
                             if not (prev_feedback and prev_feedback.endswith(f": {r}"))]
            if human_reasons:
                prompt_parts += [
                    "## Human reject reasons (every point must be addressed)",
                    *(f"{i}. {r}" for i, r in enumerate(human_reasons, 1)),
                ]
            story_project_id = story.project_id
            # resolved for BOTH executors: the pump exposes run_tests /
            # run_verify through the toolbox, the headless path names the
            # concrete commands in its prompt rules.
            verify_command, verify_timeout = resolve_verify_command(db, story_project_id)
            test_command = resolve_test_command(db, story_project_id)
            # the effective executor for this story's CODE stage, resolved
            # read-time like every other path (override > project > internal)
            project_executor_value: str | None = None
            if story_project_id:
                project_row = db.get(Project, story_project_id)
                if project_row is not None:
                    project_executor_value = project_row.executor
            executor = effective_execution(story, project_executor_value)
            # Claude Max (claude_code) cannot drive Fubala's pump tool loop:
            # its CODE stage always runs as the headless CLI session
            if executor == StoryExecution.INTERNAL and self._claude_code_target(story.model):
                executor = StoryExecution.HEADLESS
            # the project's declared test command (run_tests): the coder must
            # know the exact command, not guess at uv flags/interpreters
            test_line = test_command_prompt_line(test_command)
            if test_line:
                prompt_parts.append(test_line)
            user_prompt = "\n".join(prompt_parts)
            model = story.model
            # captured inside the session: `story` is detached once the
            # block exits, and the headless branch needs the turn cap below
            story_max_turns = story.max_tool_calls
            bus.publish(
                "story_log",
                {"story_id": story_id, "text": f"[coder] worktree {workspace_path.name}\n"},
            )

        toolbox = ToolBox(
            engine=self.engine,
            project_root=str(workspace_path),
            workspace=str(workspace_path),
            deny_globs=policy.denied_patterns(),
            allow_commands=policy.allowed_commands,
            skip_permissions=policy.permission_mode == "skip",
            extra_roots=[project_root, *proj_extras],
            # the worktree IS the designed jail — the coder is the one agent
            # allowed to write, and only there
            writable=True,
            # run_verify: the operator's trusted command, same cwd/env/timeout
            # as a verify round; project_root=main checkout for shared caches
            verify_command=verify_command or None,
            verify_timeout=verify_timeout,
            verify_project_root=project_root,
            # run_tests: the project's declared test command; None = not
            # exposed (project has no test_command)
            test_command=test_command or None,
            # process-group bookkeeping: the coder's verify/test/run_command
            # children become killable by cancel/stop and the /proc sweep
            story_id=story_id,
            # worker pods: run_verify/run_tests only (run_command and the
            # file tools stay local); None = in-process
            executor=self._executors.get(story_id),
            # tool-invoked verifies are silent like a verify round: floor the
            # watchdog with verify_timeout while they run (None clears)
            watchdog_floor=self._watchdog_floor_stamp(story_id),
        )
        guarded = GuardedExecutor(
            toolbox, policy, engine=self.engine,
            context={"story_id": story_id, "project_id": proj_id},
            # every guarded tool call is bounded by policy.timeout_seconds;
            # floor the watchdog with it so a silent-but-bounded tool never
            # reads as a hung stream
            watchdog_floor=self._watchdog_floor_stamp(story_id),
        )

        async def coder_round(prompt: str) -> None:
            # every round — first and every verify-fix round — starts with a
            # full tool/time budget; approvals in toolbox.allowed_roots and
            # the run context survive, only the counters reset.
            guarded.reset()
            round_started = time.monotonic()

            def on_correction(tool: str, rnd: int) -> None:
                # a malformed tool-call 400 repaired in-round: surfaced as a
                # note so the operator sees WHY a round was re-sent.
                # "feedback": False and no "reason" key — this must never be
                # fed back as reject feedback by _coder_feedback.
                with session_scope(self.engine) as db:
                    record_event(
                        db,
                        story_id,
                        "note",
                        {
                            "bad_tool_args": True,
                            "tool": tool,
                            "round": rnd,
                            "feedback": False,
                        },
                    )

            span_start, span_end = activity_tool_span_hooks(
                self.engine, story_id, policy.timeout_seconds, verify_timeout
            )
            try:
                async with self._coder_slot(story_id):
                    await run_agent(
                        self.gateway,
                        model=model,
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": prompt},
                        ],
                        tools=toolbox.schemas() or None,
                        execute=guarded,
                        on_tool_start=span_start,
                        on_tool_end=span_end,
                        on_delta=lambda text: bus.publish(
                            "story_log", {"story_id": story_id, "text": text}
                        ),
                        on_tool=activity_tool_hook(self.engine, story_id),
                        on_progress=self._progress_hook(story_id, "coder"),
                        max_tool_calls=policy.max_tool_calls,
                        timeout_seconds=policy.timeout_seconds,
                        max_output_tokens=_phase_budget(
                            load_settings(self.engine), "coder_max_output_tokens"
                        ),
                        max_output_tokens_cap=int(
                            load_settings(self.engine).get("agent_max_output_tokens_cap", 32768)
                            or 0
                        )
                        or None,
                        on_correction=on_correction,
                        log_context=story_id,
                    )
            finally:
                # the pump's CODE-stage run note: the same shape the headless
                # executor records, so the executor report (wall time per run,
                # rounds to landing) compares both executors symmetrically.
                with session_scope(self.engine) as db:
                    record_event(
                        db,
                        story_id,
                        "note",
                        {
                            "executor": StoryExecution.INTERNAL,
                            "code_stage_ms": int((time.monotonic() - round_started) * 1000),
                            "feedback": False,
                        },
                    )

        async def take_in_main_and_resolve() -> bool:
            """Merge the project root's current HEAD into the story worktree.

            Returns True when the story may continue (merge clean, or
            conflicts the coder resolved). False means: terminal — the story
            is parked in human review with the conflicts named, and the caller
            must stop without committing or verifying.

            Exactly one resolution round: an agent loop over a conflict is
            worse than a human looking at it. Any git-level failure raises
            WorkspaceError (the story fails; we never merge blind). Main
            advancing during the later verify-fix loop is NOT re-taken —
            that small window is picked up by the next coder round."""
            # push delivery: take in the remote's current default tip, not a
            # stale local HEAD
            await self._sync_push_root_async(proj_id, story_id, project_root)
            conflicted, paths = await asyncio.to_thread(
                workspaces.take_in_main, project_root, story_id
            )
            if not conflicted:
                return True
            bus.publish(
                "story_log",
                {
                    "story_id": story_id,
                    "text": f"[coder] merge with main conflicts: {', '.join(paths)}\n",
                },
            )
            with session_scope(self.engine) as db:
                record_event(
                    db,
                    story_id,
                    "note",
                    {"conflicts": paths, "phase": "merge-with-main"},
                )
            excerpt_blocks: list[str] = []
            for p in paths[:10]:
                excerpt = await asyncio.to_thread(
                    workspaces.markers_excerpt, project_root, story_id, p
                )
                excerpt_blocks.append(f"### {p}\n```\n{excerpt}\n```")
            excerpts = "\n\n".join(excerpt_blocks)
            conflict_prompt = "\n".join(
                [
                    "=== MERGE CONFLICT RESOLUTION REQUEST ===",
                    f"# Story: {story_title}",
                    "",
                    "Current main was merged into this worktree and git could not",
                    "reconcile it with your changes. The following paths still have",
                    "conflict markers:",
                    "",
                    "\n".join(f"- {p}" for p in paths),
                    "",
                    excerpts,
                    "",
                    "Resolve every conflict:",
                    "1. Edit each listed file, removing ALL `<<<<<<<`/`=======`/`>>>>>>>`",
                    "   markers and keeping the code that satisfies the spec.",
                    "2. Run `git add <path>` (or the add_path tool) for each resolved file.",
                    "3. Do not leave any marker behind; unresolved files block this story.",
                    "",
                    "Finish with a short statement of what you resolved.",
                ]
            )
            try:
                await code_round(conflict_prompt)
            except _HeadlessRequeued:
                raise
            except (workspaces.WorkspaceError, GatewayError) as e:
                if isinstance(e, GatewayError) and _is_transient_error(str(e)):
                    raise
                msg = redact_secrets(str(e))
                with session_scope(self.engine) as db:
                    record_event(
                        db,
                        story_id,
                        "note",
                        {
                            "phase": "merge-with-main",
                            "conflicts": paths,
                            "conflict_round_error": msg,
                            "feedback": False,
                        },
                    )
                bus.publish(
                    "story_log",
                    {
                        "story_id": story_id,
                        "text": f"[coder] merge conflict round failed: {msg}\n",
                    },
                )
                self._finish(
                    story_id,
                    StoryStatus.HUMAN_REVIEW,
                    StoryStatus.EXECUTING,
                    error=(
                        "merge with main has conflicts: "
                        + ", ".join(paths)
                        + f" (conflict round failed: {msg})"
                    ),
                )
                return False
            unresolved = await asyncio.to_thread(
                # the take-in's conflicted paths are belt-and-braces: they are
                # story-touched by definition, but passing them keeps the
                # resolution round's check explicit
                workspaces.unmerged, project_root, story_id, paths
            )
            if unresolved:
                bus.publish(
                    "story_log",
                    {
                        "story_id": story_id,
                        "text": (
                            "[coder] merge conflicts unresolved after one round: "
                            f"{', '.join(unresolved)}\n"
                        ),
                    },
                )
                self._finish(
                    story_id,
                    StoryStatus.HUMAN_REVIEW,
                    StoryStatus.EXECUTING,
                    error="merge with main has conflicts: " + ", ".join(unresolved),
                )
                return False
            return True

        # ---- executor branch ------------------------------------------------
        #
        # headless: the CODE stage is one Claude Code CLI run in the
        # worktree. Everything after it — commit, take-in, vendored guard,
        # verify, review — is shared with the pump path. A headless outcome
        # that is not "ok" takes the same paths a pump coder error would.
        story_model = model

        async def headless_round(prompt: str) -> None:
            """One headless CODE round: attach the tool-neutral rules to the
            runner-built CODE REQUEST, run the child, map the outcome onto
            the runner's existing finish paths."""
            settings_now = load_settings(self.engine)
            test_timeout = float(
                settings_now.get("headless_test_timeout_seconds")
                or headless_exec.HEADLESS_TEST_TIMEOUT_DEFAULT
            )
            prompt = headless_exec.with_rules(
                prompt,
                test_command=test_command or None,
                verify_command=verify_command or None,
                test_timeout_seconds=test_timeout,
            )
            base_url: str | None = None
            api_key: str | None = None
            small_fast: str | None = None
            limit_reset_tz = "UTC"
            oauth = False
            try:
                resolved = self.registry.resolve(story_model or "")
                oauth = resolved.provider.kind == "claude_code"
                base_url = None if oauth else resolved.provider.base_url
                api_key = self.registry.effective_key(resolved.provider)
                small_fast = self._flash_model_for_provider(resolved)
                # the provider states usage-limit reset times in its own zone
                # (z.ai: UTC+8) — the headless stream parser must read them
                # in that zone, never as UTC
                limit_reset_tz = limit_reset_zone(resolved.provider)
            except UnknownModelError:
                base_url = None
                api_key = None
            config = headless_exec.HeadlessConfig(
                base_url=base_url,
                api_key=api_key,
                model=story_model or "",
                small_fast_model=small_fast,
                max_turns=story_max_turns
                or int(settings_now.get("tools_max_calls", 40) or 40),
                timeout_seconds=float(
                    settings_now.get(
                        "headless_timeout_seconds",
                        headless_exec.HEADLESS_TIMEOUT_DEFAULT,
                    )
                    or headless_exec.HEADLESS_TIMEOUT_DEFAULT
                ),
                test_timeout_seconds=test_timeout,
                # per-story artifact dir (kept transcripts land under
                # <data>/headless/<story>/<stamp>.jsonl)
                artifacts_dir=str(data_dir_for_artifacts()),
                limit_reset_tz=limit_reset_tz,
                oauth=oauth,
                effort=(settings_now.get("code_effort") or None) if oauth else None,
            )
            # run_code_stage relays child stream events, but a quiet child
            # past WATCHDOG_TIMEOUT would still read as hung; the read loop
            # carries its own bound (headless_timeout_seconds), so floor the
            # watchdog with it for the duration of the CODE stage.
            def on_stream(payload: dict[str, Any]) -> None:
                publish_activity(
                    self.engine,
                    story_id,
                    "headless_stream",
                    {**payload, "phase": "coder", "executor": StoryExecution.HEADLESS},
                )

            async def run_segment(
                seg_prompt: str, resume: tuple[str, str, bool] | None
            ) -> tuple[headless_exec.HeadlessOutcome, bool]:
                """One CLI run (a segment of the round) inside ONE model
                slot, tests it runs itself (targeted, cut at the short
                timeout) included. The pod is set up inside it
                (_coder_slot). `resume` = (session id, config dir, ran in
                the pod): the CLI session is resumed with `--resume`.
                Returns the outcome and whether it ran in the pod."""
                async with self._coder_slot(story_id):
                    # run_executor=pod: the CLI runs in the run's worker pod. A
                    # merge in progress (conflict resolution round) stays local:
                    # MERGE_HEAD does not travel in the snapshot bundle.
                    pod_ex = self._executors.get(story_id)
                    use_pod = bool(
                        pod_ex is not None
                        and pod_ex.mode == "pod"
                        and not pod_ex.disabled
                        and project_root
                        and not await asyncio.to_thread(
                            workspaces.merge_in_progress, workspace_path
                        )
                    )
                    if resume is not None:
                        # the session lives where the first segment ran
                        if resume[2] and not use_pod:
                            return (
                                headless_exec.HeadlessOutcome(
                                    outcome="fix",
                                    reason="worker pod unavailable for the session resume",
                                ),
                                False,
                            )
                        use_pod = resume[2]
                    graph_on = self._code_graph_project(story_id)[1]
                    if graph_on:
                        # the pod builds its own graph under /tmp; locally the
                        # graph lives in the data dir. Never in the worktree.
                        config.mcp_config = (
                            code_graph.pod_mcp_config_json(pods.REPO_DIR)
                            if use_pod
                            else await self._graph_mcp(story_id, workspace_path)
                        )
                    floor = config.timeout_seconds + (
                        pods.EXEC_MARGIN + 300.0 if use_pod else 0.0
                    )
                    self._set_watchdog_floor(story_id, floor)
                    try:
                        outcome: headless_exec.HeadlessOutcome | None = None
                        if use_pod:
                            assert pod_ex is not None
                            try:
                                outcome = await asyncio.to_thread(
                                    pod_ex.run_headless,
                                    seg_prompt,
                                    config,
                                    on_stream,
                                    workspace=workspace_path,
                                    project_root=project_root,
                                    commit_message=f"story {story_id}: coder output",
                                    on_artefacts=_artefact_note_writer(self, story_id),
                                    **(
                                        {"resume": (resume[0], resume[1])}
                                        if resume is not None
                                        else {}
                                    ),
                                )
                            except pods.PodUnavailable:
                                if resume is not None:
                                    return (
                                        headless_exec.HeadlessOutcome(
                                            outcome="fix",
                                            reason="worker pod unavailable for the session resume",
                                        ),
                                        True,
                                    )
                                if pods.local_fallback_disabled(pod_ex.settings):
                                    self._requeue_fallback_wait(story_id, 0)
                                    raise _HeadlessRequeued() from None
                                outcome = None  # before the exec started: run locally
                                if graph_on:
                                    config.mcp_config = await self._graph_mcp(
                                        story_id, workspace_path
                                    )
                            except pods.PodRunLost as e:
                                # after it started: never rerun the round locally.
                                # The first loss requeues the story, a second
                                # fails it. After a cancel the endpoint marks the
                                # story failed itself, so only abort.
                                if self._requeue_pod_lost(story_id, str(e), pod_ex):
                                    raise _HeadlessRequeued() from None
                                if self._still_this_run(story_id, pod_ex):
                                    self._fail_infrastructure(
                                        story_id, f"worker pod lost mid-run: {e}"
                                    )
                                raise _HeadlessAborted() from None
                        if outcome is None:
                            resume_kwargs: dict[str, Any] = (
                                {
                                    "resume_session_id": resume[0],
                                    "config_dir": resume[1],
                                }
                                if resume is not None
                                else {}
                            )
                            use_pod = False
                            outcome = await asyncio.to_thread(
                                headless_exec.run_code_stage,
                                story_id=story_id,
                                worktree=workspace_path,
                                prompt=seg_prompt,
                                config=config,
                                on_event=on_stream,
                                **resume_kwargs,
                            )
                        return outcome, use_pod
                    finally:
                        self._clear_watchdog_floor(story_id)
                        if graph_on:
                            code_graph.cleanup(story_id)

            async def run_suite(pod_mode: bool) -> tuple[int, str]:
                """The full verify command outside the session: the model
                slot is already released, a test slot is held instead."""
                pod_ex = self._executors.get(story_id)
                if story_id not in self._running or (
                    pod_ex is not None
                    and pod_mode
                    and not self._still_this_run(story_id, pod_ex)
                ):
                    raise _HeadlessAborted()
                async with self._test_slot(story_id):
                    self._set_watchdog_floor(story_id, verify_timeout)
                    try:
                        return await asyncio.to_thread(
                            _run_verify_command,
                            workspace_path,
                            verify_command,
                            verify_timeout,
                            project_root,
                            story_id,
                            pod_ex if pod_mode else None,
                        )
                    except pods.FallbackCapFull:
                        self._requeue_fallback_wait(story_id, 0)
                        raise _HeadlessRequeued() from None
                    finally:
                        self._clear_watchdog_floor(story_id)

            secrets = headless_exec.run_secrets(config)
            requests = 0
            cuts = 0
            total_ms = 0
            total_turns: int | None = None
            total_usage: dict[str, Any] = {}
            resume: tuple[str, str, bool] | None = None
            resume_prompt = ""
            kept_local_dir: str | None = None
            try:
                while True:
                    outcome, in_pod = await run_segment(
                        prompt if resume is None else resume_prompt, resume
                    )
                    kept_local_dir = None
                    total_ms += outcome.duration_ms or 0
                    if outcome.num_turns is not None:
                        total_turns = (total_turns or 0) + outcome.num_turns
                    for key, value in outcome.usage.items():
                        total_usage[key] = total_usage.get(key, 0) + value
                    cuts += outcome.targeted_cuts
                    for _ in range(outcome.targeted_cuts):
                        bus.publish(
                            "story_log",
                            {
                                "story_id": story_id,
                                "text": (
                                    "[coder:headless] targeted test run cut at "
                                    f"{config.test_timeout_seconds:.0f} s\n"
                                ),
                            },
                        )
                    if outcome.outcome != "run_tests":
                        break
                    if not in_pod:
                        kept_local_dir = outcome.config_dir
                    if not verify_command:
                        # no rule was sent: a stray marker is a plain finish
                        outcome.outcome = "ok"
                        break
                    if requests >= HEADLESS_MAX_TEST_REQUESTS:
                        bus.publish(
                            "story_log",
                            {
                                "story_id": story_id,
                                "text": (
                                    "[coder:headless] test request cap "
                                    f"({HEADLESS_MAX_TEST_REQUESTS}) reached; "
                                    "treating the round as finished\n"
                                ),
                            },
                        )
                        outcome.outcome = "ok"
                        break
                    requests += 1
                    assert outcome.session_id and outcome.config_dir
                    rc, suite_output = await run_suite(in_pod)
                    suite_output = headless_exec._redact(suite_output, secrets)
                    resume_prompt = (
                        "=== TEST RESULT ===\n"
                        f"Command: {verify_command}\n"
                        f"Exit code: {rc}\n"
                        "Output (tail):\n"
                        + suite_output[-HEADLESS_TEST_RESULT_CAP:]
                        + "\n\nContinue the story. End your turn with "
                        f"{headless_exec.RUN_TESTS_MARKER} as the last line "
                        "to run the suite again."
                    )
                    resume = (outcome.session_id, outcome.config_dir, in_pod)
            finally:
                if kept_local_dir:
                    shutil.rmtree(kept_local_dir, ignore_errors=True)
            outcome.duration_ms = total_ms
            outcome.num_turns = total_turns
            outcome.usage = total_usage
            outcome.targeted_cuts = cuts
            with session_scope(self.engine) as db:
                record_event(
                    db,
                    story_id,
                    "note",
                    {
                        "executor": StoryExecution.HEADLESS,
                        "model": story_model,
                        "effort": config.effort,
                        "code_stage_ms": outcome.duration_ms or 0,
                        "num_turns": outcome.num_turns,
                        "outcome": outcome.outcome,
                        "exit_code": outcome.exit_code,
                        "graph_tool_calls": outcome.graph_tool_calls,
                        "test_requests": requests,
                        "targeted_cuts": cuts,
                        "feedback": False,
                    },
                )
            publish_activity(
                self.engine,
                story_id,
                "round",
                {
                    "phase": "coder",
                    "executor": StoryExecution.HEADLESS,
                    "num_turns": outcome.num_turns,
                    "duration_ms": outcome.duration_ms,
                    "outcome": outcome.outcome,
                },
            )
            bus.publish(
                "story_log",
                {
                    "story_id": story_id,
                    "text": (
                        f"[coder:headless] {outcome.outcome}"
                        + (f" — {outcome.reason}" if outcome.reason else "")
                        + "\n"
                    ),
                },
            )
            if outcome.outcome == "transient":
                # 429/usage limit from the CLI: patient requeue, never a fix
                # round (a rate limit is not coder feedback). A usage-limit
                # answer with a stated reset also HOLDS the provider, so
                # sibling stories on it wait for the window instead of each
                # burning a run into the same wall.
                reason = outcome.reason or "rate limit (headless)"
                provider_id = (
                    story_model.split("/", 1)[0]
                    if story_model and "/" in story_model
                    else ""
                )
                if provider_id:
                    hold_until = self.set_provider_hold(
                        provider_id, outcome.limit_reset_at, reason
                    )
                    if outcome.limit_note:
                        with session_scope(self.engine) as db:
                            record_event(
                                db,
                                story_id,
                                "note",
                                {
                                    "provider_limit": True,
                                    "provider_id": provider_id,
                                    "limit": outcome.limit_note,
                                    "reset_at": outcome.limit_reset_at.isoformat()
                                    if outcome.limit_reset_at
                                    else None,
                                    "held_until": hold_until.isoformat(),
                                    "reason": reason,
                                    "feedback": False,
                                },
                            )
                self._requeue_for_rate_limit(story_id, reason, reason="rate limit")
                raise _HeadlessRequeued()
            if outcome.outcome != "ok":
                raise workspaces.WorkspaceError(
                    outcome.reason or "headless coder failed"
                )
            if outcome.transcript_path:
                # the kept, redacted session transcript: recorded in an
                # event and served read-only by GET /stories/{id}/headless-transcript
                with session_scope(self.engine) as db:
                    record_event(
                        db,
                        story_id,
                        "note",
                        {
                            "headless_transcript": True,
                            "path": outcome.transcript_path,
                            "feedback": False,
                        },
                    )
            if outcome.permission_denials:
                # BLOCKED TOOLS VISIBLE: a denied tool is never a silent ok.
                # A real StoryNote (the gate's work-log) plus an event, each
                # denial named with a short input summary.
                denial_lines = [
                    "headless: permission denied for "
                    f"{len(outcome.permission_denials)} tool call(s):"
                ]
                for d in outcome.permission_denials:
                    tool = d.get("tool", "?")
                    summary = d.get("input", "")
                    denial_lines.append(f"- {tool}: {summary}".rstrip())
                denial_body = redact_secrets("\n".join(denial_lines)).strip()
                with session_scope(self.engine) as db:
                    db.add(
                        StoryNote(
                            story_id=story_id,
                            body=denial_body[:8192],
                            author="fubala",
                        )
                    )
                    record_event(
                        db,
                        story_id,
                        "note",
                        {
                            "headless_denied_tools": True,
                            "denials": outcome.permission_denials,
                            "feedback": False,
                        },
                    )
                publish_activity(
                    self.engine,
                    story_id,
                    "round",
                    {
                        "phase": "coder",
                        "executor": StoryExecution.HEADLESS,
                        "num_turns": outcome.num_turns,
                        "duration_ms": outcome.duration_ms,
                        "outcome": outcome.outcome,
                        "permission_denials": len(outcome.permission_denials),
                    },
                )
                bus.publish(
                    "story_log",
                    {
                        "story_id": story_id,
                        "text": denial_body + "\n",
                    },
                )
            with session_scope(self.engine) as db:
                record_event(
                    db,
                    story_id,
                    "note",
                    {
                        "headless_result": True,
                        "num_turns": outcome.num_turns,
                        "duration_ms": outcome.duration_ms,
                        "usage": outcome.usage,
                        "result": outcome.summary or "",
                        "feedback": False,
                    },
                )

        # every later coder round (merge-conflict resolution, reject retry,
        # verify fix) uses the SAME executor as the first one
        code_round = headless_round if executor == StoryExecution.HEADLESS else coder_round
        await code_round(user_prompt)
        if project_root:
            # commit BEFORE taking main in: an uncommitted coder change that
            # touches a file main also moved makes git refuse the merge
            # outright ("local changes would be overwritten"), which surfaces
            # as WorkspaceError — the story fails with no resolution round.
            # Committed, the same overlap becomes a normal merge conflict the
            # coder gets exactly one round to resolve.
            await asyncio.to_thread(
                workspaces.commit,
                project_root,
                story_id,
                f"story {story_id}: coder output",
                _artefact_note_writer(self, story_id),
            )
            if not await take_in_main_and_resolve():
                return
            # concluding commit: after a coder-RESOLVED conflict MERGE_HEAD is
            # still set, so this is what finishes the merge; when git
            # auto-merged cleanly it already committed and this is a no-op
            # (empty status → commit() returns early)
            await asyncio.to_thread(
                workspaces.commit, project_root, story_id, f"story {story_id}: merge main"
            )
            bus.publish(
                "story_log", {"story_id": story_id, "text": "[coder] changes committed\n"}
            )

        if code_reject_head:
            # The human rejected this code; a rerun that commits nothing must
            # never be reviewed as if the objection had been addressed. The
            # count is --first-parent --no-merges: main's own progress rides
            # in through the take-in MERGE commit (second parent), so only
            # commits landed directly on the story branch count as agent
            # work (AC4: main advancing is not coder work).
            new_commits = await asyncio.to_thread(
                workspaces.commits_since, project_root, story_id, code_reject_head
            )
            if new_commits == 0:
                bus.publish(
                    "story_log",
                    {
                        "story_id": story_id,
                        "text": (
                            "[coder] no new commits after the human reject — "
                            "one stronger retry\n"
                        ),
                    },
                )
                numbered = "\n".join(
                    f"{i}. {reason}" for i, reason in enumerate(reject_reasons, 1)
                )
                await code_round(
                    "\n".join(
                        [
                            "=== CODE REJECT RETRY ===",
                            f"# Story: {story_title}",
                            "",
                            "A human rejected the previous version of this code:",
                            numbered or "(no reason recorded)",
                            "",
                            "each item must change code or tests; a round "
                            "without a commit is rejected",
                        ]
                    )
                )
                await asyncio.to_thread(
                    workspaces.commit,
                    project_root,
                    story_id,
                    f"story {story_id}: coder output",
                    _artefact_note_writer(self, story_id),
                )
                if project_root:
                    if not await take_in_main_and_resolve():
                        return
                    await asyncio.to_thread(
                        workspaces.commit,
                        project_root,
                        story_id,
                        f"story {story_id}: merge main",
                    )
                recount = await asyncio.to_thread(
                    workspaces.commits_since, project_root, story_id, code_reject_head
                )
                if recount == 0:
                    # parks at the human gate with an explicit marker: never
                    # silently reviewed, never requeued (a requeue would loop
                    # the same no-op round forever)
                    with session_scope(self.engine) as db:
                        record_event(
                            db,
                            story_id,
                            "note",
                            {
                                "code_reject": True,
                                "gate": "code",
                                "no_op": True,
                                "by": "code-reviewer",
                            },
                        )
                    self._finish(
                        story_id,
                        StoryStatus.HUMAN_REVIEW,
                        StoryStatus.EXECUTING,
                        error="coder made no changes after the human reject",
                    )
                    return
            with session_scope(self.engine) as db:
                story = db.get(Story, story_id)
                if story is not None and story.code_reject_head:
                    # proven reacted-to: drop the marker in the SAME session
                    # that finishes to AI_REVIEW, so a crash between the two
                    # cannot leave a consumed marker parked on a story
                    story.code_reject_head = None
                    story.updated_at = utcnow()
                    db.add(story)
                    record_event(
                        db, story_id, "note", {"code_reject_cleared": True}
                    )

        # vendored-dependency guard (16 Oct lesson): the coder must never reach
        # review with node_modules/site-packages/.venv dumps or mass file adds.
        # Diff against story.base_ref — never HEAD — because retries accumulate.
        violation = None
        offenses = 0
        if project_root:
            with Session(self.engine) as db:
                story_row = db.get(Story, story_id)
                base = story_row.base_ref if story_row else None
                events = db.exec(
                    select(StoryEvent)
                    .where(
                        StoryEvent.story_id == story_id,
                        StoryEvent.kind == "note",
                    )
                    .order_by(StoryEvent.id)
                ).all()
            for ev in events:
                try:
                    data = json.loads(ev.data or "{}")
                except ValueError:
                    continue
                if isinstance(data, dict) and data.get("by") == "vendored-guard":
                    offenses += 1
            diff_text = await asyncio.to_thread(
                workspaces.diff, project_root, story_id, base, max_chars=None
            )
            added_paths = workspaces.diff_added_paths(diff_text)
            violation = vendored_guard(added_paths)
        if violation:
            with session_scope(self.engine) as db:
                record_event(
                    db,
                    story_id,
                    "note",
                    {"reason": violation, "by": "vendored-guard"},
                )
            bus.publish(
                "story_log",
                {
                    "story_id": story_id,
                    "text": f"[vendored-guard] {violation.splitlines()[0][:300]}\n",
                },
            )
            if offenses == 0:
                # first offense: requeue to the coder so the reason lands in
                # prev_feedback on the retry (never silently discarded)
                self._finish(story_id, StoryStatus.QUEUED, StoryStatus.EXECUTING)
                return
            # repeat offense: preserve the work with a visible marker — a
            # human decides, we never delete (the 16 Oct lesson)
            self._finish(
                story_id,
                StoryStatus.HUMAN_REVIEW,
                StoryStatus.EXECUTING,
                verify_result=json.dumps(
                    {
                        "passed": False,
                        "command": "vendored-guard",
                        "attempts": 0,
                        "exit_code": 1,
                        "output": violation[:4000],
                    }
                ),
            )
            return

        try:
            verify = await self._verify_and_fix(
                story_id,
                project_root,
                workspace_path,
                code_round,
                story_title,
                spec,
                park_on_round_crash=(executor == StoryExecution.HEADLESS),
            )
        except _FixRoundParked:
            return
        self._finish(
            story_id,
            StoryStatus.AI_REVIEW,
            StoryStatus.EXECUTING,
            verify_result=json.dumps(verify) if verify else None,
        )

    def _verify_baseline_flag(
        self,
        story_id: str,
        command: str,
        ran: int,
        threshold_pct: float,
        project_root: str,
    ) -> dict[str, Any] | None:
        """Flag a clear drop in tests run against the newest green DONE story
        of the same project (same verify command). Advisory: never changes
        `passed`. No baseline story = nothing flagged."""
        with Session(self.engine) as db:
            story = db.get(Story, story_id)
            if story is None:
                return None
            cond = (
                Story.project_id == story.project_id
                if story.project_id is not None
                else Story.project_id.is_(None)  # type: ignore[union-attr]
            )
            rows = db.exec(
                select(Story)
                .where(
                    cond,
                    Story.status == StoryStatus.DONE,
                    Story.id != story_id,
                    Story.verify_result.is_not(None),  # type: ignore[union-attr]
                )
                .order_by(Story.updated_at.desc())  # type: ignore[attr-defined]
            ).all()
            base_ref = story.base_ref
            main_ran = None
            for row in rows:
                try:
                    vr = json.loads(row.verify_result or "")
                except ValueError:
                    continue
                if not isinstance(vr, dict) or not vr.get("passed"):
                    continue
                if vr.get("command") != command:
                    continue
                c = vr.get("counts")
                if isinstance(c, dict) and isinstance(c.get("ran"), int):
                    main_ran = c["ran"]
                    break
        if main_ran is None:
            return None
        delta = 0
        try:
            diff_text = workspaces.diff(
                project_root, story_id, base_ref, max_chars=None
            )
        except Exception:  # noqa: BLE001 - advisory only
            diff_text = ""
        for line in diff_text.splitlines():
            if line.startswith(("+++", "---")):
                continue
            if re.match(r"[+-]\s*(?:async\s+)?def test_", line):
                delta += 1 if line[0] == "+" else -1
        expected = main_ran + delta
        if ran < expected * (1 - threshold_pct / 100):
            return {"main_ran": main_ran, "flag": f"ran {ran} tests, main ran {main_ran}"}
        return None

    async def _verify_and_fix(
        self,
        story_id: str,
        project_root: str | None,
        workspace_path: Path,
        coder_round: Callable[[str], Awaitable[None]],
        title: str,
        spec: str | None,
        park_on_round_crash: bool = False,
    ) -> dict[str, Any] | None:
        """Run the verify command in the worktree; failures bounce back to the
        coder for bounded fix rounds. The result lands on the story (and gates
        the final merge). None = verification not configured."""
        settings = load_settings(self.engine)
        max_attempts = 1 + int(settings.get("verify_fix_rounds", 2))
        with Session(self.engine) as db:
            story = db.get(Story, story_id)
            project_id = story.project_id if story is not None else None
            # extracted helper: the coder's run_verify tool resolves the same
            # command through this exact function, so they cannot drift
            command, timeout = resolve_verify_command(db, project_id)
        if not command or not project_root:
            return None
        result: dict[str, Any] = {}
        attempts = 0
        while attempts < max_attempts:
            attempts += 1
            # `_run_verify_command` stays sync (test_warm_verify.py pins its
            # rc 124/125 mapping, timeout kill, cwd/env and warm-verify
            # stamps); it runs in a thread so the loop keeps serving
            # /health, SSE and other stories during a long verify.
            # Verify emits NOTHING on the bus: without flooring the
            # watchdog, a healthy verify longer than WATCHDOG_TIMEOUT reads
            # as a hung stream and is killed mid-round. The verify carries
            # its own bound (`timeout`), so the floor is timeout + margin.
            # One test slot for the whole run (in-process or in the worker
            # pod), and no model slot: the story asks for one again only for
            # a fix round, queueing behind everyone already waiting.
            await self._wait_for_train_ci(story_id)
            async with self._test_slot(story_id):
                self._set_watchdog_floor(story_id, timeout)
                try:
                    rc, output = await asyncio.to_thread(
                        _run_verify_command,
                        workspace_path,
                        command,
                        timeout,
                        project_root,
                        story_id,
                        self._executors.get(story_id),
                    )
                except pods.FallbackCapFull:
                    self._requeue_fallback_wait(story_id, 0)
                    raise _HeadlessRequeued() from None
                finally:
                    self._clear_watchdog_floor(story_id)
            counts = parse_summaries(output)
            mismatch = count_mismatch(rc, counts)
            passed = rc == 0 and not mismatch
            tail = trim_output(output)
            result = {
                "passed": passed,
                "command": command,
                "attempts": attempts,
                "exit_code": rc,
                "output": tail,
                "counts": counts,
                "mismatch": mismatch,
            }
            if passed and counts:
                baseline = await asyncio.to_thread(
                    self._verify_baseline_flag,
                    story_id,
                    command,
                    counts["ran"],
                    float(settings.get("verify_drop_threshold_pct", 2.0)),
                    project_root,
                )
                if baseline:
                    result["baseline"] = baseline
                    with session_scope(self.engine) as db:
                        record_event(
                            db,
                            story_id,
                            "note",
                            {"phase": "verify", "reason": baseline["flag"]},
                        )
            totals = (counts or {}).get("totals") or {}
            count_text = (
                f" [{totals.get('passed', 0)} passed, {totals.get('failed', 0)} failed, "
                f"{totals.get('errors', 0)} errors, {totals.get('skipped', 0)} skipped]"
                if counts
                else ""
            )
            bus.publish(
                "story_log",
                {
                    "story_id": story_id,
                    "text": f"[verify] round {attempts}: "
                    f"{'PASSED' if passed else f'FAILED (exit {rc})'}"
                    f"{count_text}{f' - {mismatch}' if mismatch else ''}\n",
                },
            )
            with session_scope(self.engine) as db:
                record_event(
                    db,
                    story_id,
                    "verify",
                    {
                        "attempt": attempts,
                        "passed": passed,
                        "exit_code": rc,
                        "counts": totals if counts else None,
                        "mismatch": mismatch,
                    },
                )
            if passed:
                break
            if attempts < max_attempts:
                bus.publish(
                    "story_log",
                    {"story_id": story_id, "text": "[verify] asking the coder to fix\n"},
                )
                fix_prompt = "\n".join(
                    [
                        "=== FIX REQUEST ===",
                        f"# Story: {title}",
                        "## Spec to implement",
                        (spec or "")[:8000],
                        f"## Verification is failing (exit {rc}"
                        f"{f', {mismatch}' if mismatch else ''})",
                        f"$ {command}",
                        "```",
                        tail,
                        "```",
                        "Fix the code in the workspace so verification passes. "
                        "Prefer minimal, targeted changes.",
                    ]
                )
                try:
                    await coder_round(fix_prompt)
                except _HeadlessRequeued:
                    raise
                except (workspaces.WorkspaceError, GatewayError) as e:
                    if not park_on_round_crash or (
                        isinstance(e, GatewayError) and _is_transient_error(str(e))
                    ):
                        raise
                    msg = redact_secrets(str(e))
                    with session_scope(self.engine) as db:
                        record_event(
                            db,
                            story_id,
                            "note",
                            {
                                "phase": "verify-fix",
                                "fix_round_error": msg,
                                "attempt": attempts,
                                "feedback": False,
                            },
                        )
                    bus.publish(
                        "story_log",
                        {
                            "story_id": story_id,
                            "text": f"[verify] fix round failed: {msg}\n",
                        },
                    )
                    self._finish(
                        story_id,
                        StoryStatus.HUMAN_REVIEW,
                        StoryStatus.EXECUTING,
                        error=f"verify fix round {attempts} failed: {msg}",
                        verify_result=json.dumps(result),
                    )
                    raise _FixRoundParked() from e
                await asyncio.to_thread(
                    workspaces.commit,
                    project_root,
                    story_id,
                    f"story {story_id}: fix round {attempts}",
                    _artefact_note_writer(self, story_id),
                )
        return result

    async def _impact_section(
        self, story_id: str, project_root: str, base: str
    ) -> str:
        """The `## Impact report` prompt section for a story diff, recorded
        as a note event. Fails open: the section then says it is unavailable."""
        worktree = workspaces.worktree_path(project_root, story_id)
        out_dir = code_graph.graph_dir(story_id)
        report: str | None = None
        try:
            built = await asyncio.to_thread(code_graph.build, worktree, out_dir)
            if built is not None:
                report = await asyncio.to_thread(
                    code_graph.impact_report, worktree, base, built
                )
        finally:
            code_graph.cleanup(story_id)
        with session_scope(self.engine) as db:
            record_event(
                db,
                story_id,
                "note",
                {
                    "impact_report": True,
                    "content": report or code_graph.UNAVAILABLE,
                    "available": report is not None,
                    "feedback": False,
                },
            )
        return code_graph.report_section(report)

    async def _run_code_review(self, story_id: str) -> None:
        settings = load_settings(self.engine)
        project_root = settings.get("project_root")
        with session_scope(self.engine) as db:
            story = db.get(Story, story_id)
            if story is None or story.status != StoryStatus.AI_REVIEW or not story.base_ref:
                return
            # the worktree lives in the STORY'S project (created there by
            # `_execute_story`), not necessarily the global project_root —
            # diffing the wrong repo reads as "no changes" and fails the
            # story. Same resolution pattern as `_generate_spec`.
            proj_root, _, _ = self._project_roots(story.project_id)
            project_root = proj_root or project_root
            base = story.base_ref
            diff = (
                # git diff over a big repo is loop-blocking slow: run it in a
                # thread (see test_event_loop_responsiveness.py)
                await asyncio.to_thread(
                    workspaces.diff, project_root, story_id, base, max_chars=None
                )
                if project_root
                else ""
            )
            if not diff.strip():
                no_change_notes = [
                    e
                    for e in db.exec(
                        select(StoryEvent).where(
                            StoryEvent.story_id == story_id, StoryEvent.kind == "note"
                        )
                    ).all()
                    if "no changes" in (e.data or "")
                ]
                if len(no_change_notes) >= 2:
                    self._finish(
                        story_id,
                        StoryStatus.FAILED,
                        StoryStatus.AI_REVIEW,
                        error="coder produced no changes after retries",
                    )
                    return
                self._finish(
                    story_id,
                    StoryStatus.QUEUED,
                    StoryStatus.AI_REVIEW,
                )
                with session_scope(self.engine) as db2:
                    record_event(
                        db2,
                        story_id,
                        "note",
                        {"reason": "coder produced no changes; requeued", "by": "code-reviewer"},
                    )
                return
            # off-branch detection (advisory gate): a coder that left the
            # worktree on another branch (ext/*, the empty-land incident)
            # has commits the story branch will not carry — the diff above
            # still shows them because it reads the worktree FILES. Flag it
            # for the reviewer; no auto-fix anywhere (push adopts the
            # worktree HEAD itself at approve time).
            branch_warning_section = ""
            head_ref = (
                await asyncio.to_thread(workspaces.head_branch, project_root, story_id)
                if project_root
                else ""
            )
            if head_ref and head_ref != f"fubala/{story_id}":
                head_sha_now = ""
                try:
                    head_sha_now = (
                        await asyncio.to_thread(
                            workspaces._git,
                            workspaces.worktree_path(project_root, story_id),
                            "rev-parse",
                            "HEAD",
                        )
                    ).strip()
                except workspaces.WorkspaceError:
                    pass
                record_event(
                    db,
                    story_id,
                    "note",
                    {
                        "branch_off_story_branch": True,
                        "head_branch": head_ref,
                        "head_sha": head_sha_now,
                        "expected_branch": f"fubala/{story_id}",
                        "reason": (
                            f"worktree HEAD is on {head_ref}, not "
                            f"fubala/{story_id} — the story branch does not "
                            "carry the coder's commits"
                        ),
                    },
                )
                branch_warning_section = "\n".join(
                    [
                        "## Branch warning",
                        f"The coder's worktree HEAD is on `{head_ref}`, not the "
                        f"story branch `fubala/{story_id}` (HEAD "
                        f"{head_sha_now[:12]}). The diff below is read from the "
                        "worktree files, so it shows the change — but the story "
                        "branch itself carries none of these commits. Factor "
                        "that into your verdict.",
                    ]
                )
            system_prompt = CODE_REVIEWER_PROMPT.read_text()
            # a human rejected the previous code: the reviewer must check each
            # point instead of rubber-stamping an approving verdict
            reject_reasons = _code_gate_reasons(db, story_id)
            reason_points = _reason_points(reject_reasons)
            binding_notes = _binding_feedback_notes(db, story_id)
            operator_notes = _without_binding(
                _operator_notes_since(db, story_id), binding_notes
            )
            binding_section = (
                _binding_notes_section(binding_notes)
                + "\nAny decision not implemented in the diff means REJECTED."
                if binding_notes
                else ""
            )
            verify_section = ""
            if story.verify_result:
                v = json.loads(story.verify_result)
                verify_section = "\n".join(
                    [
                        "## Verification",
                        f"command `{v.get('command')}` -> "
                        f"{'PASSED' if v.get('passed') else 'FAILED'} "
                        f"after {v.get('attempts')} attempt(s)",
                    ]
                )
                if not v.get("passed"):
                    verify_section += (
                        "\nlast output tail:\n```\n"
                        + (v.get("output") or "")[-1500:]
                        + "\n```"
                    )
            model = story.model
            story_title = story.title
            story_spec = (story.spec or "")[:8000]
            review_project = db.get(Project, story.project_id) if story.project_id else None
            graph_on = bool(review_project and review_project.code_graph_enabled)
            test_command = resolve_test_command(db, story.project_id)
            # parsed here while the story is bound to the session; consulted
            # after the review stream to keep a red verify from approving
            verify = None
            if story.verify_result:
                try:
                    verify = json.loads(story.verify_result)
                except (TypeError, ValueError):
                    verify = None

        # the reviewer has its own model and effort settings; empty = the
        # story's model / no flag
        model, phase_error = self._phase_model(settings, "review_model", model)
        if phase_error is not None or model is None:
            self._fail(story_id, phase_error or "review_model: no model")
            return
        review_effort = self._cli_effort(model, settings.get("review_effort"))
        self._record_phase(story_id, "code_review", model, review_effort)

        # code graph: the impact report of the story diff, in every prompt path
        impact_section = ""
        if graph_on and project_root:
            impact_section = await self._impact_section(story_id, project_root, base)

        review_cap = int(settings.get("review_max_tool_calls") or 40)
        session_timeout = float(settings.get("review_session_timeout_seconds") or 900)
        test_budget = float(settings.get("review_test_budget_seconds") or 600)
        worktree = workspaces.worktree_path(project_root, story_id)
        # one prompt, one capped diff: the reviewer reads the files the cap cut
        # from the worktree (chunking is gone)
        diff_chunks = chunk_diff(diff, max_chars=DIFF_CHUNK_SIZE)
        not_shown = review_evidence.not_shown_paths(diff, diff_chunks)
        changed_tests = review_evidence.runnable_tests(
            test_command, review_evidence.changed_test_files(diff)
        )
        deny_globs = ToolPolicy.from_settings(settings).denied_patterns()
        bus.publish(
            "story_log", {"story_id": story_id, "text": "[code-review] reviewing diff\n"}
        )
        user_prompt = "\n".join(
            [
                "=== CODE REVIEW REQUEST ===",
                f"# Story: {story_title}",
                "## Spec",
                story_spec,
                verify_section,
                *([impact_section] if impact_section else []),
                *([branch_warning_section] if branch_warning_section else []),
                *(
                    [_reject_reasons_section(reject_reasons, reason_points)]
                    if reject_reasons
                    else []
                ),
                *([binding_section] if binding_section else []),
                *([_operator_notes_section(operator_notes)] if operator_notes else []),
                "## Diff (unified, vs base)",
                diff_chunks[0] if diff_chunks else diff,
                *(
                    [
                        "## Changed files not shown above (read them from the worktree)",
                        *(f"- {p}" for p in not_shown),
                    ]
                    if not_shown
                    else []
                ),
                "## Your turn",
                "Inspect the change with your read-only tools. Do not give a verdict "
                "yet: end with the MUTATIONS block described in your instructions.",
            ]
        )
        turn = dict(
            model=model, worktree=worktree, settings=settings, effort=review_effort,
            cap=review_cap, timeout=session_timeout,
        )
        ledger = review_evidence.Ledger(changed_tests=changed_tests)
        flags = review_evidence.SessionFlags()
        review_a = c_text = retry_text = ""
        verdict = reason = None
        try:
            # ---- phase A: inspect (model slot) ---------------------------
            a = await self._review_turn(
                story_id,
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                tools_on=True, phase="code_review", **turn,
            )
            review_a = a.text
            flags.timed_out, flags.turn_cap = a.timed_out, a.turn_cap
            if not (flags.timed_out or flags.turn_cap):
                # ---- phase B: evidence (test slot, no model slot) ------------
                ledger.mutations_block, entries = review_evidence.parse_mutations(review_a)
                await self._review_evidence(
                    story_id, ledger, entries,
                    worktree=worktree, project_root=project_root,
                    test_command=test_command, deny_globs=deny_globs,
                    budget=test_budget, settings=settings,
                )
                # ---- phase C: verdict (model slot) ---------------------------
                c_messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                    {"role": "assistant", "content": review_a or "(no output)"},
                    {
                        "role": "user",
                        "content": "\n".join(
                            [
                                "=== REVIEW EVIDENCE ===",
                                review_evidence.render_section(ledger),
                                "Only this ledger counts as evidence. Conclude with your "
                                "verdict as the last line: exactly 'VERDICT: APPROVED' or "
                                "'VERDICT: REJECTED - <one-line reason>'.",
                            ]
                        ),
                    },
                ]
                c = await self._review_turn(
                    story_id, c_messages, tools_on=True, phase="code_review_verdict", **turn
                )
                flags.timed_out, flags.turn_cap = c.timed_out, c.turn_cap
                c_text = c.text
                verdict, reason = parse_verdict(c_text)
                if verdict is None and not (flags.timed_out or flags.turn_cap):
                    # A review with no parsable VERDICT line used to be
                    # fail-open approved at the human gate. Give the reviewer
                    # exactly one corrective retry (multi-turn, so we don't
                    # replay an identical prompt that would reproduce the same
                    # truncation) and still reject if it stays verdict-less: a
                    # human can approve.
                    bus.publish(
                        "story_log",
                        {
                            "story_id": story_id,
                            "text": "[code-review] no verdict; asking for one\n",
                        },
                    )
                    retry_prompt = "\n".join(
                        [
                            "=== CODE REVIEW VERDICT REQUIRED ===",
                            "Your previous reply contained no parsable verdict line, so "
                            "it cannot be recorded as an approval.",
                            "Reply again, concluding with exactly one line "
                            "'VERDICT: APPROVED' or 'VERDICT: REJECTED - <one-line reason>'.",
                            "That final line is mandatory; nothing may follow it.",
                        ]
                    )
                    r = await self._review_turn(
                        story_id,
                        [
                            *c_messages,
                            {"role": "assistant", "content": c_text or "(no output)"},
                            {"role": "user", "content": retry_prompt},
                        ],
                        tools_on=False, phase="code_review_nudge", **turn,
                    )
                    retry_text = r.text
                    verdict, reason = parse_verdict(retry_text)
        except _ReviewPodGone:
            ledger.pod_unavailable = True
        review = "\n\n".join(
            part
            for part in (
                review_a.strip(),
                # the ledger's own words: rendered from what Fubala ran
                review_evidence.render_section(ledger).strip(),
                f"## Verdict turn\n\n{c_text.strip()}" if c_text.strip() else "",
                (
                    f"## Retry after missing verdict\n\n{retry_text.strip()}"
                    if retry_text.strip()
                    else ""
                ),
            )
            if part
        )
        if verdict is None and not (
            flags.timed_out or flags.turn_cap or ledger.pod_unavailable
        ):
            with session_scope(self.engine) as db:
                record_event(
                    db,
                    story_id,
                    "note",
                    {"reason": "code review produced no verdict", "by": "code-reviewer"},
                )
            self._finish(
                story_id,
                StoryStatus.HUMAN_REVIEW,
                StoryStatus.AI_REVIEW,
                ai_review=review,
                ai_verdict="rejected",
            )
            return
        # human reject reasons: an unanswered, explicitly-not-addressed or
        # hedged point overrides even an approving verdict — the reviewer
        # checked the human's points and left one unhandled. A reply with no
        # per-reason lines at all ⇒ trust the verdict (fail-open parity with
        # the spec review's `_uncovered_ac`).
        if reason_points and verdict == "approved":
            unaddressed = _unaddressed_reason(review, reason_points)
            if unaddressed is not None:
                verdict, reason = "rejected", unaddressed
        decision, cause = review_evidence.decide(verdict, ledger, flags)
        if decision == "incomplete":
            review = review.replace(
                review_evidence.render_section(ledger).strip(),
                review_evidence.render_section(ledger, cause).strip(),
                1,
            )
        if verdict == "rejected":
            # loop guard: a coder/reviewer pair that keeps disagreeing over
            # the same unanswered point must not ping-pong forever — after
            # CODE_REVIEW_REJECT_LIMIT consecutive rejections the story parks
            # at the human gate naming what is still open (precedent:
            # spec_reject_limit). Counted since the last human touch (spec
            # approval or a fresh code-gate reject), so a human objection
            # restarts the conversation.
            with session_scope(self.engine) as db:
                prior_rejects = _code_review_rejects(db, story_id)
            if prior_rejects >= CODE_REVIEW_REJECT_LIMIT:
                still_open = (
                    _unaddressed_reason(review, reason_points)
                    or (reason or "rejected by code reviewer")
                )
                park_text = (
                    f"code review rejected {prior_rejects} times in a row: "
                    f"{still_open}"
                )
                with session_scope(self.engine) as db:
                    record_event(
                        db,
                        story_id,
                        "note",
                        {"reason": park_text, "by": "code-reviewer"},
                    )
                self._finish(
                    story_id,
                    StoryStatus.HUMAN_REVIEW,
                    StoryStatus.AI_REVIEW,
                    ai_review=review,
                    ai_verdict=verdict,
                    error=park_text,
                )
                # the auto code gate runs here too: a rejected review fails
                # `ai_code_review_approved`, and the gate contract (any
                # failed check parks with the check named) must hold on this
                # route as well as on the approving one
                self._maybe_auto_code_gate(story_id, review, verdict)
                return
            self._finish(
                story_id,
                StoryStatus.QUEUED,
                StoryStatus.AI_REVIEW,
                ai_review=review,
                ai_verdict=verdict,
            )
            with session_scope(self.engine) as db:
                record_event(
                    db,
                    story_id,
                    "note",
                    {
                        "reason": reason or "rejected by code reviewer",
                        "by": "code-reviewer",
                        # the loop guard's counter key: a park note (above)
                        # deliberately omits it, so parking never inflates a
                        # future count
                        "code_review_reject": True,
                    },
                )
            return
        # A red verify can never be approved: force a rejection at the human
        # gate instead of trusting the reviewer's verdict. Fix rounds are
        # already exhausted by construction (the story only reaches review
        # after `_verify_and_fix` gives up), so requeueing to the coder would
        # loop forever — mark rejected and let the human decide (their approve
        # needs override_verify + justification while verify_result is red).
        verify_failed = (
            bool(verify)
            and verify.get("applicable") is not False
            and not verify.get("passed", True)
        )
        if verify_failed:
            reason_text = (
                "verify failed after "
                f"{verify.get('attempts')} attempts: "
                f"{_first_failure_line(str(verify.get('output') or ''))}"
            )
            with session_scope(self.engine) as db:
                record_event(db, story_id, "note", {"reason": reason_text, "by": "code-reviewer"})
            self._finish(
                story_id,
                StoryStatus.HUMAN_REVIEW,
                StoryStatus.AI_REVIEW,
                ai_review=review,
                ai_verdict="rejected",
            )
            # the auto code gate still runs here: `verify_green` is one of its
            # checks, so a red verify must park with that check named in the
            # note (and the gate_auto event) rather than only as a review
            # rejection. The verdict passed is the reviewer's OWN parsed
            # verdict — the forced "rejected" above is bookkeeping for the
            # human gate, not the reviewer's opinion, and the note must name
            # exactly the checks that actually failed.
            self._maybe_auto_code_gate(story_id, review, verdict)
            return
        if decision == "incomplete":
            # the review could not be completed with evidence: a human decides.
            # Never requeued to the coder (nothing was found wrong), never
            # approved, and the auto code gate fails `ai_code_review_approved`.
            with session_scope(self.engine) as db:
                record_event(
                    db,
                    story_id,
                    "note",
                    {
                        "reason": f"code review incomplete: {cause}",
                        "by": "code-reviewer",
                        "review_incomplete": True,
                    },
                )
            self._finish(
                story_id,
                StoryStatus.HUMAN_REVIEW,
                StoryStatus.AI_REVIEW,
                ai_review=review,
                ai_verdict="incomplete",
            )
            self._maybe_auto_code_gate(story_id, review, "incomplete")
            return
        self._finish(
            story_id,
            StoryStatus.HUMAN_REVIEW,
            StoryStatus.AI_REVIEW,
            ai_review=review,
            ai_verdict=verdict,
        )
        # ---- code gate auto -------------------------------------------
        # An approving code review would park the story at the human gate;
        # with gate_code == "auto" the machine checks decide instead. All
        # green -> land through the same code-gate branch a human approve
        # takes (merge or push+PR). Any fail -> stay at the human gate with
        # the failed checks named in a note. Runs ONLY here, at this
        # transition — never on land-poller bounces.
        self._maybe_auto_code_gate(story_id, review, verdict)

    # ---- code review as a tool-using session ------------------------------

    async def _ensure_review_pod(self, story_id: str, settings: dict) -> None:
        """The review's worker pod, placed like the spec phase's: built lazily,
        behind the backend-node fallback gate (requeues when the dev node is
        full). In pod mode a pod that cannot be had is PodUnavailable: it never
        degrades to a local run."""
        mode = str(settings.get("run_executor") or "")
        if mode not in ("verify", "pod"):
            return
        if story_id not in self._executors:
            executor = self._make_executor(story_id, asyncio.get_running_loop())
            if executor is not None:
                self._executors[story_id] = executor
        executor = self._executors.get(story_id)
        if mode == "pod" and executor is None:
            # preflight failed (no worker image, POD_* env missing): the API
            # pod must not run the review
            self._review_pod_unavailable(story_id, None, "no worker pod executor")
        if story_id not in self._gated:
            if not await self._fallback_gate(story_id):
                raise _HeadlessRequeued()  # requeued: the dev node is full
            self._gated.add(story_id)
        if executor is not None and executor.mode == "pod" and executor.disabled:
            self._review_pod_unavailable(story_id, executor, "no worker pod could be had")

    def _review_pod_unavailable(
        self, story_id: str, executor: pods.PodExecutor | None, cause: str
    ) -> None:
        """PodUnavailable in pod mode: requeue the story once (raises
        _HeadlessRequeued); the second time raise _ReviewPodGone so the review
        ends `incomplete`. Never runs anything locally."""
        if executor is None:
            gone = story_id not in self._running
        else:
            gone = not self._still_this_run(story_id, executor)
        if gone:
            raise _HeadlessAborted()  # cancel took the story over
        attempt = self._status_flag_count(story_id, "review_pod_requeue") + 1
        if attempt > REVIEW_POD_REQUEUE_CAP:
            raise _ReviewPodGone()
        note = redact_secrets(
            f"worker pod unavailable during the code review: {cause}; "
            f"requeued {attempt}/{REVIEW_POD_REQUEUE_CAP}"
        )
        with session_scope(self.engine) as db:
            story = db.get(Story, story_id)
            if story is None or story.status != StoryStatus.AI_REVIEW:
                raise _HeadlessAborted()
            story.status = StoryStatus.QUEUED
            story.error = None
            story.queued_at = utcnow()
            story.updated_at = utcnow()
            db.add(story)
            record_event(
                db,
                story_id,
                "status",
                {
                    "from": StoryStatus.AI_REVIEW,
                    "to": StoryStatus.QUEUED,
                    "note": note,
                    "review_pod_requeue": True,
                    "attempt": attempt,
                },
            )
            db.add(StoryNote(story_id=story_id, body=note[:8192], author="fubala"))
            payload = story_payload(story)
        bus.publish("story", {"story": payload})
        log.info("story %s requeued: review pod unavailable (%d)", story_id, attempt)
        raise _HeadlessRequeued()

    async def _review_turn(
        self,
        story_id: str,
        messages: list[dict],
        *,
        model: str,
        worktree: Path,
        settings: dict,
        effort: str | None,
        cap: int,
        timeout: float,
        tools_on: bool,
        phase: str,
    ) -> _ReviewTurn:
        """One read-only review session on the story worktree, under one model
        slot (released before the next phase takes a test slot)."""
        self._record_phase(story_id, phase, model, effort)
        cli_target = self._claude_code_target(model)
        async with self._model_slot(story_id):
            if cli_target is not None:
                return await self._review_cli_turn(
                    story_id, cli_target, messages, worktree, settings,
                    effort=effort, cap=cap, timeout=timeout, tools_on=tools_on, phase=phase,
                )
            return await self._review_agent_turn(
                story_id, model, messages, worktree, settings,
                effort=effort, cap=cap, timeout=timeout, tools_on=tools_on,
            )

    async def _review_cli_turn(
        self, story_id, target, messages, worktree, settings, *,
        effort, cap, timeout, tools_on, phase,
    ) -> _ReviewTurn:
        """Claude Max: one `claude -p` session with Read, Grep, Glob (no tools
        for the nudge). In pod mode it runs in the worker pod and never
        locally; otherwise (local dev) as a child process in the worktree."""
        from .providers import claude_code as cc

        resolved, key = target
        cli_tools = READ_ONLY_CLI_TOOLS if tools_on else ""
        max_turns = cap if tools_on else None
        await self._ensure_review_pod(story_id, settings)
        pod_ex = self._executors.get(story_id)
        in_pod = (
            str(settings.get("run_executor") or "") == "pod"
            and pod_ex is not None
            and pod_ex.mode == "pod"
        )
        if in_pod:
            graph_on = bool(cli_tools) and self._code_graph_project(story_id)[1]
            argv, env, prompt = cc.build_cli_session(
                resolved, key, messages, cli_tools=cli_tools, max_turns=max_turns,
                effort=effort,
                mcp_config=code_graph.pod_mcp_config_json(pods.REPO_DIR) if graph_on else None,
            )
            self._set_watchdog_floor(story_id, timeout + pods.EXEC_MARGIN + 300.0)
            try:
                rc, out, err = await asyncio.to_thread(
                    pod_ex.run_cli_session, argv, env, prompt, timeout, worktree
                )
            except pods.PodUnavailable:
                self._review_pod_unavailable(story_id, pod_ex, "the session could not start")
            except pods.PodRunLost as e:
                if self._requeue_pod_lost(story_id, str(e), pod_ex):
                    raise _HeadlessRequeued() from None
                if self._still_this_run(story_id, pod_ex):
                    self._fail_infrastructure(story_id, f"worker pod lost mid-run: {e}")
                raise _HeadlessAborted() from None
            finally:
                self._clear_watchdog_floor(story_id)
            if rc == 124:
                return _ReviewTurn(timed_out=True)
            text, usage = cc.parse_cli_result(
                resolved.provider.id, key, rc, out, err, allow_turn_cap=True
            )
            bus.publish("story_log", {"story_id": story_id, "text": text})
            return _ReviewTurn(text=text, turn_cap=usage.turn_cap)
        tag = f"{story_id}-{phase}"
        try:
            mcp_config = await self._graph_mcp(story_id, worktree, tag) if cli_tools else None
            text, usage = await cc.run_cli_agent(
                resolved, key, messages, cwd=str(worktree), cli_tools=cli_tools,
                max_turns=max_turns, timeout=timeout, effort=effort,
                mcp_config=mcp_config, allow_turn_cap=True,
            )
        except cc.CliTimeout:
            return _ReviewTurn(timed_out=True)
        finally:
            code_graph.cleanup(tag)
        bus.publish("story_log", {"story_id": story_id, "text": text})
        return _ReviewTurn(text=text, turn_cap=usage.turn_cap)

    async def _review_agent_turn(
        self, story_id, model, messages, worktree, settings, *,
        effort, cap, timeout, tools_on,
    ) -> _ReviewTurn:
        """Other models: Fubala's tool loop over a NON-writable ToolBox rooted
        at the worktree (HTTP plus in-process file reads, no process)."""
        policy = ToolPolicy.from_settings(settings)
        policy.max_tool_calls = cap
        policy.timeout_seconds = timeout
        toolbox = ToolBox(
            project_root=str(worktree), deny_globs=policy.denied_patterns(), writable=False
        )
        guarded = GuardedExecutor(
            toolbox, policy, watchdog_floor=self._watchdog_floor_stamp(story_id)
        )
        started = time.monotonic()
        result = await run_agent(
            self.gateway,
            model=model,
            messages=messages,
            tools=(toolbox.schemas() or None) if tools_on else None,
            execute=guarded if tools_on else None,
            on_delta=lambda text: bus.publish(
                "story_log", {"story_id": story_id, "text": text}
            ),
            on_tool=activity_tool_hook(self.engine, story_id),
            on_progress=self._progress_hook(story_id, "code_review"),
            max_tool_calls=cap if tools_on else None,
            timeout_seconds=timeout if tools_on else None,
            max_output_tokens_cap=int(settings.get("agent_max_output_tokens_cap", 32768) or 0)
            or None,
            effort=effort,
        )
        return _ReviewTurn(
            text=result.text if result.complete else "",
            timed_out=tools_on and time.monotonic() - started > timeout,
            turn_cap=tools_on and result.tool_calls >= cap,
        )

    async def _review_evidence(
        self,
        story_id: str,
        ledger: review_evidence.Ledger,
        entries: list[dict],
        *,
        worktree: Path,
        project_root: str,
        test_command: str,
        deny_globs: tuple[str, ...],
        budget: float,
        settings: dict,
    ) -> None:
        """Phase B: the changed tests, then at most MAX_MUTATIONS mutations,
        run by Fubala under one test slot and `budget` seconds, recorded in
        `ledger`. The model slot is not held (the sessions on either side
        release theirs)."""
        valid: list[tuple[review_evidence.Mutation, bytes]] = []
        for entry in entries[: review_evidence.MAX_MUTATIONS]:
            mutation, data, why = await asyncio.to_thread(
                review_evidence.validate_mutation, entry, worktree, deny_globs
            )
            if mutation is None or data is None:
                ledger.refused.append((entry, why or "refused"))
            else:
                valid.append((mutation, data))
        if not test_command:
            ledger.no_test_command = True
            return
        if not ledger.changed_tests and not valid:
            return
        await self._ensure_review_pod(story_id, settings)
        executor = self._executors.get(story_id)
        if executor is not None and executor.disabled:
            executor = None  # verify mode only: pod mode raised above
        bus.publish(
            "story_log", {"story_id": story_id, "text": "[code-review] running evidence\n"}
        )
        async with self._test_slot(story_id):
            self._set_watchdog_floor(story_id, budget + pods.EXEC_MARGIN + 300.0)
            try:
                try:
                    await asyncio.to_thread(
                        self._review_commands, story_id, ledger, valid, worktree,
                        project_root, test_command, budget, executor,
                    )
                except pods.PodUnavailable:
                    if (
                        executor is None
                        or executor.mode == "pod"
                        or pods.local_fallback_disabled(executor.settings)
                    ):
                        self._review_pod_unavailable(
                            story_id, executor, "the evidence run could not start"
                        )
                    ledger.test_run, ledger.mutations = None, []
                    ledger.over_budget = False
                    await asyncio.to_thread(
                        self._review_commands, story_id, ledger, valid, worktree,
                        project_root, test_command, budget, None,
                    )
            finally:
                self._clear_watchdog_floor(story_id)

    def _review_commands(
        self,
        story_id: str,
        ledger: review_evidence.Ledger,
        valid: list[tuple[review_evidence.Mutation, bytes]],
        worktree: Path,
        project_root: str,
        test_command: str,
        budget: float,
        executor: pods.PodExecutor | None,
    ) -> None:
        """Blocking part of phase B. Pod: everything in the worker pod
        (PodUnavailable propagates). In-process (local dev only): a scratch
        detached checkout of the snapshot commit, removed in a `finally`."""
        deadline = time.monotonic() + budget
        scratch: Path | None = None
        env = None
        if executor is not None and executor.is_pod:
            executor.prepare()
            executor.sync_workspace(worktree)

            def run_tests(command: str, timeout: float) -> tuple[int, str]:
                return executor.run(pods.add_maxprocesses(command), "", None, timeout)

            def run_mutation(m, data: bytes, command: str, timeout: float) -> tuple[int, str]:
                return executor.run_mutation(m.path, data, command, timeout)

        else:
            _, sha = workspaces.snapshot_bundle(worktree, story_id)
            scratch = Path(tempfile.mkdtemp(prefix="fubala-review-"))
            env = workspaces.scrub_child_env(
                {**os.environ, **workspaces.shared_cache_env(project_root)}
            )
            local = procs.LocalExecutor(scratch, story_id)
            try:
                workspaces._git(worktree, "worktree", "add", "--detach", str(scratch), sha)
            except BaseException:
                shutil.rmtree(scratch, ignore_errors=True)
                raise

            def run_tests(command: str, timeout: float) -> tuple[int, str]:
                return local.run(command, "", env, timeout)

            def run_mutation(m, data: bytes, command: str, timeout: float) -> tuple[int, str]:
                target = scratch / m.path
                try:
                    target.write_bytes(data)
                    return local.run(command, "", env, timeout)
                finally:
                    with contextlib.suppress(workspaces.WorkspaceError):
                        workspaces._git(scratch, "checkout", "--", m.path)

        try:
            if ledger.changed_tests:
                command = review_evidence.scope_command(test_command, ledger.changed_tests)
                rc, out = run_tests(command, max(1.0, deadline - time.monotonic()))
                ledger.test_run = review_evidence.TestRun(
                    command, rc, list(ledger.changed_tests), out
                )
                if rc != 0:
                    return  # mutations on a red baseline prove nothing
            for mutation, data in valid:
                left = deadline - time.monotonic()
                if left <= 0:
                    ledger.over_budget = True
                    return
                command = review_evidence.scope_command(
                    test_command,
                    review_evidence.runnable_tests(test_command, mutation.tests)
                    or ledger.changed_tests,
                )
                rc, out = run_mutation(mutation, data, command, left)
                ledger.mutations.append(
                    review_evidence.MutationRun(
                        mutation, review_evidence.mutation_status(rc), rc, command, out
                    )
                )
        finally:
            if scratch is not None:
                try:
                    workspaces._git(worktree, "worktree", "remove", "--force", str(scratch))
                except workspaces.WorkspaceError:
                    pass
                shutil.rmtree(scratch, ignore_errors=True)
                with contextlib.suppress(workspaces.WorkspaceError):
                    workspaces._git(worktree, "worktree", "prune")

    def _maybe_auto_code_gate(
        self, story_id: str, review: str, verdict: str | None
    ) -> None:
        with session_scope(self.engine) as db:
            story = db.get(Story, story_id)
            if (
                story is None
                or story.status != StoryStatus.HUMAN_REVIEW
                or not story.base_ref
                or (story.gate_code or StoryGate.HUMAN) != StoryGate.AUTO
                or story.execution == StoryExecution.EXTERNAL
            ):
                return
            project = _gate_project(db, story)
            project_root = _story_project_root_for_diff(db, story)
            diff_text = (
                workspaces.diff(project_root, story_id, story.base_ref, max_chars=None)
                if project_root
                else ""
            )
            try:
                verify = json.loads(story.verify_result) if story.verify_result else None
            except (TypeError, ValueError):
                verify = None
            checks = evaluate_code_gate(
                ai_verdict=verdict,
                verify=verify,
                diff_text=diff_text,
                human_rejected=bool(story.human_rejected),
                protected_globs=project.protected_globs if project else None,
                frozen_globs=project.frozen_globs if project else None,
                auto_max_diff_lines=(
                    project.auto_max_diff_lines
                    if project is not None and project.auto_max_diff_lines is not None
                    else 400
                ),
            )
            if not _evaluate_and_record(db, story, "code", checks):
                return
            from fastapi import HTTPException

            from .routers.stories import _land_executed_story  # noqa: PLC0415

            prev = story.status
            story.approved_by = "auto (gate)"
            try:
                _land_executed_story(
                    self.engine, None, db, story, prev, "auto (gate)"
                )
            except HTTPException as e:
                # a failed land (merge conflict, push error) already parked
                # the story FAILED with the reason; surface it as a note too
                record_event(
                    db,
                    story_id,
                    "note",
                    {
                        "reason": f"auto code gate land failed: {e.detail}",
                        "by": "fubala",
                    },
                )
                db.commit()

    async def _run_ai_review(self, story_id: str) -> None:
        with session_scope(self.engine) as db:
            story = db.get(Story, story_id)
            if story is None or story.status != StoryStatus.AI_REVIEW or not story.spec:
                return
            # deterministic gate first: an incomplete, truncated or
            # placeholder-only spec is rejected locally, before any LLM call.
            # Synthesising the review and falling through to the ordinary
            # rejected branch means a reject cap counts it automatically.
            preflight = _spec_preflight(story.spec, story.kind or StoryKind.CODE)
            # captured while the session is open: detached Story attributes
            # must not be read after the session closes
            story_acceptance = story.acceptance
            if preflight is not None:
                review = f"VERDICT: REJECTED - {preflight}"
                verdict, reason = "rejected", preflight
                system_prompt = user_prompt = model = None
            else:
                system_prompt = effective_prompt(self.engine, "ai_reviewer_prompt")
                feedback = self._reject_feedback(db, story_id)
                human_reasons = [f.reason for f in feedback if f.source == "operator"]
                user_prompt = _build_review_prompt(story, human_reasons)
                model = story.model
                review = ""
                verdict = reason = None

        review_effort: str | None = None
        if model is not None:
            # the reviewer has its own model and effort settings; empty = the
            # story's model / no flag
            review_settings = load_settings(self.engine)
            model, phase_error = self._phase_model(review_settings, "review_model", model)
            if phase_error is not None or model is None:
                self._fail(story_id, phase_error or "review_model: no model")
                return
            review_effort = self._cli_effort(model, review_settings.get("review_effort"))
            bus.publish(
                "story_log",
                {"story_id": story_id, "text": f"[ai-review] using model {model}\n"},
            )
            self._record_phase(story_id, "ai_review", model, review_effort)

        if model is not None:
            parts: list[str] = []
            async for event in self._stream(
                model,
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                story_id,
                review_effort,
            ):
                if isinstance(event, Delta):
                    parts.append(event.text)

            review = "".join(parts)
            verdict, reason = parse_verdict(review)
            # per-AC coverage: an explicitly uncovered criterion overrides
            # even an approving verdict. A reply with no coverage block at
            # all falls back to the verdict line, which keeps the spec review
            # fail-open (pinned by tests/test_stories.py).
            n_acs = len(_split_acceptance(story_acceptance))
            if n_acs and verdict == "approved":
                uncovered = _uncovered_ac(review, n_acs)
                if uncovered is not None:
                    verdict, reason = "rejected", uncovered
        if verdict == "approved" and load_settings(self.engine)[
            "auto_approve_after_ai_review"
        ]:
            # operator opted out of the human gate for AI-approved specs
            self._reset_spec_rejects(story_id)
            self._finish(
                story_id,
                StoryStatus.DONE,
                StoryStatus.AI_REVIEW,
                ai_review=review,
                ai_verdict=verdict,
                approved_by="auto (ai-approved)",
                spec_review=True,
            )
            return
        if verdict == "rejected":
            # AI rejection mirrors human rejection: back to queued, reason
            # feeds the next spec run — but bounded: after spec_reject_limit
            # consecutive rejections the story parks at the human gate so a
            # disagreeing writer/reviewer pair cannot loop forever.
            #
            # An AUTO spec gate short-circuits that loop: the review verdict
            # is itself a machine check (`ai_spec_review_approved`), so a
            # rejection is a failed check and the gate contract applies —
            # park at the human spec gate naming it, never re-queue. Without
            # this the check could never fail (it only ran on approvals) and
            # a rejected auto story would silently lose the human gate.
            gate_held = False
            with session_scope(self.engine) as db:
                story = db.get(Story, story_id)
                if (
                    story is not None
                    and story.status == StoryStatus.AI_REVIEW
                    and (story.gate_spec or StoryGate.HUMAN) == StoryGate.AUTO
                ):
                    checks = evaluate_spec_gate(
                        ai_verdict=verdict,
                        human_rejected=bool(story.human_rejected),
                    )
                    gate_held = not _evaluate_and_record(db, story, "spec", checks)
            if gate_held:
                self._finish(
                    story_id,
                    StoryStatus.HUMAN_REVIEW,
                    StoryStatus.AI_REVIEW,
                    ai_review=review,
                    ai_verdict=verdict,
                    reason=reason,
                    spec_review=True,
                )
                return
            limit = int(load_settings(self.engine).get("spec_reject_limit") or 0)
            rejects = self._bump_spec_rejects(story_id)
            note_reason = reason or "rejected by AI reviewer"
            if limit and rejects >= limit:
                self._finish(
                    story_id,
                    StoryStatus.HUMAN_REVIEW,
                    StoryStatus.AI_REVIEW,
                    ai_review=review,
                    ai_verdict=verdict,
                    reason=reason,
                    spec_review=True,
                )
                with session_scope(self.engine) as db:
                    record_event(
                        db,
                        story_id,
                        "note",
                        {
                            "reason": (
                                f"spec rejected {rejects} times by AI review: "
                                f"{note_reason}"
                            ),
                            "by": "ai-reviewer",
                        },
                    )
                return
            self._finish(
                story_id,
                StoryStatus.QUEUED,
                StoryStatus.AI_REVIEW,
                ai_review=review,
                ai_verdict=verdict,
                reason=reason,
                spec_review=True,
            )
            with session_scope(self.engine) as db:
                record_event(
                    db,
                    story_id,
                    "note",
                    {"reason": note_reason, "by": "ai-reviewer"},
                )
            return
        # any approval (auto or gated) resets the consecutive-reject counter
        self._reset_spec_rejects(story_id)
        # ---- spec gate auto -------------------------------------------
        # An approving AI review would park the story at the human gate;
        # with gate_spec == "auto" the machine checks decide instead. All
        # green -> the exact path a human approve takes (spec_approved,
        # rejects reset, EXECUTING). Any fail -> stay at the human gate with
        # the failed checks named in a note.
        with session_scope(self.engine) as db:
            story = db.get(Story, story_id)
            if (
                story is not None
                and story.status == StoryStatus.AI_REVIEW
                and (story.gate_spec or StoryGate.HUMAN) == StoryGate.AUTO
            ):
                checks = evaluate_spec_gate(
                    ai_verdict=verdict if verdict is not None else "approved",
                    human_rejected=bool(story.human_rejected),
                )
                if _evaluate_and_record(db, story, "spec", checks):
                    story.spec_rejects = 0
                    record_event(db, story.id, "spec_approved", {"by": "auto (gate)"})
                    prev = story.status
                    story.status = StoryStatus.EXECUTING
                    story.approved_by = "auto (gate)"
                    story.updated_at = utcnow()
                    db.add(story)
                    record_event(
                        db,
                        story.id,
                        "status",
                        {
                            "from": prev,
                            "to": story.status,
                            "approved_by": "auto (gate)",
                            "execute": True,
                        },
                    )
                    payload = story_payload(story)
                    db.commit()
                    bus.publish("story", {"story": payload})
                    return
        self._finish(
            story_id,
            StoryStatus.HUMAN_REVIEW,
            StoryStatus.AI_REVIEW,
            ai_review=review,
            # spec review stays fail-open: no verdict is still an approval
            # candidate (pinned by tests/test_stories.py:132)
            ai_verdict=verdict if verdict is not None else "approved",
            reason=reason,
            spec_review=True,
        )

    def _bump_spec_rejects(self, story_id: str) -> int:
        with session_scope(self.engine) as db:
            story = db.get(Story, story_id)
            if story is None:
                return 0
            story.spec_rejects = (story.spec_rejects or 0) + 1
            story.updated_at = utcnow()
            db.add(story)
            return story.spec_rejects

    def _reset_spec_rejects(self, story_id: str) -> None:
        with session_scope(self.engine) as db:
            story = db.get(Story, story_id)
            if story is None or not story.spec_rejects:
                return
            story.spec_rejects = 0
            story.updated_at = utcnow()
            db.add(story)

    def _append_spec_history(
        self, db: Session, story: Story, verdict: str, reason: str | None
    ) -> None:
        """One {version, verdict, reason, timestamp} row per reviewed spec
        version; called from inside the _finish session."""
        try:
            history = json.loads(story.spec_history or "[]")
        except ValueError:
            history = []
        history.append(
            {
                "version": story.spec_version,
                "verdict": verdict,
                "reason": reason,
                "timestamp": utcnow().isoformat(),
            }
        )
        story.spec_history = json.dumps(history)

    def _stream(
        self,
        model: str,
        messages: list[dict[str, str]],
        story_id: str,
        effort: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        # effort only reaches the gateway when set (claude_code targets), so
        # gateways without the parameter never see the kwarg
        extra = {"effort": effort} if effort is not None else {}

        async def gen() -> AsyncIterator[StreamEvent]:
            # every reviewer call (AI review, code review chunks, reasons
            # judgement, verdict retry) holds a model slot for its stream only
            async with self._model_slot(story_id):
                async for event in self.gateway.stream_chat(
                    model=model, messages=messages, **extra
                ):
                    if isinstance(event, Delta):
                        bus.publish(
                            "story_log", {"story_id": story_id, "text": event.text}
                        )
                    yield event

        return gen()

    def _finish(
        self,
        story_id: str,
        status: str,
        entry_statuses: tuple[str, ...] | str,
        *,
        spec: str | None = None,
        ai_review: str | None = None,
        ai_verdict: str | None = None,
        approved_by: str | None = None,
        error: str | None = None,
        verify_result: str | None = None,
        reason: str | None = None,
        spec_review: bool = False,
        provider: dict | None = None,
    ) -> None:
        if isinstance(entry_statuses, str):
            entry_statuses = (entry_statuses,)
        with session_scope(self.engine) as db:
            story = db.get(Story, story_id)
            if story is None or story.status not in entry_statuses:
                return
            prev = story.status
            story.status = status
            story.error = error
            if status == StoryStatus.QUEUED:
                # re-entering the queue restarts the fairness clock
                story.queued_at = utcnow()
            if verify_result is not None:
                story.verify_result = verify_result
            if spec is not None:
                # single choke point for every persisted deliverable: no
                # provider placeholder line ('[System: …]') may ever be stored
                story.spec = strip_placeholders(spec)
                story.spec_version += 1
            if ai_review is not None:
                story.ai_review = strip_placeholders(ai_review)
                story.ai_verdict = ai_verdict
                # verdict history: one row per reviewed spec version. The
                # reason rides along in ai_review_reason via _finish callers
                # that have one (the AI-review stages).
                if spec_review:
                    # spec_history holds SPEC-review verdicts only; the code
                    # review shares _finish but must never add a row
                    self._append_spec_history(db, story, ai_verdict or "approved", reason)
            if approved_by is not None:
                story.approved_by = approved_by
            story.updated_at = utcnow()
            db.add(story)
            if spec is not None:
                record_event(db, story_id, "spec", {"version": story.spec_version})
            if ai_review is not None:
                record_event(db, story_id, "ai_review", {"verdict": ai_verdict})
            status_data = {"from": prev, "to": status, "error": error}
            if provider:
                status_data["provider"] = provider
            record_event(db, story_id, "status", status_data)
            payload = story_payload(story)
        bus.publish("story", {"story": payload})

    def _reject_feedback(self, db, story_id: str) -> list[RejectFeedback]:
        """Structured reject feedback for the next spec round.

        Two labelled sources:
        - operator reasons: every note with by != 'ai-reviewer' since the
          last human spec approval, verbatim, deduplicated, in order. These
          persist across an AI approval — only a human approving the spec
          (or re-spec) clears them.
        - AI reasons: only the LATEST ai-reviewer note, so the writer fixes
          the current objection instead of re-litigating stale ones.

        Falls back to the newest operator-authored StoryNote (as_feedback on
        POST /notes) when no note events exist."""
        # the cut-off: a human approving the spec closes that revision's
        # feedback loop — everything before the latest spec_approved marker
        # belongs to a spec nobody is revising anymore
        marker = db.exec(
            select(StoryEvent.id)
            .where(StoryEvent.story_id == story_id, StoryEvent.kind == "spec_approved")
            .order_by(StoryEvent.id.desc())
        ).first()
        since = marker or 0
        operator_reasons: list[str] = []
        ai_reason: str | None = None
        for event in db.exec(
            select(StoryEvent)
            .where(
                StoryEvent.story_id == story_id,
                StoryEvent.kind == "note",
                StoryEvent.id > since,
            )
            .order_by(StoryEvent.id)
        ).all():
            try:
                data = json.loads(event.data)
            except ValueError:
                continue
            reason = data.get("reason")
            if not reason or data.get("feedback") is False:
                # "feedback": False marks operator bookkeeping (re-spec,
                # retry, lease releases): an audit trail, never a gripe the
                # spec writer could act on
                continue
            if data.get("by") == "ai-reviewer":
                # the parked note carries a bookkeeping prefix, not a fresh
                # objection — the latest raw rejection is what matters
                if reason.startswith("spec rejected ") and " times by AI review: " in reason:
                    reason = reason.split(" times by AI review: ", 1)[1]
                ai_reason = reason
                continue
            if data.get("by") in AGENT_AUTHORS:
                continue  # pipeline diagnostics, not operator feedback
            if reason not in operator_reasons:
                operator_reasons.append(reason)
        if not operator_reasons and ai_reason is None:
            # operator feedback can also arrive as a plain note (as_feedback
            # on POST /notes): fall back to the newest note authored by the
            # operator ("local") — never agent STATE logs, which would drown
            # the prompt
            note = db.exec(
                select(StoryNote)
                .where(StoryNote.story_id == story_id, StoryNote.author == "local")
                .order_by(StoryNote.id.desc())
            ).first()
            if note is not None:
                operator_reasons.append(note.body)
        out: list[RejectFeedback] = [
            RejectFeedback(reason=r, source="operator") for r in operator_reasons
        ]
        if ai_reason is not None:
            out.append(RejectFeedback(reason=ai_reason, source="ai"))
        return out


def _labels(feedback: list[RejectFeedback]) -> list[tuple[RejectFeedback, str]]:
    """(entry, label) pairs: 'operator' for human reasons, 'AI review' for
    the latest AI objection — the labels rendered into the spec prompt."""
    return [(item, "AI review" if item.source == "ai" else "operator") for item in feedback]


def _kind_line(kind: str, story_id: str | None = None) -> str | None:
    """One line telling the writer/reviewer what the deliverable is for a
    non-code story; None for code (byte-for-byte unchanged prompt)."""
    if kind == StoryKind.INFO:
        return (
            "## Story kind: info\n"
            "The deliverable is the document itself — no code will be executed. "
            "Write the spec as the finished artifact the human approves."
        )
    if kind == StoryKind.PREP:
        return (
            "## Story kind: prep\n"
            "The deliverable is child stories: create each one with the "
            "create_story tool, with depends_on including "
            f"{story_id or '<this story id>'} (this story's id). Do not "
            "re-create children that already exist (check with list_stories). "
            "This story cannot be approved until at least one child links back."
        )
    return None


def _build_user_prompt(
    story: Story, feedback: list[str], last_spec: str | None = None
) -> str:
    lines = [
        "=== SPEC REQUEST ===",
        f"# Story: {story.title}",
        "## Description",
        story.description.strip() or "(none provided)",
        "## Acceptance criteria",
        story.acceptance.strip() or "(none provided)",
        "## Model that will implement this",
        story.model,
    ]
    kind_line = _kind_line(story.kind or StoryKind.CODE, story.id)
    if kind_line:
        lines.append(kind_line)
    if last_spec:
        # revise, don't restart: the previous spec was rejected, so improve it
        # in place instead of writing a fresh one from scratch
        lines.append(
            "## Rejected spec to revise\n"
            "The spec below was rejected. Revise it: address every item in the "
            "feedback above, and keep the parts that were not criticised.\n\n"
            f"{last_spec.strip()}"
        )
    if feedback:
        lines.append("## Feedback from previous rejected attempt(s)")
        lines.extend(f"- {f}" for f in feedback)
    return "\n".join(lines)


def _build_review_prompt(
    story: Story, human_reasons: list[str] | None = None
) -> str:
    acs = _split_acceptance(story.acceptance)
    lines = [
        "=== AI REVIEW REQUEST ===",
        f"# Story: {story.title}",
        "## Description",
        story.description.strip() or "(none provided)",
    ]
    if acs:
        numbered = "\n".join(
            f"{i}. {_strip_ac_number(ac)}" for i, ac in enumerate(acs, 1)
        )
        lines.append("## Acceptance criteria")
        lines.append(numbered)
        lines.append(
            "## Coverage report (required)\n"
            "Your review must end with one line per acceptance criterion, "
            "exactly:\n"
            "    AC <n>: COVERED — <where the spec covers it>\n"
            "    AC <n>: NOT COVERED — <what is missing>\n"
            "Then the verdict line, as the last line of all."
        )
    else:
        lines.append("## Acceptance criteria")
        lines.append("(none provided)")
    lines += [
        "## Spec to review (v" + str(story.spec_version) + ")",
        story.spec or "(no spec)",
    ]
    kind_line = _kind_line(story.kind or StoryKind.CODE, story.id)
    if kind_line:
        # same line the spec writer saw, so the reviewer judges the right
        # artifact: a document (info) or a set of child stories (prep) —
        # never "where is the code?"
        lines.append(kind_line)
    if human_reasons:
        # reviewer accountability: the operator rejected a previous spec for
        # these reasons; a revised spec that ignores any of them is not done
        lines.append("## Operator reasons the revised spec must address")
        lines.append(
            "The operator rejected an earlier spec for the reasons below. "
            "Check that the current spec addresses each one; if any reason is "
            "not addressed, reply with VERDICT: REJECTED."
        )
        lines.extend(f"- the spec must address: {r}" for r in human_reasons)
    return "\n".join(lines)
