"""Story CRUD and guarded lifecycle actions.

Guardrails (Auto-Claude lessons):
- Transitions are action-oriented endpoints, not a free-form "set status" —
  invalid moves can't even be expressed, and the server still validates.
- The runner owns in_progress/review; humans own queue/approve/reject.
- `review -> done` only via /approve (the human gate); /reject sends the story
  back to queued with the reason fed into the next spec run.
- Stories are immutable while in flight (in_progress/review) and when done.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from pathlib import Path
from types import SimpleNamespace

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse
from sqlmodel import Session, or_, select

from .. import workspaces
from ..db import session_scope
from ..events import bus
from ..models import (
    Project,
    ProjectDelivery,
    Story,
    StoryEvent,
    StoryExecution,
    StoryGate,
    StoryKind,
    StoryNote,
    StoryStatus,
    effective_execution,
    effective_gate,
    utcnow,
)
from ..registry import TIERS, UnknownModelError
from ..runner import (
    apply_ab_headless,
    queued_pickup_order,
    record_event,
    release_claim,
    story_payload,
)
from ..schemas import (
    ApproveBody,
    BlockingStory,
    CancelBody,
    ClaimBody,
    CompleteBody,
    EvidenceIn,
    EvidenceOut,
    RejectBody,
    ReleaseBody,
    ReSpecBody,
    RetryBody,
    RetryLandBody,
    StoryCreate,
    StoryDetailOut,
    StoryEventOut,
    StoryNoteCreate,
    StoryNoteOut,
    StoryOut,
    StoryUpdate,
    SupersedeBody,
    as_utc_iso,
)
from ..settings_store import load_settings

router = APIRouter(prefix="/stories", tags=["stories"])

log = logging.getLogger(__name__)

EDITABLE_STATUSES = {StoryStatus.BACKLOG, StoryStatus.QUEUED, StoryStatus.FAILED}


def _validate_dependencies(db, story_id: str | None, depends_on: list[str]) -> None:
    """Dependencies must exist, not include self, and not form a cycle."""
    import json as _json

    seen = set(depends_on)
    if story_id and story_id in seen:
        raise HTTPException(status_code=400, detail="a story cannot depend on itself")
    for dep_id in depends_on:
        dep = db.get(Story, dep_id)
        if dep is None:
            raise HTTPException(status_code=400, detail=f"unknown dependency: {dep_id}")
    # walk the graph from each dependency; hitting ourselves = a cycle
    stack = list(depends_on)
    visited: set[str] = set()
    while stack:
        current_id = stack.pop()
        if current_id in visited:
            continue
        visited.add(current_id)
        current = db.get(Story, current_id)
        if current is None:
            continue
        for nxt in _json.loads(current.depends_on or "[]"):
            if story_id and nxt == story_id:
                raise HTTPException(
                    status_code=400,
                    detail="dependency cycle: this story is (transitively) blocked by itself",
                )
            stack.append(nxt)


def _story_project_root(engine, project_id: str | None) -> str | None:
    """The git root a story's worktree lives in: the owning project's root
    when the project exists and has one, otherwise the global `project_root`
    setting (also the fallback for a deleted project or an empty root).

    Pure root resolution only — deliberately no HTTP semantics, so callers
    keep deciding for themselves what "no root" means (the detail view
    tolerates it silently, /diff and approve 404/409 on it)."""
    root = load_settings(engine).get("project_root")
    if project_id:
        with Session(engine) as db:
            project = db.get(Project, project_id)
            if project is not None and project.root:
                root = project.root
    return root


def _get_story(db: Session, story_id: str) -> Story:
    story = db.get(Story, story_id)
    if story is None:
        raise HTTPException(status_code=404, detail="story not found")
    return story


def _resolve_model_or_400(request: Request, model: str) -> str:
    try:
        request.app.state.registry.resolve(model)
    except UnknownModelError:
        raise HTTPException(status_code=400, detail=f"unknown model: {model}") from None
    return model


def _story_out(story: Story, project_executor: str | None = None) -> StoryOut:
    # story_payload decodes the depends_on JSON string for us; pass the
    # project executor so `execution` comes out as the effective value.
    # `branch` follows the _annotate precedent: mutate after construction,
    # because story_payload (the SSE builder) never carries it. External
    # stories have no Fubala worktree/branch; a story still in the create
    # window (worktree exists, base_ref not yet pinned) stays None — the
    # branch cannot be assumed to exist yet.
    out = StoryOut(**story_payload(story, project_executor))
    if story.execution != StoryExecution.EXTERNAL and story.base_ref:
        out.branch = f"fubala/{story.id}"
    return out


def _project_executor(db: Session, story: Story) -> str | None:
    """The owning project's executor, for stories that inherit (NULL)."""
    if story.project_id is None:
        return None
    project = db.get(Project, story.project_id)
    return (project.executor or StoryExecution.INTERNAL) if project else None


def _effective_out(db: Session, story: Story) -> StoryOut:
    return _story_out(story, _project_executor(db, story))


def _queue_annotations(db: Session) -> tuple[dict[str, int], int]:
    """(positions, busy_slots) for the queue annotations.

    Positions come from `queued_pickup_order` — the same fair_order_key the
    scheduler's `_tick` sorts queued candidates with — so the number the
    board shows is the number the runner honours. deps-blocked or external
    queued stories simply don't appear in the map and stay None.
    `waiting_behind` counts internal stories holding a slot right now."""
    positions = {
        story.id: index
        for index, story in enumerate(queued_pickup_order(db), start=1)
    }
    busy = len(
        db.exec(
            select(Story.id).where(
                Story.status.in_(
                    [
                        StoryStatus.IN_PROGRESS,
                        StoryStatus.EXECUTING,
                        StoryStatus.AI_REVIEW,
                    ]
                ),
                or_(
                    Story.execution.is_(None),
                    Story.execution == StoryExecution.INTERNAL,
                ),
            )
        ).all()
    )
    return positions, busy


def _annotate(out: StoryOut, positions: dict[str, int], busy: int) -> StoryOut:
    """Fill queue_position/waiting_behind; leave None for non-queued stories
    (SSE payloads from story_payload never carry these fields, so the
    frontend guards on null)."""
    if out.status == StoryStatus.QUEUED:
        out.queue_position = positions.get(out.id)
        out.waiting_behind = busy
    return out


def _running_tools(db: Session, story_ids: list[str]) -> dict[str, list[dict]]:
    """Open tool calls per executing story (one query): the tool_started events
    after the latest round event that have no tool_finished, oldest first."""
    if not story_ids:
        return {}
    rows = db.exec(
        select(StoryEvent.story_id, StoryEvent.kind, StoryEvent.data)
        .where(StoryEvent.story_id.in_(story_ids))
        .where(StoryEvent.kind.in_(("round", "tool_started", "tool_finished")))
        .order_by(StoryEvent.id)
    ).all()
    open_calls: dict[str, dict[str, dict]] = {}
    for sid, kind, raw in rows:
        calls = open_calls.setdefault(sid, {})
        if kind == "round":
            calls.clear()
            continue
        try:
            data = json.loads(raw or "{}")
        except ValueError:
            continue
        call_id = data.get("call_id")
        if not call_id:
            continue
        if kind == "tool_started":
            calls[call_id] = {
                "call_id": call_id,
                "name": data.get("name", ""),
                "args": data.get("args", ""),
                "started_at": data.get("started_at", ""),
                "timeout_s": data.get("timeout_s"),
            }
        else:
            calls.pop(call_id, None)
    return {
        sid: sorted(calls.values(), key=lambda t: t["started_at"])
        for sid, calls in open_calls.items()
        if calls
    }


def _commit_and_publish(db: Session, story: Story, kind: str, data: dict) -> StoryOut:
    """Record an event, commit it, then publish the snapshot on the bus.

    The publish happens *after* `db.commit()`: a subscriber (board SSE, MCP
    wait_for_story) must never observe a status that a later rollback undoes —
    that was the phantom-status bug. The outer `session_scope` commit on exit
    is then a harmless no-op, and exceptions still roll back cleanly.
    """
    record_event(db, story.id, kind, data)
    story.updated_at = utcnow()
    db.add(story)
    db.commit()
    project_executor = _project_executor(db, story)
    out = _story_out(story, project_executor)
    payload = story_payload(story, project_executor)
    bus.publish("story", {"story": payload})
    return out


@router.post("", response_model=StoryOut, status_code=status.HTTP_201_CREATED)
def create_story(payload: StoryCreate, request: Request) -> StoryOut:
    """Model may be explicit, or a skill tier — resolved cost-aware at pickup."""
    if payload.skill is not None and payload.skill not in TIERS:
        raise HTTPException(status_code=400, detail=f"skill must be one of {TIERS}")
    if payload.model is not None:
        model: str | None = _resolve_model_or_400(request, payload.model)
        skill = payload.skill
    elif payload.skill is not None:
        model, skill = None, payload.skill  # scheduler stamps a concrete model
    else:
        model = _resolve_model_or_400(
            request,
            load_settings(request.app.state.engine).get("default_model")
            or request.app.state.registry.default_model(),
        )
        skill = None
    from ..models import Epic, Project

    with session_scope(request.app.state.engine) as db:
        project = None
        project_gates = ("human", "human")
        if payload.project_id is None:
            all_projects = db.exec(select(Project)).all()
            if len(all_projects) >= 2:
                options = "; ".join(f"{p.name} ({p.id})" for p in all_projects)
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "multiple projects configured: pass project_id to choose "
                        f"one of: {options}"
                    ),
                )
        if payload.project_id is not None:
            project = db.get(Project, payload.project_id)
            if project is None:
                raise HTTPException(status_code=400, detail="unknown project")
            # captured as plain values: the Project object is bound to this
            # session and expires with it, while the Story below is created
            # in a separate session_scope block
            project_gates = (project.default_gate_spec, project.default_gate_code)
        if payload.epic_id is not None and db.get(Epic, payload.epic_id) is None:
            raise HTTPException(status_code=400, detail="unknown epic")
        if payload.depends_on:
            _validate_dependencies(db, None, payload.depends_on)
        # An inherit story whose project executor is external never runs the
        # coder, so the default model stamp is noise there (and would defeat
        # the model-change refusal on such stories: the 409 promises the
        # stored model is untouched). Explicit model/skill survive: an
        # internal override can still run the coder with them.
        if (
            payload.execution is None
            and payload.model is None
            and payload.skill is None
            and project is not None
            and (project.executor or StoryExecution.INTERNAL) == StoryExecution.EXTERNAL
        ):
            model = None
        project = None  # expires with the session; gates already captured

    if payload.execution is not None and payload.execution not in StoryExecution.ALL:
        raise HTTPException(
            status_code=400,
            detail=f"execution must be one of {list(StoryExecution.ALL)}",
        )
    if payload.kind not in StoryKind.ALL:
        raise HTTPException(
            status_code=400,
            detail=f"kind must be one of {list(StoryKind.ALL)}",
        )

    with session_scope(request.app.state.engine) as db:
        story = Story(
            title=payload.title,
            description=payload.description,
            acceptance=payload.acceptance,
            model=model,
            skill=skill,
            execution=payload.execution,
            kind=payload.kind,
            # gates resolve payload -> project default -> human and are
            # stamped as concrete values, so a later project-level flip can
            # never retro-actively change an existing story's gate
            gate_spec=effective_gate(payload.gate_spec, project_gates[0]),
            gate_code=effective_gate(payload.gate_code, project_gates[1]),
            project_id=payload.project_id,
            epic_id=payload.epic_id,
            depends_on=json.dumps(payload.depends_on),
            max_tool_calls=payload.max_tool_calls,
            created_by="local",
        )
        db.add(story)
        db.flush()
        return _commit_and_publish(db, story, "created", {"title": story.title})


@router.get("", response_model=list[StoryOut])
def list_stories(request: Request, status_filter: str | None = None) -> list[StoryOut]:
    with session_scope(request.app.state.engine) as db:
        query = select(Story).order_by(Story.updated_at.desc())
        if status_filter is not None:
            if status_filter not in StoryStatus.ALL:
                raise HTTPException(status_code=400, detail=f"unknown status: {status_filter}")
            query = query.where(Story.status == status_filter)
        stories = db.exec(query).all()
        positions: dict[str, int] = {}
        busy = 0
        if any(s.status == StoryStatus.QUEUED for s in stories):
            positions, busy = _queue_annotations(db)
        running = _running_tools(
            db, [s.id for s in stories if s.status == StoryStatus.EXECUTING]
        )
        outs = [
            _annotate(_story_out(s, _project_executor(db, s)), positions, busy)
            for s in stories
        ]
        for out in outs:
            out.running_tools = running.get(out.id, [])
        return outs


# -- registered before /{story_id} so "events" is not captured as an id --


@router.get("/events")
async def story_events(request: Request) -> StreamingResponse:
    queue = await bus.subscribe()

    async def stream():
        try:
            while True:
                message = await queue.get()
                parsed = json.loads(message)
                kind = parsed["kind"]
                data = json.dumps(parsed["data"])
                yield f"event: {kind}\ndata: {data}\n\n"
        except asyncio.CancelledError:
            raise
        finally:
            bus.unsubscribe(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post(
    "/{story_id}/notes", status_code=status.HTTP_201_CREATED, response_model=StoryNoteOut
)
def add_story_note(story_id: str, payload: StoryNoteCreate, request: Request) -> StoryNoteOut:
    """Append a work-log note (append-only: no update/delete routes exist).
    Notes are operator/agent-authored resumable state; events stay the
    system's record. Redacted first; hard byte cap after redaction.

    `as_feedback: true` additionally records a reason-shaped note event
    (`{"reason": ..., "by": ...}`) so the operator can steer the next spec or
    coder run without going through reject — the spec and coder prompts both
    pick reason events up (and fall back to the newest local note)."""
    from ..guardrails import redact_secrets

    with session_scope(request.app.state.engine) as db:
        story = _get_story(db, story_id)
        redacted = redact_secrets(payload.body).strip()
        if not redacted:
            raise HTTPException(status_code=400, detail="note body is empty after redaction")
        if len(redacted.encode("utf-8")) > 8192:
            raise HTTPException(
                status_code=400,
                detail="note exceeds 8192 bytes (counted after secret redaction)",
            )
        note = StoryNote(story_id=story.id, body=redacted, author=payload.author or "local")
        db.add(note)
        db.flush()
        out = StoryNoteOut.model_validate(note, from_attributes=True)
        story.updated_at = utcnow()
        db.add(story)
        record_event(db, story_id, "note", {"author": out.author, "preview": redacted[:120]})
        if payload.as_feedback:
            record_event(
                db, story_id, "note",
                {"reason": redacted, "by": out.author,
                 "as_feedback": True, "note_id": out.id},
            )
        note_payload = {
            "id": out.id, "story_id": out.story_id, "body": out.body,
            "author": out.author, "created_at": as_utc_iso(out.created_at),
        }
        db.commit()
    # publish after commit so refetching consumers see the row
    bus.publish("story_note", {"story_id": story_id, "note": note_payload})
    return out


@router.get("/{story_id}", response_model=StoryDetailOut)
def get_story(story_id: str, request: Request) -> StoryDetailOut:
    with session_scope(request.app.state.engine) as db:
        story = _get_story(db, story_id)
        events = db.exec(
            select(StoryEvent)
            .where(StoryEvent.story_id == story_id)
            .order_by(StoryEvent.id)
        ).all()
        out = StoryDetailOut(**story_payload(story, _project_executor(db, story)))
        # same branch rule as _story_out: internal stories with a pinned
        # base_ref advertise the branch carrying their executed work
        if story.execution != StoryExecution.EXTERNAL and story.base_ref:
            out.branch = f"fubala/{story.id}"
        positions, busy = _queue_annotations(db)
        _annotate(out, positions, busy)
        if story.status == StoryStatus.EXECUTING:
            out.running_tools = _running_tools(db, [story.id]).get(story.id, [])
        out.events = [StoryEventOut.model_validate(e, from_attributes=True) for e in events]
        notes = db.exec(
            select(StoryNote).where(StoryNote.story_id == story_id).order_by(StoryNote.id)
        ).all()
        out.notes = [StoryNoteOut.model_validate(n, from_attributes=True) for n in notes]
        feedback_ids: set[int] = set()
        seen_ids: set[int] = set()
        for e in events:
            if e.kind != "note":
                continue
            try:
                data = json.loads(e.data or "{}")
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            if data.get("as_feedback") and isinstance(data.get("note_id"), int):
                feedback_ids.add(data["note_id"])
            saw = data.get("coder_saw_feedback")
            if isinstance(saw, list):
                seen_ids.update(i for i in saw if isinstance(i, int))
        for n in out.notes:
            n.as_feedback = n.id in feedback_ids
            n.seen_by_coder = n.id in seen_ids
        blocking = []
        for dep_id in out.depends_on:
            dep = db.get(Story, dep_id)
            if dep is not None and dep.status != StoryStatus.DONE:
                blocking.append(
                    BlockingStory(id=dep.id, title=dep.title, status=dep.status)
                )
        out.blocking = blocking
        project_root = _story_project_root(request.app.state.engine, story.project_id)
        if story.base_ref and project_root:
            try:
                out.code_diff = (
                    workspaces.diff(project_root, story_id, story.base_ref) or None
                )
            except workspaces.WorkspaceError:
                out.code_diff = None
        if story.evidence:
            # corrupt/manual edits must not 500 the story view
            try:
                out.evidence = EvidenceOut.model_validate(json.loads(story.evidence))
            except (ValueError, TypeError):
                out.evidence = None
        if out.code_diff is None and out.evidence is not None:
            # no internal worktree diff to show — surface what the external
            # executor submitted instead
            out.code_diff = out.evidence.code_diff or None
        return out


@router.get("/{story_id}/diff")
def get_story_diff(
    story_id: str,
    request: Request,
    download: bool = False,
) -> Response:
    """The UNCAPPED live worktree diff, as plain text.

    Opt-in by design: the default `code_diff` on GET /stories/{id} stays
    capped at 60k for old clients; this endpoint serves the complete diff
    (diff viewer, raw download). Read-only — it stages into a scratch index
    so it can never collide with the coder's commit in the same worktree.
    """
    with session_scope(request.app.state.engine) as db:
        story = _get_story(db, story_id)
        base_ref = story.base_ref
        status = story.status
        project_id = story.project_id
    project_root = _story_project_root(request.app.state.engine, project_id)
    if not base_ref:
        raise HTTPException(
            status_code=404,
            detail=(
                "no diff yet — the story has no base_ref (it has not reached "
                "execution or landed no worktree)"
            ),
        )
    if status not in (
        StoryStatus.EXECUTING,
        StoryStatus.AI_REVIEW,
        StoryStatus.HUMAN_REVIEW,
        StoryStatus.DONE,
        StoryStatus.FAILED,
    ):
        raise HTTPException(
            status_code=404,
            detail=f"no diff yet — the story is still {status} (diff exists from executing onward)",
        )
    if not project_root:
        raise HTTPException(status_code=404, detail="no project root configured")
    try:
        text = workspaces.diff(project_root, story_id, base_ref, max_chars=None)
    except workspaces.WorkspaceError as e:
        raise HTTPException(
            status_code=404, detail=f"worktree missing or unreadable: {e}"
        ) from e
    if not text.strip():
        raise HTTPException(
            status_code=404,
            detail="worktree missing or empty — no changes to diff "
            "(it may have been merged or discarded)",
        )
    headers = {"Cache-Control": "no-store"}
    if download:
        headers["Content-Disposition"] = f'attachment; filename="{story_id}.diff"'
    return Response(
        content=text,
        media_type="text/plain; charset=utf-8",
        headers=headers,
    )


@router.get("/{story_id}/headless-transcript")
def get_story_transcript(story_id: str, request: Request) -> Response:
    """The newest kept, redacted headless session transcript (read-only).

    The headless CODE executor copies the CLI's session JSONL out of the
    per-run CLAUDE_CONFIG_DIR before deleting it, redacts every line with
    the provider key as an extra literal, and records the artifact path in a
    `headless_transcript` note. This endpoint serves the newest such
    artifact; a story that never ran headless (or whose transcript could not
    be kept) is a 404.
    """
    import json as _json
    from pathlib import Path as _Path

    from ..models import StoryEvent

    with session_scope(request.app.state.engine) as db:
        _get_story(db, story_id)
        events = db.exec(
            select(StoryEvent)
            .where(StoryEvent.story_id == story_id)
            .order_by(StoryEvent.id.desc())
        ).all()
        recorded: str | None = None
        for event in events:
            try:
                data = _json.loads(event.data or "{}")
            except ValueError:
                continue
            if isinstance(data, dict) and data.get("headless_transcript"):
                path = data.get("path")
                if isinstance(path, str) and path:
                    recorded = path
                    break
    if not recorded:
        raise HTTPException(
            status_code=404,
            detail="no headless transcript for this story",
        )
    path = _Path(recorded)
    if not path.is_file():
        raise HTTPException(
            status_code=404,
            detail="the kept transcript is gone (artifact dir rotated)",
        )
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise HTTPException(
            status_code=404, detail=f"transcript unreadable: {e}"
        ) from e
    return Response(
        content=text,
        media_type="text/plain; charset=utf-8",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="{story_id}-transcript.jsonl"',
        },
    )


@router.patch("/{story_id}", response_model=StoryOut)
def update_story(story_id: str, payload: StoryUpdate, request: Request) -> StoryOut:
    with session_scope(request.app.state.engine) as db:
        story = _get_story(db, story_id)
        # An effectively-external story (stored override or inherited from the
        # project's executor) never runs the coder, so a model on it is
        # meaningless — refuse in any status, on both endpoints' worth of
        # semantics (PATCH here, approve in approve_story). Explicit
        # `model: null` is caught too: no silent fallback to skill routing.
        if "model" in payload.model_fields_set and (
            effective_execution(story, _project_executor(db, story))
            == StoryExecution.EXTERNAL
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    "external stories never run the coder, "
                    "so their model cannot be changed"
                ),
            )
        # Spec-gate model switch: at human_review with no executed work
        # (base_ref unset) and no external execution, the operator may still
        # pick the coder model — the coder run reads story.model from the DB
        # row, so setting it here lands before any execution. Everything else
        # stays locked; the code gate (base_ref set) is never editable.
        # External execution is resolved through inheritance (story NULL +
        # project executor = effective external). Explicit `model: null` does
        # NOT match (no silent fallback to skill routing) and keeps the 409.
        changed = {
            field for field in payload.model_fields_set if getattr(payload, field) is not None
        }
        if (
            story.status == StoryStatus.HUMAN_REVIEW
            and not story.base_ref
            and effective_execution(story, _project_executor(db, story))
            != StoryExecution.EXTERNAL
            and changed == {"model"}
        ):
            previous_model = story.model
            story.model = _resolve_model_or_400(request, payload.model)
            return _commit_and_publish(
                db,
                story,
                "note",
                {
                    "model_switch": True,
                    "from": previous_model,
                    "to": story.model,
                    "gate": "spec",
                    "by": "operator",
                },
            )
        if story.status not in EDITABLE_STATUSES:
            raise HTTPException(
                status_code=409,
                detail=f"story is {story.status} and cannot be edited "
                "(only backlog/queued/failed stories are editable)",
            )
        if payload.model is not None:
            story.model = _resolve_model_or_400(request, payload.model)
        for field in ("title", "description", "acceptance"):
            value = getattr(payload, field)
            if value is not None:
                setattr(story, field, value)
        if payload.epic_id is not None:
            story.epic_id = payload.epic_id or None
        if payload.depends_on is not None:
            _validate_dependencies(db, story_id, payload.depends_on)
            story.depends_on = json.dumps(payload.depends_on)
        if payload.execution is not None:
            if payload.execution not in StoryExecution.ALL:
                raise HTTPException(
                    status_code=400,
                    detail=f"execution must be one of {list(StoryExecution.ALL)}",
                )
        if "execution" in payload.model_fields_set:
            # explicit key = explicit intent; null clears the override so the
            # story inherits Project.executor again, a value switches it.
            # (Omitting the key entirely stays a no-op.)
            if payload.execution is None:
                if story.execution is not None:
                    story.execution = None
                    story.claimed_by = None
                    story.claimed_at = None
            elif payload.execution != effective_execution(
                story, _project_executor(db, story)
            ):
                story.execution = payload.execution
                # switching mode drops any stale claim: an internal story is
                # the runner's to pick up, an external one must be re-claimed
                story.claimed_by = None
                story.claimed_at = None
        if payload.kind is not None:
            if payload.kind not in StoryKind.ALL:
                raise HTTPException(
                    status_code=400,
                    detail=f"kind must be one of {list(StoryKind.ALL)}",
                )
            if payload.kind != (story.kind or StoryKind.CODE):
                story.kind = payload.kind
        if "max_tool_calls" in payload.model_fields_set:
            # explicit key = explicit intent; null clears the override so the
            # story inherits the app's tools_max_calls setting again. Only
            # editable stories reach this point (409 above otherwise); the
            # spec-gate equivalent lives in approve_story's payload handling.
            story.max_tool_calls = payload.max_tool_calls
        # Gates are editable with the same guard as `execution` (only
        # backlog/queued/failed). An explicit null clears back to "human"
        # — never to the project default, which was resolved at create.
        for gate_field in ("gate_spec", "gate_code"):
            if gate_field in payload.model_fields_set:
                value = getattr(payload, gate_field)
                story.__setattr__(
                    gate_field, value if value in StoryGate.ALL else StoryGate.HUMAN
                )
        return _commit_and_publish(db, story, "note", {"edited": True})


@router.delete("/{story_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_story(story_id: str, request: Request) -> Response:
    with session_scope(request.app.state.engine) as db:
        story = _get_story(db, story_id)
        if request.app.state.story_runner.is_running(story_id):
            raise HTTPException(status_code=409, detail="story is currently being processed")
        project_root = load_settings(request.app.state.engine).get("project_root")
        for event in db.exec(
            select(StoryEvent).where(StoryEvent.story_id == story_id)
        ).all():
            db.delete(event)
        db.delete(story)
    # best-effort: a deleted story leaves no worktree behind (current or
    # legacy location)
    if project_root:
        try:
            workspaces.discard(project_root, story_id)
        except workspaces.WorkspaceError as e:
            log.debug("discarding worktree for deleted story %s failed: %s", story_id, e)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---- lifecycle actions ------------------------------------------------


@router.post("/{story_id}/queue", response_model=StoryOut)
def queue_story(story_id: str, request: Request) -> StoryOut:
    with session_scope(request.app.state.engine) as db:
        story = _get_story(db, story_id)
        if story.status not in {StoryStatus.BACKLOG, StoryStatus.FAILED}:
            raise HTTPException(
                status_code=409,
                detail=f"cannot queue a story that is {story.status}",
            )
        prev = story.status
        story.status = StoryStatus.QUEUED
        story.queued_at = utcnow()
        # a queued story holds no claim (external stories start over)
        story.claimed_by = None
        story.claimed_at = None
        # A/B split: a NEW run of a story with no explicit execution
        # override may be routed to the headless CODE executor by the
        # project's share. Deterministic per story id, so the same story
        # resolves the same way across restarts. Once materialized the
        # override sticks — re-queueing never re-rolls the dice. The draw
        # lives in the runner helper so the scheduler's pickup (the
        # chokepoint for stories that reach `queued` through reject,
        # re-spec, release or a rate-limit requeue) writes the identical
        # event shape.
        apply_ab_headless(db, story)
        out = _commit_and_publish(db, story, "status", {"from": prev, "to": story.status})
        # a failed story re-queued by an operator is the same recovery as a
        # retry: cascade dependents that only failed because of it come back
        _reopen_cascade_dependents(db, story)
        return out


@router.post("/{story_id}/unqueue", response_model=StoryOut)
def unqueue_story(story_id: str, request: Request) -> StoryOut:
    with session_scope(request.app.state.engine) as db:
        story = _get_story(db, story_id)
        if story.status != StoryStatus.QUEUED:
            raise HTTPException(
                status_code=409, detail=f"cannot unqueue a story that is {story.status}"
            )
        prev = story.status
        story.status = StoryStatus.BACKLOG
        return _commit_and_publish(db, story, "status", {"from": prev, "to": story.status})


@router.post("/{story_id}/approve", response_model=StoryOut)
def approve_story(
    story_id: str,
    payload: ApproveBody,
    request: Request,
    background_tasks: BackgroundTasks,
) -> StoryOut:
    """The human gate, with two faces: approving a spec sends the story to
    execution (a coder agent in a worktree); approving reviewed code merges
    the worktree branch and finishes the story.

    Story kind decides which face applies (checked after the external
    branch — an external story's semantics always win, so an external ×
    info/prep story still just finishes on approve):
    code → the full worktree pipeline; info/prep → no worktree is ever
    touched, approval finishes the story (prep only once ≥1 child story
    links back via depends_on)."""
    with session_scope(request.app.state.engine) as db:
        story = _get_story(db, story_id)
        if story.status != StoryStatus.HUMAN_REVIEW:
            raise HTTPException(
                status_code=409,
                detail=f"only stories in human review can be approved (story is {story.status})",
            )
        if not story.spec and story.execution != StoryExecution.EXTERNAL:
            # internal stories reach human_review only after a spec (spec ->
            # ai review -> human review); an external one arrives here
            # straight from /complete with no spec at all
            raise HTTPException(status_code=409, detail="story has no spec to approve")
        if payload.model is not None:
            # Spec-gate model switch on approve: pick the coder model in the
            # same transaction as the status flip (the coder reads story.model
            # from the DB row, so this is race-free). The code gate
            # (base_ref set) and external stories — stored or inherited via
            # the project executor — stay locked: no coder run follows an
            # external approve, so a model there is meaningless.
            # Applied BEFORE any branch below so every approval path sees it.
            if story.base_ref or (
                effective_execution(story, _project_executor(db, story))
                == StoryExecution.EXTERNAL
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "model can only be switched at the spec gate "
                        "(human review with no executed work)"
                    ),
                )
            previous_model = story.model
            story.model = _resolve_model_or_400(request, payload.model)
            record_event(
                db,
                story.id,
                "note",
                {
                    "model_switch": True,
                    "from": previous_model,
                    "to": story.model,
                    "gate": "spec",
                    "by": payload.by,
                },
            )
        if payload.max_tool_calls is not None:
            # Spec-gate budget override, same gate rules as the model switch:
            # the coder reads story.max_tool_calls from the DB row at execute
            # time, so setting it in the same transaction is race-free. The
            # code gate (base_ref set) and external stories stay locked — no
            # coder run follows an external approve.
            if story.base_ref or (
                effective_execution(story, _project_executor(db, story))
                == StoryExecution.EXTERNAL
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "max_tool_calls can only be set at the spec gate "
                        "(human review with no executed work)"
                    ),
                )
            previous_budget = story.max_tool_calls
            story.max_tool_calls = payload.max_tool_calls
            record_event(
                db,
                story.id,
                "note",
                {
                    "max_tool_calls": True,
                    "from": previous_budget,
                    "to": story.max_tool_calls,
                    "gate": "spec",
                    "by": payload.by,
                },
            )
        prev = story.status
        story.approved_by = payload.by

        if story.execution == StoryExecution.EXTERNAL:
            # the code already landed outside Fubala (PR + CI): never enter
            # EXECUTING, never touch workspaces — this branch must stay
            # ABOVE the base_ref/worktree path so an external story that
            # happens to carry a base_ref can't be routed into a merge
            story.status = StoryStatus.DONE
            return _commit_and_publish(
                db,
                story,
                "status",
                {
                    "from": prev,
                    "to": story.status,
                    "approved_by": payload.by,
                    "execution": StoryExecution.EXTERNAL,
                },
            )

        if (story.kind or StoryKind.CODE) != StoryKind.CODE:
            # info/prep: the spec pipeline already delivered the artifact
            # (the document / the child stories); there is no code to
            # execute and no worktree to merge. This branch must stay ABOVE
            # the base_ref/worktree path (a non-code story should never have
            # one, but never route it into a merge if it somehow does).
            kind = story.kind or StoryKind.CODE
            story.spec_rejects = 0  # a human approved the spec: reject loop over
            record_event(db, story.id, "spec_approved", {"by": payload.by})
            children: list[Story] = []
            if kind == StoryKind.PREP:
                # Python-side parse of depends_on (NOT a SQL substring match,
                # which would false-positive on id prefixes) — a child is any
                # story whose depends_on list contains this id.
                children = [
                    candidate
                    for candidate in db.exec(select(Story)).all()
                    if story_id in json.loads(candidate.depends_on or "[]")
                ]
                if not children:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "prep story has no linked children — create at least "
                            "one story with depends_on=["
                            f"{story_id}] (and this story's epic_id) first"
                        ),
                    )
            story.status = StoryStatus.DONE
            return _commit_and_publish(
                db,
                story,
                "status",
                {
                    "from": prev,
                    "to": story.status,
                    "approved_by": payload.by,
                    "kind": kind,
                    "children": len(children) if kind == StoryKind.PREP else 0,
                },
            )

        if story.base_ref:
            return _land_executed_story(
                request.app.state.engine,
                background_tasks,
                db,
                story,
                prev,
                payload.by,
                payload.override_verify,
                payload.justification,
            )

        from ..guardrails import ToolPolicy

        policy = ToolPolicy.from_settings(load_settings(request.app.state.engine))
        story.spec_rejects = 0  # a human approved the spec: reject loop over
        record_event(db, story.id, "spec_approved", {"by": payload.by})
        if not policy.write_tools_enabled:
            # execution is opt-in; with write tools off, approval completes
            story.status = StoryStatus.DONE
            return _commit_and_publish(
                db,
                story,
                "status",
                {"from": prev, "to": story.status, "approved_by": payload.by},
            )
        story.status = StoryStatus.EXECUTING
        return _commit_and_publish(
            db,
            story,
            "status",
            {"from": prev, "to": story.status, "approved_by": payload.by, "execute": True},
        )


def _land_executed_story(
    engine,
    background_tasks,
    db: Session,
    story: Story,
    prev: str,
    approved_by: str | None,
    override_verify: bool = False,
    justification: str | None = None,
) -> StoryOut:
    """The code gate's landing branch, shared by the HTTP endpoint and the
    auto gate (which passes approved_by="auto (gate)" and never an override).

    Byte-identical behaviour to the old inline block: merge (with the red-
    verify guard and the self-update hook) or push+PR delivery."""
    story_id = story.id
    settings = load_settings(engine)
    # merge gate: verification is configured and failed -> the human
    # must reject (feedback) or discard instead of merging red code.
    # The one exception is an explicit operator override with a
    # justification — that path stamps the override into verify_result
    # and the event log so it is auditable forever.
    if (settings.get("verify_command") or "").strip() and story.verify_result:
        verify = json.loads(story.verify_result)
        # {"applicable": false} (info/prep) has no `passed` — it must
        # never read as a failure. Unreachable for non-code today (the
        # kind branch above finishes those stories with no base_ref),
        # but a story edited from code to info mid-flight could land
        # here with a stamped n/a, so guard anyway.
        if verify.get("applicable") is not False and not verify.get("passed", True):
            if not override_verify:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "verification failed after "
                        f"{verify.get('attempts')} attempt(s) — reject the story with "
                        "feedback, discard the changes, or approve with "
                        "override_verify + justification"
                    ),
                )
            justification = (justification or "").strip()
            if not justification:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "override_verify requires a justification — describe "
                        "why the failed verification is acceptable to merge"
                    ),
                )
            prior_output = str(verify.get("output") or "")
            tail = prior_output[-2000:]
            story.verify_result = json.dumps(
                {
                    "passed": True,
                    "command": "operator override",
                    "attempts": verify.get("attempts"),
                    "output": (justification + "\n\n--- prior failed output ---\n" + tail),
                }
            )
            record_event(
                db,
                story_id,
                "verify",
                {
                    "passed": True,
                    "override": True,
                    "by": approved_by,
                    "justification": justification[:2000],
                },
            )
            record_event(
                db,
                story_id,
                "note",
                {
                    "reason": f"verify override approved: {justification}",
                    "by": approved_by,
                },
            )
            db.add(story)
    # The merge/discard root comes from the STORY'S project (the
    # worktree was created there by the runner), with the global
    # project_root setting as fallback — they only coincide in
    # single-project setups. Root resolution is shared with the
    # detail/diff endpoints via _story_project_root.
    push_project = (
        db.get(Project, story.project_id) if story.project_id else None
    )
    project_root = _story_project_root(engine, story.project_id)
    # Delivery mode: a project configured for "push" ships the branch
    # to the remote instead of merging into the root checkout.
    if push_project is not None and (
        push_project.delivery or ProjectDelivery.MERGE
    ) == ProjectDelivery.PUSH:
        return _approve_push(
            db,
            story,
            prev,
            push_project,
            approved_by,
        )
    try:
        workspaces.merge(project_root, story_id)
    except workspaces.WorkspaceError as e:
        raise HTTPException(status_code=409, detail=f"merge failed: {e}") from e
    workspaces.discard(project_root, story_id)
    story.status = StoryStatus.DONE
    # the code is merged and the story finished: the reject marker
    # belongs to a round that is over (hygiene, same as discard)
    story.code_reject_head = None
    out = _commit_and_publish(
        db,
        story,
        "status",
        {"from": prev, "to": story.status, "approved_by": approved_by, "merged": True},
    )
    # Self-update manages the FUBALA checkout only: the helper
    # restarts this server onto the merge it just made and reverts it
    # if the new code won't boot. A story merged into a foreign
    # project repo must not restart Fubala — gate on the merge root
    # being the global root (the only branch that fired before).
    if settings.get("self_update_enabled") and Path(project_root).resolve() == Path(
        settings.get("project_root") or ""
    ).resolve():
        # restart onto the new code; the detached helper waits for this
        # server to exit (background task, post-response) and reverts
        # the merge if the new code won't boot
        from ..config import get_settings

        _spawn_self_update(
            {
                "port": get_settings().port,
                "host": get_settings().host,
                "old_pid": os.getpid(),
                "repo": str(Path(project_root).resolve()),
                "db": engine.url.database,
                "story": story_id,
                "merge_sha": workspaces.head(project_root),
                # stories.py lives in backend/app/routers/ — the
                # package root is three levels up
                "backend_dir": str(Path(__file__).resolve().parent.parent.parent),
            },
            background_tasks,
        )
    return out


def _approve_push(
    db: Session,
    story: Story,
    prev: str,
    project: Project,
    approved_by: str | None,
) -> StoryOut:
    """Push-mode approval: push `fubala/<story>` to the remote (plus an
    optional Gitea pull request) instead of merging into the root checkout.

    Failure policy mirrors merge's conflict handling, plus persistence: the
    story is marked FAILED with the git/HTTP error, a note records what was
    attempted, the worktree is KEPT (the only evidence of the work), and the
    operator gets a 409 naming the error."""
    from .. import pr as pr_module

    story_id = story.id
    branch = f"fubala/{story_id}"

    # Idempotent re-land: a story the poller bounced back to this gate
    # (merge conflict / failed CI / closed PR) still has its PR open and its
    # branch on the remote — the worktree was discarded when landing began,
    # so `workspaces.push` below would die on "branch does not exist" and
    # the operator could never re-arm the watch by approving again. There is
    # nothing local to deliver a second time: just re-enter landing and let
    # the poller read the PR (retry-land is the other way in, for when the
    # base itself needs updating on Gitea's side).
    if story.pr_index is not None and not workspaces.worktree_exists(
        project.root, story_id
    ):
        if (story.error or "").startswith("land failed: main moved during land"):
            # main moved under a green suite: a bare re-enter would read the
            # same red head again, so update the branch first (the helper
            # also resets the re-land attempt count)
            return _update_branch_and_reland(db, story, project, approved_by)
        record_event(
            db,
            story_id,
            "note",
            {
                "delivery": "push",
                "re_land": True,
                "pr_index": story.pr_index,
                "pr_url": story.pr_url,
                "approved_by": approved_by,
            },
        )
        story.status = StoryStatus.LANDING
        story.error = None
        story.land_state = None
        return _commit_and_publish(
            db,
            story,
            "status",
            {
                "from": prev,
                "to": story.status,
                "approved_by": approved_by,
                "landing": True,
                "re_land": True,
                "pr_index": story.pr_index,
                "pr_url": story.pr_url,
            },
        )

    pushed_sha = ""
    pr_url: str | None = None
    pr_index: int | None = None
    reused_pr = False
    resolved_409 = False
    merge_train_on = False
    try:
        # base_ref = the story's pinned fork point: workspaces.push refuses
        # an empty push against it, so an approve can never land nothing.
        pushed = workspaces.push(project.root, story_id, base_ref=story.base_ref)
        pushed_sha = str(pushed) if pushed else ""
        if getattr(pushed, "adopted", False):
            # the worktree HEAD was not on fubala/<id> (a coder that
            # committed on another branch): the story branch was moved onto
            # that HEAD before the push — the reviewed work is what lands
            record_event(
                db,
                story_id,
                "note",
                {
                    "delivery": "push",
                    "adopted_worktree_head": True,
                    "from_branch": getattr(pushed, "head_branch", "") or "(detached)",
                    "pushed_sha": pushed_sha,
                    "reason": (
                        "story branch adopted the worktree HEAD "
                        f"({getattr(pushed, 'head_branch', '') or 'detached'}) "
                        "before the push"
                    ),
                },
            )
        # All four pr_* fields set => also open a pull request. Anything
        # missing means "push only" (deliberately partial, not an error).
        if all(
            (
                project.pr_api_base,
                project.pr_repo,
                project.pr_base_branch,
                project.pr_token_file,
            )
        ):
            # Merge train: the batch PR is opened later, by the flush. The
            # story only joins the project's forming batch here (its branch
            # is on the remote); the worktree is KEPT so a bounced member's
            # work survives for the coder.
            from .. import train as train_module

            if train_module.train_enabled(project):
                if not train_module.train_ready(project):
                    raise workspaces.WorkspaceError(
                        "merge train requires push delivery and the full pr_* "
                        "wiring on the project"
                    )
                merge_train_on = True
            if not merge_train_on:
                token = pr_module.read_token(project.pr_token_file)

                # Re-approval with a PR still believed open: ask Gitea before
                # creating a second one — a blind POST /pulls 409s and fails the
                # story even though the PR is alive (incident 6a438b3d). The PR
                # is reused as-is; closed/merged (or a get_pull error) falls
                # through to create_pull as before.
                if story.pr_index is not None:
                    try:
                        existing = pr_module.get_pull(
                            base_url=project.pr_api_base,
                            repo=project.pr_repo,
                            token=token,
                            index=story.pr_index,
                        )
                    except pr_module.PullRequestError:
                        existing = None
                    if existing is not None and existing.get("state") == "open":
                        reused_pr = True
                        pr_url = story.pr_url
                        pr_index = story.pr_index
                if not reused_pr:
                    try:
                        html_url, pr_index = pr_module.create_pull(
                            base_url=project.pr_api_base,
                            repo=project.pr_repo,
                            token=token,
                            head=branch,
                            base_branch=project.pr_base_branch,
                            title=story.title or story_id,
                            body=f"Fubala story {story_id} approved by {approved_by or 'human'}",
                        )
                        pr_url = pr_module.public_pr_url(
                            html_url,
                            api_base=project.pr_api_base,
                            web_base=project.pr_web_base,
                            repo=project.pr_repo,
                            index=pr_index,
                        )
                    except pr_module.PullRequestConflictError:
                        # 409 "already exists": find the open PR for this head
                        # and adopt it instead of failing the story. No open PR
                        # found (or the lookup itself failed) falls into the
                        # failure path below — FAILED + 409 response, unchanged.
                        found: tuple[str, int] | None = None
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
                            raise
                        resolved_409 = True
                        found_url, pr_index = found
                        pr_url = pr_module.public_pr_url(
                            found_url,
                            api_base=project.pr_api_base,
                            web_base=project.pr_web_base,
                            repo=project.pr_repo,
                            index=pr_index,
                        )
    except (workspaces.WorkspaceError, pr_module.PullRequestError) as e:
        message = str(e)
        story.status = StoryStatus.FAILED
        story.error = message
        # persist the head now that it is known: the reconcile sweep needs
        # proof the story produced a commit, and the note alone is a weak
        # (and unindexed) place to keep it
        story.pushed_sha = pushed_sha or None
        record_event(
            db,
            story_id,
            "note",
            {
                "delivery": "push",
                "pushed_sha": pushed_sha or None,
                "error": message,
                "remote_branch": branch,
            },
        )
        _commit_and_publish(
            db,
            story,
            "status",
            {
                "from": prev,
                "to": story.status,
                "approved_by": approved_by,
                "pushed": False,
                "error": message,
            },
        )
        raise HTTPException(status_code=409, detail=f"push failed: {message}") from e

    if merge_train_on:
        # Merge train: no PR here. The story joins the forming batch and the
        # worktree is KEPT (a bounced member's work is the coder's input).
        from .. import train as train_module

        train_module.join(db, project, story, prev, approved_by, pushed_sha)
        return _story_out(story, _project_executor(db, story))

    record_event(
        db,
        story_id,
        "note",
        {
            "delivery": "push",
            "pushed_sha": pushed_sha,
            "pr_url": pr_url,
            "pr_index": pr_index,
            "reused_pr": reused_pr,
            "resolved_409": resolved_409,
        },
    )
    if pr_index is not None:
        # A pull request is open: the story is NOT done. It sits in landing
        # until the poller sees the PR merged (then done) or the land fails
        # (then back to this gate with the reason). The PR facts are
        # persisted here so a restart resumes the watch from the DB.
        story.pr_url = pr_url
        story.pr_index = pr_index
        story.pushed_sha = pushed_sha or None
        story.land_state = None
        story.status = StoryStatus.LANDING
        story.error = None
        out = _commit_and_publish(
            db,
            story,
            "status",
            {
                "from": prev,
                "to": story.status,
                "approved_by": approved_by,
                "pushed": True,
                "pushed_sha": pushed_sha,
                "pr_url": pr_url,
                "pr_index": pr_index,
                "landing": True,
            },
        )
        # the branch lives on the remote now — cleanup matches merge mode
        workspaces.discard(project.root, story_id)
        return out
    story.status = StoryStatus.DONE
    story.error = None
    out = _commit_and_publish(
        db,
        story,
        "status",
        {
            "from": prev,
            "to": story.status,
            "approved_by": approved_by,
            "pushed": True,
            "pushed_sha": pushed_sha,
            "pr_url": pr_url,
        },
    )
    # the branch lives on the remote now — cleanup matches merge mode
    workspaces.discard(project.root, story_id)
    # NOTE: no self-update here. workspaces.head(project.root) would report
    # the LOCAL checkout's sha, which push mode never advances — restarting
    # on it would roll the server back to pre-story code.
    return out


def _spawn_self_update(info: dict, background_tasks: BackgroundTasks) -> None:
    """Detached helper restarts the server on the merged code; this process
    then steps aside (after the response has been sent)."""
    import subprocess
    import sys

    from ..config import get_settings

    # stderr lands in the helper's own log path — a silent spawn is undebuggable
    spawn_log = open("/tmp/fubala-selfupdate-spawn.log", "ab")
    argv = [
        sys.executable, "-m", "app.selfupdate",
        "--port", str(info.get("port") or get_settings().port),
        # bind host: caller passes the setting explicitly; fall back to the
        # env so the helper never restarts onto a different interface
        "--host", str(info.get("host") or get_settings().host),
    ]
    if info.get("old_pid") is not None:
        argv += ["--old-pid", str(info["old_pid"])]
    argv += [
        "--repo", info["repo"],
        "--db", info["db"],
        "--story", info["story"],
        "--merge-sha", info["merge_sha"],
        "--backend-dir", info["backend_dir"],
    ]
    subprocess.Popen(
        argv,
        cwd=info["backend_dir"],
        stdout=spawn_log,
        stderr=spawn_log,
        start_new_session=True,
    )

    def _step_aside(delay: float = 1.5) -> None:
        time.sleep(delay)
        os.kill(os.getpid(), signal.SIGTERM)

    background_tasks.add_task(_step_aside)


@router.post("/{story_id}/discard", response_model=StoryOut)
def discard_story(story_id: str, request: Request) -> StoryOut:
    """Throw away the executed changes: drop the worktree/branch, story returns
    to backlog for reconsideration.

    Works from human_review (with a pinned base_ref — the executed-work gate)
    and from failed (a cancelled story carries its wip commit on
    fubala/<id>). Discarding a failed story FORFEITS the reconcile sweep's
    self-heal: if the branch had actually merged on the remote, the sweep
    would have marked the story done — after a discard the branch is gone and
    that can no longer happen (the sweep skips deleted branches gracefully).
    """
    with session_scope(request.app.state.engine) as db:
        story = _get_story(db, story_id)
        if story.status not in {StoryStatus.HUMAN_REVIEW, StoryStatus.FAILED} or not story.base_ref:
            raise HTTPException(
                status_code=409,
                detail="only executed stories in human review or failed can be discarded",
            )
        if story.execution == StoryExecution.EXTERNAL:
            # external code never lived in one of our worktrees — there is
            # nothing here to discard, and touching workspaces would risk
            # deleting an unrelated internal worktree
            raise HTTPException(
                status_code=409,
                detail=(
                    "external stories have no Fubala worktree to discard — "
                    "reject the story instead"
                ),
            )
        project_root = _story_project_root(request.app.state.engine, story.project_id)
        try:
            workspaces.discard(project_root, story_id)
        except workspaces.WorkspaceError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        prev = story.status
        story.status = StoryStatus.BACKLOG
        story.base_ref = None
        # a fresh execution re-pins base_ref from scratch: the old code-gate
        # reject marker would otherwise park the next run for changes that
        # were thrown away with the branch
        story.code_reject_head = None
        return _commit_and_publish(
            db, story, "status", {"from": prev, "to": story.status, "discarded": True}
        )


@router.post("/{story_id}/reject", response_model=StoryOut)
def reject_story(story_id: str, payload: RejectBody, request: Request) -> StoryOut:
    """human_review -> queued with feedback; the next spec run sees the reason.

    A code-gate reject (base_ref set: the story has executed work) also pins
    `code_reject_head` so the rerun can prove the coder actually reacted —
    a round that commits nothing parks at the human gate instead of being
    reviewed as if the human's objection had been addressed."""
    with session_scope(request.app.state.engine) as db:
        story = _get_story(db, story_id)
        if story.status != StoryStatus.HUMAN_REVIEW:
            raise HTTPException(
                status_code=409,
                detail=f"only stories in human review can be rejected (story is {story.status})",
            )
        gate = "code" if story.base_ref else "spec"
        if story.base_ref:
            project_root = _story_project_root(request.app.state.engine, story.project_id)
            story.code_reject_head = None
            if project_root:
                try:
                    story.code_reject_head = (
                        workspaces.branch_head(project_root, story_id) or None
                    )
                except workspaces.WorkspaceError:
                    # an unreadable repo must never block the reject itself
                    story.code_reject_head = None
        prev = story.status
        story.status = StoryStatus.QUEUED
        story.queued_at = utcnow()
        # Reject permanence: a human rejection at EITHER gate permanently
        # disables the auto gates for this story (PATCH can never clear it —
        # update_story never writes this column).
        story.human_rejected = True
        # a rejected story goes back in the pool: whatever held it must
        # claim it again before working it (external) and the runner starts
        # clean (internal)
        story.claimed_by = None
        story.claimed_at = None
        _commit_and_publish(db, story, "status", {"from": prev, "to": story.status})
        # the gate marker rides as a SEPARATE event: the reason event must
        # stay exactly {reason, by} so `_reject_feedback` / `prev_feedback`
        # never mistake bookkeeping for a gripe
        if gate == "code":
            record_event(
                db,
                story.id,
                "note",
                {
                    "code_reject": True,
                    "gate": "code",
                    "by": payload.by,
                    "head": story.code_reject_head or "",
                },
            )
        record_event(db, story.id, "note", {"reason": payload.reason, "by": payload.by})
        return _effective_out(db, story)


# ---- operator retry / re-spec -----------------------------------------


# stages an operator may retry into; each maps onto a StoryStatus the
# scheduler already knows how to route (see runner._tick). `landing` re-arms
# the push+PR poller for a story that failed around its PR — it carries a
# `pr_index` guard in retry_story (nothing to watch without one).
RETRY_STAGES = {
    "ai_review": StoryStatus.AI_REVIEW,
    "human_review": StoryStatus.HUMAN_REVIEW,
    "executing": StoryStatus.EXECUTING,
    "queued": StoryStatus.QUEUED,
    "landing": StoryStatus.LANDING,
}

RETRYABLE_STATUSES = {StoryStatus.FAILED, StoryStatus.BACKLOG, StoryStatus.QUEUED}


# Only _cascade_failure writes this prefix (runner.py), so it marks a story
# that failed for no reason of its own. Matched as a PREFIX, never against the
# parent title inside it: with two failed parents the error names whichever
# failed FIRST, so an exact-title match would only ever restore on that one
# parent's retry (the v4 defect).
CASCADE_ERROR_PREFIX = "dependency failed: "


def _event_data(event: StoryEvent) -> dict:
    """Parsed `data` JSON of an event; junk (or a non-object payload) parses
    to {} so a malformed row can never crash a restore decision."""
    try:
        parsed = json.loads(event.data or "{}")
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _cascade_restore_target(db: Session, dependent: Story) -> str | None:
    """The status a cascade-failed dependent goes back to, or None when it
    must stay failed.

    Every condition is stated positively; ANY miss means left failed:
      - still `failed` with the cascade error prefix (its own failure, a
        cancel, an infra error or an untouched cascade never restores);
      - its last `to=failed` status event exists and came from backlog or
        queued — exactly what the cascade writes, nothing else is guessed;
      - no status event AFTER it (the story moved on its own: retried,
        re-queued, cancelled);
      - no `{"edited": True}` note event after it (a PATCH touched the story;
        a plain POST /notes note does NOT disqualify);
      - no dependency of it is failed right now (a second failed parent keeps
        it correctly failed until that parent is retried too).
    """
    if dependent.status != StoryStatus.FAILED:
        return None
    if not (dependent.error or "").startswith(CASCADE_ERROR_PREFIX):
        return None
    events = db.exec(
        select(StoryEvent)
        .where(StoryEvent.story_id == dependent.id)
        .order_by(StoryEvent.id)
    ).all()
    cascade = None
    for event in reversed(events):
        if event.kind == "status" and _event_data(event).get("to") == StoryStatus.FAILED:
            cascade = event
            break
    if cascade is None:
        return None
    restored = _event_data(cascade).get("from")
    if restored not in (StoryStatus.BACKLOG, StoryStatus.QUEUED):
        return None
    for event in events:
        if event.kind == "status" and event.id > cascade.id:
            return None  # it moved after the cascade
        if (
            event.kind == "note"
            and event.id > cascade.id
            and _event_data(event).get("edited") is True
        ):
            return None  # PATCHed since the cascade
    for dep_id in json.loads(dependent.depends_on or "[]"):
        dep = db.get(Story, dep_id)
        if dep is not None and dep.status == StoryStatus.FAILED:
            return None
    return restored


def _reopen_cascade_dependents(
    db: Session, retried: Story, _visited: set[str] | None = None
) -> None:
    """Undo a dependency cascade for the dependents that only failed because
    of `retried` (a failed story just retried or re-queued by an operator).

    Depth-first, same session: a restored dependent is itself a parent, so its
    own cascade victims come back too. Touched dependents (PATCHed, or moved
    on their own since the cascade) and dependents still blocked by another
    failed parent stay failed — and a dependent left failed is never recursed
    into (its own dependents keep their failure until it is retried).
    """
    visited = _visited if _visited is not None else set()
    visited.add(retried.id)
    # Python-side parse of depends_on (mirrors the prep scan in approve_story,
    # NOT a SQL substring match, which would false-positive on id prefixes)
    candidates = [
        candidate
        for candidate in db.exec(select(Story)).all()
        if retried.id in json.loads(candidate.depends_on or "[]")
    ]
    for dependent in candidates:
        if dependent.id in visited:
            continue
        restored = _cascade_restore_target(db, dependent)
        if restored is None:
            continue
        dependent.status = restored
        dependent.error = None
        _commit_and_publish(
            db,
            dependent,
            "status",
            {"from": StoryStatus.FAILED, "to": restored, "reopened": True},
        )
        record_event(
            db,
            dependent.id,
            "note",
            {"reopened": f"re-opened: {retried.title} was retried"},
        )
        _reopen_cascade_dependents(db, dependent, visited)


@router.post("/{story_id}/retry", response_model=StoryOut)
def retry_story(story_id: str, payload: RetryBody, request: Request) -> StoryOut:
    """Resume a failed/backlog/queued story at a chosen pipeline stage.

    `stage` selects where the story re-enters the pipeline; `base_ref` and
    `spec` are kept intact so the scheduler routes from there:
      - `ai_review` + base_ref -> code review only (the coder run is not
        repeated, its worktree is reused as-is)
      - `executing` + base_ref -> coder re-run in the existing worktree
        (partial work is preserved; the retry note's reason reaches the coder)
      - `queued` -> re-route per base_ref (spec pipeline when no worktree
        exists yet, coder pipeline when one does)
      - `human_review` -> straight to the human gate; a failed verify still
        409s the merge there (override_verify on /approve is that path)

    Worktree liveness: a failed coder run may still hold its worktree —
    retrying to `executing`/`ai_review` reuses it on purpose, preserving
    partial work. Call /re-spec (or /discard) first to start clean.

    Guarded to failed/backlog/queued: in-flight statuses belong to the
    runner's own task set, and done is terminal."""
    stage = payload.stage.strip().lower()
    if stage not in RETRY_STAGES:
        raise HTTPException(
            status_code=400,
            detail=f"stage must be one of {sorted(RETRY_STAGES)} (got {payload.stage!r})",
        )
    with session_scope(request.app.state.engine) as db:
        story = _get_story(db, story_id)
        if story.status not in RETRYABLE_STATUSES:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"only failed, backlog or queued stories can be retried "
                    f"(story is {story.status})"
                ),
            )
        prev = story.status
        if stage == "landing" and story.pr_index is None:
            raise HTTPException(
                status_code=409,
                detail="story has no pull request",
            )
        story.status = RETRY_STAGES[stage]
        # retry clears the failure bookkeeping but keeps base_ref/spec: the
        # scheduler uses them to route, and a claim from an external attempt
        # must not survive a manual resume. land_state too — a stale conflict
        # stamp would let the poller skip its fresh verdict.
        story.error = None
        story.land_state = None
        story.claimed_by = None
        story.claimed_at = None
        if story.status == StoryStatus.QUEUED:
            story.queued_at = utcnow()
        out = _commit_and_publish(
            db, story, "status", {"from": prev, "to": story.status, "retry": True}
        )
        record_event(
            db,
            story.id,
            "note",
            {
                "reason": f"operator retry from stage {stage}",
                "by": payload.by,
                "feedback": False,
            },
        )
        # the retry that revived this story also revives the dependents the
        # cascade failed over it (untouched ones only — see the helper)
        _reopen_cascade_dependents(db, story)
        return out


@router.post("/{story_id}/retry-land", response_model=StoryOut)
def retry_land(story_id: str, request: Request, payload: RetryLandBody | None = None) -> StoryOut:
    """Re-arm a stalled push+PR landing: ask Gitea to update the PR's branch
    from its base (the usual fix for a conflict the base introduced), clear the
    failure reason, and put the story back in `landing` for the poller.

    Only for stories that carry a PR (pr_index set) and sit at the gate
    (human_review) or are still landing — done/failed/backlog have nothing to
    retry, and a story without a PR was never on the landing path. Approving
    again at the gate is the other half of the same recovery (idempotent
    re-land), but this endpoint exists so an operator (or MCP agent) can retry
    WITHOUT re-pushing a branch that already carries the work."""
    payload = payload or RetryLandBody()
    with session_scope(request.app.state.engine) as db:
        story = _get_story(db, story_id)
        if story.pr_index is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    "story has no pull request to retry — only push+PR stories "
                    "can retry a landing"
                ),
            )
        if story.status not in (StoryStatus.LANDING, StoryStatus.HUMAN_REVIEW):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"only landing or human-review stories can retry a land "
                    f"(story is {story.status})"
                ),
            )
        project = db.get(Project, story.project_id) if story.project_id else None
        if project is None or not all(
            (project.pr_api_base, project.pr_repo, project.pr_token_file)
        ):
            raise HTTPException(
                status_code=409,
                detail="pull request configuration is incomplete on this project",
            )
        return _update_branch_and_reland(db, story, project, payload.by)


def _update_branch_and_reland(
    db: Session, story: Story, project: Project, by: str | None
) -> StoryOut:
    """Ask Gitea to update the PR branch from its base and put the story back
    in `landing`. A merged PR is simply done; an up-to-date branch reruns CI
    on the current head; any other refusal parks the story at the gate and
    answers 409. Shared by retry-land and the approve of a "main moved"
    gate story."""
    from .. import pr as pr_module

    def _merged_done() -> StoryOut:
        """The PR merged meanwhile: the story is simply done, not failed.

        Same shape as the poller's merged path — error and land_state
        cleared, a status event (merged: True) published, 200 to the
        caller."""
        prev = story.status
        story.status = StoryStatus.DONE
        story.error = None
        story.land_state = None
        out = _commit_and_publish(
            db,
            story,
            "status",
            {
                "from": prev,
                "to": story.status,
                "retry_land": True,
                "merged": True,
                "pr_index": story.pr_index,
                "pr_url": story.pr_url,
            },
        )
        record_event(
            db,
            story.id,
            "note",
            {
                "delivery": "push",
                "retry_land": True,
                "merged": True,
                "pr_index": story.pr_index,
                "pr_url": story.pr_url,
                "by": by,
            },
        )
        return out

    # A merged PR must never retry: asking Gitea to update a merged
    # branch 422s ("already merged"), which used to become
    # `land failed:` + a 409 (incident 6a438b3d). The decision comes
    # from the PR's state, never from a message string.
    try:
        pre = pr_module.get_pull(
            project.pr_api_base,
            project.pr_repo,
            pr_module.read_token(project.pr_token_file),
            int(story.pr_index),
        )
    except pr_module.PullRequestError:
        # unreadable PR: fall through to update_branch, which reports
        # the real error on its own
        pre = None
    if pre is not None and pre.get("merged"):
        return _merged_done()

    def _land_failed(message: str) -> None:
        """Park the story at the human gate with the failure reason — the
        shape EVERY land error takes (story.error set, a note event, a
        status commit published); the caller adds the 409 on top. Shared
        by the generic branch below and the rerun/lookup failures inside
        the up-to-date recovery, so a rerun that cannot be asked for is
        booked exactly like any other failed land."""
        story.status = StoryStatus.HUMAN_REVIEW
        story.error = f"land failed: {message}"
        record_event(
            db,
            story.id,
            "note",
            {
                "delivery": "push",
                "retry_land": True,
                "error": message,
                "pr_index": story.pr_index,
            },
        )
        _commit_and_publish(
            db,
            story,
            "status",
            {
                "from": story.status,
                "to": story.status,
                "retry_land": True,
                "error": message,
            },
        )

    try:
        token = pr_module.read_token(project.pr_token_file)
        pr_module.update_branch(
            project.pr_api_base, project.pr_repo, token, int(story.pr_index)
        )
    except pr_module.PullRequestError as e:
        # the branch update can still race a just-merged PR (or Gitea
        # may 4xx "already merged" for another reason): re-read the PR
        # — merged means success, not failure
        try:
            post = pr_module.get_pull(
                project.pr_api_base,
                project.pr_repo,
                pr_module.read_token(project.pr_token_file),
                int(story.pr_index),
            )
        except pr_module.PullRequestError:
            post = None
        if post is not None and post.get("merged"):
            return _merged_done()
        if isinstance(e, pr_module.PullRequestUpToDateError):
            # Gitea refused the update because the PR branch already
            # contains its base (it reports this as a misused 500 with
            # an exact message): nothing moved, so the pre-read head is
            # still current — re-run CI on it and let the poller land
            # the story. Falls into the same shape as the success path
            # (landing, error/land_state cleared); the poller's existing
            # no-CI handling covers a head that has no run at all.
            token = pr_module.read_token(project.pr_token_file)
            head_sha = (pre or {}).get("head_sha") or (
                post.get("head_sha") if post else None
            )
            rerun_note: dict = {
                "delivery": "push",
                "retry_land": True,
                "up_to_date": True,
                "pr_index": story.pr_index,
            }
            try:
                run_id = (
                    pr_module.find_latest_workflow_run(
                        project.pr_api_base,
                        project.pr_repo,
                        token,
                        str(head_sha),
                    )
                    if head_sha
                    else None
                )
                if run_id is not None:
                    pr_module.rerun_workflow_run(
                        project.pr_api_base,
                        project.pr_repo,
                        token,
                        int(run_id),
                    )
            except pr_module.PullRequestError as rerun_error:
                # a rerun Gitea refuses (Actions off, missing rights,
                # unreachable) is a failed land like any other: the same
                # story.error + note + published commit as the generic
                # path below, then the 409 to the caller
                _land_failed(str(rerun_error))
                raise HTTPException(
                    status_code=409,
                    detail=f"retry-land failed: {rerun_error}",
                ) from rerun_error
            rerun_note["run_id"] = run_id
            rerun_note["head_sha"] = str(head_sha) if head_sha else None
            rerun_note["reason"] = (
                f"retry_land: rerun of run {run_id}"
                if run_id is not None
                else "retry_land: no workflow run found"
            )
            record_event(db, story.id, "note", rerun_note)
            prev = story.status
            story.status = StoryStatus.LANDING
            story.error = None
            story.land_state = None
            return _commit_and_publish(
                db,
                story,
                "status",
                {
                    "from": prev,
                    "to": story.status,
                    "retry_land": True,
                    "pr_index": story.pr_index,
                    "pr_url": story.pr_url,
                },
            )
        _land_failed(str(e))
        raise HTTPException(
            status_code=409, detail=f"retry-land failed: {str(e)}"
        ) from e
    prev = story.status
    story.status = StoryStatus.LANDING
    story.error = None
    story.land_state = None
    out = _commit_and_publish(
        db,
        story,
        "status",
        {
            "from": prev,
            "to": story.status,
            "retry_land": True,
            "pr_index": story.pr_index,
            "pr_url": story.pr_url,
        },
    )
    record_event(
        db,
        story.id,
        "note",
        {
            "delivery": "push",
            "retry_land": True,
            "pr_index": story.pr_index,
            "pr_url": story.pr_url,
            "by": by,
        },
    )
    return out


@router.post("/{story_id}/unland", response_model=StoryOut)
def unland_story(
    story_id: str, request: Request, payload: RetryLandBody | None = None
) -> StoryOut:
    """Pull a landing merge-train story back to the human gate.

    An open batch is cancelled as a whole (its PR is closed, every member
    bounces); a batch still forming only loses this story."""
    from .. import train as train_module
    from ..models import TrainBatch, TrainBatchState

    payload = payload or RetryLandBody()
    with session_scope(request.app.state.engine) as db:
        story = _get_story(db, story_id)
        if story.status != StoryStatus.LANDING:
            raise HTTPException(
                status_code=409,
                detail=f"only landing stories can be unlanded (story is {story.status})",
            )
        batch = db.get(TrainBatch, story.train_batch_id) if story.train_batch_id else None
        if batch is None or batch.state == TrainBatchState.MERGED:
            raise HTTPException(
                status_code=409,
                detail="story is not in an unmerged merge-train batch",
            )
        project = db.get(Project, story.project_id) if story.project_id else None
        if project is None:
            raise HTTPException(status_code=409, detail="story has no project")
        try:
            train_module.unland(db, project, story, payload.by)
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        db.refresh(story)
        return _story_out(story, _project_executor(db, story))


@router.post("/{story_id}/supersede", response_model=StoryOut)
def supersede_story(
    story_id: str, request: Request, payload: SupersedeBody | None = None
) -> StoryOut:
    """Close a FAILED or HUMAN_REVIEW story as superseded: its work was made
    irrelevant by another one (a duplicate, a hand-landed fix, an abandoned
    direction).

    Terminal like `done` — the story leaves the failed lane and is excluded
    from runner pickup and every retry set, so the failed lane holds only
    actionable failures. `superseded_by` names the story id (or a free-text
    note) that made the work irrelevant; the event trail records both it and
    the operator that closed the story.

    Superseding at the human gate also tears the delivery down best-effort:
    the worktree/branch are discarded (same as re-spec) and an open pull
    request is commented and closed — a failure there is recorded in the
    note event, never fatal. Still-running stories (in_progress, ai_review,
    executing, landing) are refused with 409: cancel/kill them first, they
    belong to the runner or the land poller. done is already terminal, and
    backlog/queued have no work to supersede."""
    payload = payload or SupersedeBody()
    with session_scope(request.app.state.engine) as db:
        story = _get_story(db, story_id)
        if story.status in (
            StoryStatus.LANDING,
            StoryStatus.IN_PROGRESS,
            StoryStatus.AI_REVIEW,
            StoryStatus.EXECUTING,
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"story is still running ({story.status}) — cancel the "
                    f"story first if its work was superseded"
                ),
            )
        if story.status not in (StoryStatus.FAILED, StoryStatus.HUMAN_REVIEW):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"only failed or human-review stories can be superseded "
                    f"(story is {story.status})"
                ),
            )
        at_gate = story.status == StoryStatus.HUMAN_REVIEW
        row = db.get(Project, story.project_id) if story.project_id else None
        # Snapshot the PR config as a detached value object: the ORM row
        # expires on commit (expire_on_commit), so every pr_* read after
        # session_scope closes raised DetachedInstanceError — a Gitea outage
        # then 500s the supersede instead of being recorded as the
        # best-effort PR failure it is meant to be.
        project = (
            SimpleNamespace(
                pr_api_base=row.pr_api_base,
                pr_repo=row.pr_repo,
                pr_token_file=row.pr_token_file,
            )
            if row and all((row.pr_api_base, row.pr_repo, row.pr_token_file))
            else None
        )
        pr_index = story.pr_index
        prev = story.status
        story.status = StoryStatus.SUPERSEDED
        story.error = None
        story.land_state = None
        out = _commit_and_publish(
            db,
            story,
            "status",
            {
                "from": prev,
                "to": story.status,
                "superseded": True,
                "superseded_by": payload.superseded_by or None,
                "by": payload.by,
            },
        )
        record_event(
            db,
            story.id,
            "note",
            {
                "superseded": True,
                "superseded_by": payload.superseded_by or None,
                "by": payload.by,
                "feedback": False,
            },
        )

    if not at_gate:
        # failed stories were already torn down by the pipeline that failed
        # them; only the gate holds live work (worktree, branch, open PR)
        return out

    # best-effort teardown outside the story transaction (same deal as
    # re-spec): a failure here must not roll back the transition
    project_root = load_settings(request.app.state.engine).get("project_root")
    if project_root:
        try:
            workspaces.discard(project_root, story_id)
        except workspaces.WorkspaceError as e:
            log.debug("discarding worktree for superseded story %s failed: %s", story_id, e)

    pr_error: str | None = None
    if pr_index is not None and project and all(
        (project.pr_api_base, project.pr_repo, project.pr_token_file)
    ):
        from .. import pr as pr_module

        try:
            token = pr_module.read_token(project.pr_token_file)
            pr_module.create_pull_comment(
                project.pr_api_base,
                project.pr_repo,
                token,
                int(pr_index),
                body=(
                    f"Superseded{f' by {payload.superseded_by}' if payload.superseded_by else ''}"
                    f"{f' by {payload.by}' if payload.by else ''} — closing without merging."
                ),
            )
            pr_module.close_pull(project.pr_api_base, project.pr_repo, token, int(pr_index))
        except pr_module.PullRequestError as e:
            # recorded in the note event, never fatal: DB truth must not
            # hinge on Gitea (an already-merged PR 409s on close)
            pr_error = str(e)

    if pr_error is not None or pr_index is not None:
        with session_scope(request.app.state.engine) as db:
            record_event(
                db,
                story_id,
                "note",
                {
                    "superseded": True,
                    "superseded_by": payload.superseded_by or None,
                    "by": payload.by,
                    "feedback": False,
                    "pr_index": pr_index,
                    "pr_closed": pr_error is None,
                    **({"pr_error": pr_error} if pr_error else {}),
                },
            )
    return out


@router.post("/{story_id}/re-spec", response_model=StoryOut)
def respec_story(story_id: str, request: Request, payload: ReSpecBody | None = None) -> StoryOut:
    """Throw the generated spec away and start the pipeline from spec
    generation: spec/base_ref/claim cleared, spec_version back to 0 (the next
    generated spec becomes v1 again), status queued.

    The worktree of an internal story is discarded best-effort: a stale one
    would otherwise re-pin `base_ref` onto the old coder commits and show
    already-merged work as this run's diff. Old spec events stay in the
    history (the event log is append-only); only the current spec is dropped.

    Same status guard as /retry (failed/backlog/queued)."""
    payload = payload or ReSpecBody()
    with session_scope(request.app.state.engine) as db:
        story = _get_story(db, story_id)
        if story.status not in RETRYABLE_STATUSES:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"only failed, backlog or queued stories can be re-specced "
                    f"(story is {story.status})"
                ),
            )
        prev = story.status
        execution = story.execution or StoryExecution.INTERNAL
        db.add(story)

    # best-effort worktree teardown outside the story transaction: a failure
    # here must not roll back the transition, and the discard itself needs no
    # story row at all — only the path convention
    project_root = load_settings(request.app.state.engine).get("project_root")
    if execution == StoryExecution.INTERNAL and project_root:
        try:
            workspaces.discard(project_root, story_id)
        except workspaces.WorkspaceError as e:
            # recorded, never fatal: the re-pinned base_ref guard in
            # _execute_story already handles a missing/rebuilt worktree
            log.debug("discarding worktree for re-specced story %s failed: %s", story_id, e)

    with session_scope(request.app.state.engine) as db:
        story = _get_story(db, story_id)
        story.spec = None
        story.spec_version = 0
        story.spec_rejects = 0  # the reject loop restarts with the spec
        story.spec_history = None
        story.base_ref = None
        # hygiene: base_ref restarts from scratch, so the code-gate reject
        # marker must not survive into the next execution round (step 6b)
        story.code_reject_head = None
        story.claimed_by = None
        story.claimed_at = None
        story.error = None
        story.queued_at = utcnow()
        story.status = StoryStatus.QUEUED
        out = _commit_and_publish(
            db, story, "status", {"from": prev, "to": story.status, "re_spec": True}
        )
        record_event(
            db,
            story.id,
            "note",
            {"reason": "re-spec by operator", "by": payload.by, "feedback": False},
        )
        return out


# ---- external execution ----------------------------------------------


def _external_wip_count(db: Session, project_id: str | None) -> int:
    """Claimed external stories currently in progress for one scope.
    project_id None means the global scope (stories with no project)."""
    return len(
        db.exec(
            select(Story).where(
                Story.project_id == project_id,
                Story.execution == StoryExecution.EXTERNAL,
                Story.status == StoryStatus.IN_PROGRESS,
                Story.claimed_by.is_not(None),
            )
        ).all()
    )


@router.post("/{story_id}/claim", response_model=StoryOut)
def claim_story(story_id: str, payload: ClaimBody, request: Request) -> StoryOut:
    """An external agent takes ownership of a queued story.

    WIP guard: at most `max_external_wip_per_project` claimed external
    stories may be in_progress per scope (project, or global when the story
    has no project). The count runs inside the same session_scope transaction
    as the claim itself; SQLite serializes writers, so two concurrent claims
    cannot both observe a free slot. On a multi-writer deployment this needs
    a real row lock or a UNIQUE partial index."""
    claimed_by = (payload.claimed_by or "").strip()
    if not claimed_by:
        raise HTTPException(status_code=422, detail="claimed_by must be a non-empty name")
    with session_scope(request.app.state.engine) as db:
        story = _get_story(db, story_id)
        project_executor = _project_executor(db, story)
        if effective_execution(story, project_executor) != StoryExecution.EXTERNAL:
            raise HTTPException(
                status_code=409,
                detail="only external stories can be claimed "
                "(this story runs on the internal pipeline)",
            )
        if story.status != StoryStatus.QUEUED:
            raise HTTPException(
                status_code=409,
                detail=f"only queued stories can be claimed (story is {story.status})",
            )
        if not _deps_met(db, story):
            raise HTTPException(
                status_code=409,
                detail="dependencies are not done yet — the story cannot be claimed",
            )
        limit = int(
            load_settings(request.app.state.engine).get("max_external_wip_per_project")
            or 1
        )
        if _external_wip_count(db, story.project_id) >= limit:
            scope = story.project_id or "the global scope"
            raise HTTPException(
                status_code=409,
                detail=f"external WIP limit reached for {scope} "
                f"({limit} in-progress claimed story"
                f"{'s' if limit != 1 else ''}) — release or complete one first",
            )
        prev = story.status
        story.status = StoryStatus.IN_PROGRESS
        story.claimed_by = claimed_by
        story.claimed_at = utcnow()
        story.error = None
        # materialize the inherited executor: from here on the lease sweep,
        # WIP count, complete/release and the approve external branch all
        # read a concrete execution value and keep working unmodified
        story.execution = StoryExecution.EXTERNAL
        return _commit_and_publish(
            db,
            story,
            "status",
            {"from": prev, "to": story.status, "claimed_by": claimed_by},
        )


@router.post("/{story_id}/complete", response_model=StoryOut)
def complete_story(
    story_id: str, request: Request, payload: CompleteBody | None = None
) -> StoryOut:
    """The claimed agent is finished: code landed via PR/CI, so the story
    moves straight to the human gate. `claimed_by` is kept for display.
    The body (and its note) is optional."""
    payload = payload or CompleteBody()
    with session_scope(request.app.state.engine) as db:
        story = _get_story(db, story_id)
        if story.execution != StoryExecution.EXTERNAL:
            raise HTTPException(
                status_code=409,
                detail="only external stories can be completed by an agent",
            )
        if story.status != StoryStatus.IN_PROGRESS or not story.claimed_by:
            raise HTTPException(
                status_code=409,
                detail="only a claimed, in-progress story can be completed "
                f"(story is {story.status})",
            )
        prev = story.status
        story.status = StoryStatus.HUMAN_REVIEW
        note = (payload.note or "").strip()
        data = {
            "from": prev,
            "to": story.status,
            "completed_by": story.claimed_by,
        }
        if note:
            data["note"] = note
        out = _commit_and_publish(db, story, "status", data)
        if note:
            record_event(
                db, story.id, "note", {"author": story.claimed_by, "completion": note}
            )
        return out


@router.post("/{story_id}/evidence", response_model=EvidenceOut)
def attach_evidence(story_id: str, payload: EvidenceIn, request: Request) -> EvidenceOut:
    """Outcome evidence from an external executor: the diff it landed, how it
    verified, and a human-readable summary.

    Deliberately status-blind: any non-deleted story accepts evidence, done
    included — an executor may report after the human gate has closed, and a
    correction is just another POST (latest wins, previous content discarded).
    No internal/external guard either: an internal story can carry external
    evidence alongside its worktree diff.

    `summary` and `verify.output` are secret-redacted (same convention as
    notes); `code_diff` is left untouched — redaction would mangle legit code.
    Event data stays compact: a summary preview, the verify verdict and the
    diff byte length — never the diff itself."""
    from ..agent import strip_placeholders
    from ..guardrails import redact_secrets

    with session_scope(request.app.state.engine) as db:
        story = _get_story(db, story_id)
        summary = payload.summary
        if summary is not None:
            # secret-redacted, then stripped of provider placeholder lines
            # ('[System: …]') — no placeholder may end up in a stored
            # deliverable (same convention as runner._finish)
            summary = strip_placeholders(redact_secrets(summary)).strip() or None
        verify: dict | None = None
        if payload.verify is not None:
            verify = {
                "passed": payload.verify.passed,
                "command": payload.verify.command,
                "output": redact_secrets(payload.verify.output),
            }
        stored = EvidenceOut(code_diff=payload.code_diff, verify=verify, summary=summary)
        story.evidence = json.dumps(stored.model_dump())
        event_data: dict = {"diff_bytes": len(payload.code_diff.encode("utf-8"))}
        if summary:
            event_data["summary"] = summary[:120]
        if verify is not None:
            event_data["verify_passed"] = verify["passed"]
        _commit_and_publish(db, story, "evidence", event_data)
        return stored


@router.post("/{story_id}/cancel", response_model=StoryOut)
async def cancel_story(
    story_id: str, request: Request, payload: CancelBody | None = None
) -> StoryOut:
    """Abort an in-flight run: the runner's asyncio task is cancelled, the
    partial worktree is PRESERVED — uncommitted changes are committed as
    "wip: cancelled" onto fubala/<story_id> — and the story lands in failed
    with error "cancelled by <by>". /discard (or /re-spec, or delete) remains
    the only removal path. Unresolved conflicts skip the wip commit (recorded
    as a note) so markers are never committed as a resolution.

    Allowed from in_progress/executing/ai_review — the statuses an operator
    can plausibly want to interrupt. Orphans (in flight before a restart, no
    task handle) cancel as a pure DB transition. External stories never get
    near workspaces: their code lives outside Fubala."""
    cancellable = {
        StoryStatus.IN_PROGRESS,
        StoryStatus.EXECUTING,
        StoryStatus.AI_REVIEW,
    }
    payload = payload or CancelBody()
    project_root_global = load_settings(request.app.state.engine).get("project_root")
    with session_scope(request.app.state.engine) as db:
        story = _get_story(db, story_id)
        if story.status not in cancellable:
            raise HTTPException(
                status_code=409,
                detail=(
                    "only in-flight stories can be cancelled "
                    f"(story is {story.status})"
                ),
            )
        execution = story.execution or StoryExecution.INTERNAL
        base_ref = story.base_ref
        story_project_root = None
        if execution != StoryExecution.EXTERNAL and story.project_id:
            project = db.get(Project, story.project_id)
            story_project_root = project.root if project is not None else None

    # abort the pipeline first: once the task is dead it cannot race the
    # transition below (its own status guards make late writes no-ops anyway)
    await request.app.state.story_runner.cancel(story_id)

    # reuses _fail: keeps the dependent cascade + event/publish semantics
    request.app.state.story_runner._fail(story_id, f"cancelled by {payload.by}")

    if execution == StoryExecution.EXTERNAL:
        # DB transition only: free the WIP slot the claim was holding
        with session_scope(request.app.state.engine) as db:
            story = _get_story(db, story_id)
            story.claimed_by = None
            story.claimed_at = None
            db.add(story)
        return _fetch_story_out(request, story_id)

    # internal: preserve the partial worktree as a wip commit on the story
    # branch (the branch keeps `base_ref` so /retry and /discard keep
    # working, and the work survives for inspection or landing).
    # The worktree can exist WITHOUT a pinned base_ref yet: the story polls
    # "executing + worktree exists" while workspaces.create() is still in
    # flight, and the runner commits base_ref only in the session AFTER it
    # returns. A cancel landing in that window must still be handled here —
    # or the scratch checkout outlives the story (test_cancel_aborts_inflight_
    # coder_run). Resolve the worktree's own project root first — for a
    # story whose project root differs from the global setting, the global
    # root resolves to the wrong repo and the worktree ops silently no-op.
    if execution != StoryExecution.EXTERNAL and (
        base_ref or workspaces.worktree_exists(story_project_root or project_root_global, story_id)
    ):
        project_root = story_project_root or project_root_global
        if project_root:
            try:
                unresolved = workspaces.unmerged(project_root, story_id)
                if unresolved:
                    # `git add -A` inside commit() would stage conflict
                    # markers as if they were a resolution — never commit
                    # through a conflicted tree; surface it instead
                    with session_scope(request.app.state.engine) as db:
                        record_event(
                            db,
                            story_id,
                            "note",
                            {
                                "wip": "skipped — unresolved conflicts",
                                "paths": unresolved[:10],
                            },
                        )
                else:
                    workspaces.commit(project_root, story_id, "wip: cancelled")
            except workspaces.WorkspaceError as e:
                with session_scope(request.app.state.engine) as db:
                    record_event(
                        db,
                        story_id,
                        "note",
                        {"worktree": "wip commit failed after cancel", "error": str(e)},
                    )
                log.warning("wip commit after cancel failed for %s: %s", story_id, e)
    return _fetch_story_out(request, story_id)


def _fetch_story_out(request: Request, story_id: str) -> StoryOut:
    with session_scope(request.app.state.engine) as db:
        return _effective_out(db, _get_story(db, story_id))


@router.post("/{story_id}/release", response_model=StoryOut)
def release_story(
    story_id: str, request: Request, payload: ReleaseBody | None = None
) -> StoryOut:
    """Stale-claim escape: the claimer is gone or gives up — drop the claim,
    back to queued so another agent (or a human requeue) can pick it up.
    This also frees the per-project WIP slot the claim was holding, so a
    different story in the same scope becomes claimable again.

    Anyone may release, not just the claimer: `claimed_by` is self-declared
    with no authentication, so a claimer-only rule would be trivially forged
    and add no protection (single-admin reality). Who released is recorded
    in the event as `by` — from the optional body, `"local"` when omitted
    (a bare POST stays valid, exactly like `/complete`)."""
    payload = payload or ReleaseBody()
    with session_scope(request.app.state.engine) as db:
        story = _get_story(db, story_id)
        if story.execution != StoryExecution.EXTERNAL:
            raise HTTPException(
                status_code=409,
                detail="only external stories can be released",
            )
        if story.status != StoryStatus.IN_PROGRESS or not story.claimed_by:
            raise HTTPException(
                status_code=409,
                detail=f"story is {story.status} and not currently claimed",
            )
        payload = release_claim(db, story, by=payload.by)
        db.commit()
        bus.publish("story", {"story": payload})
        return StoryOut(**payload)


def _deps_met(db: Session, story: Story) -> bool:
    """True when every dependency exists and is done (mirrors the runner's
    gate so claim and scheduler agree on eligibility)."""
    for dep_id in json.loads(story.depends_on or "[]"):
        dep = db.get(Story, dep_id)
        if dep is None or dep.status != StoryStatus.DONE:
            return False
    return True
