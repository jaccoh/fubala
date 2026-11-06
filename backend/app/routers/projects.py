"""Projects (workspaces) and live permission decisions."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from .. import brief as brief_module
from ..db import session_scope
from ..events import bus
from ..models import (
    PermissionRequest,
    Project,
    ProjectBriefVersion,
    ProjectDelivery,
    Story,
    StoryExecution,
    StoryGate,
    utcnow,
)
from ..schemas import as_utc_iso
from ..settings_store import load_settings, save_settings

log = logging.getLogger(__name__)

router = APIRouter(prefix="/projects", tags=["projects"])

# The merge train needs the same wiring the per-story PR needed: push delivery
# plus a complete pr_* set. Refused at project create/patch time (422) so the
# misconfiguration can never reach a story approval.
_TRAIN_WIRING_ERROR = (
    "merge train requires delivery=push and the full pr_* wiring "
    "(pr_api_base, pr_repo, pr_base_branch, pr_token_file)"
)


def _train_ready(project: Project) -> bool:
    from .. import train as train_module

    return train_module.train_ready(project)


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    root: str = Field(min_length=1)
    extra_paths: list[str] = Field(default_factory=list)
    verify_command: str | None = None
    test_command: str | None = Field(
        default=None,
        description="Test command the coder may run via run_tests "
        "(empty = the tool is not exposed)",
    )
    # Str + validator (not Literal) so the 422 message names the field:
    # "Value error, executor must be one of [...]". PATCH returns 400 with the
    # same wording (see update_project).
    executor: str = Field(
        default=StoryExecution.INTERNAL,
        description='"internal" = the server runner; "external" = outside '
        "agents claim the project's stories",
    )

    @field_validator("executor")
    @classmethod
    def _executor_must_be_known(cls, value: str) -> str:
        if value not in StoryExecution.ALL:
            raise ValueError(f"executor must be one of {list(StoryExecution.ALL)}")
        return value

    delivery: str = Field(
        default=ProjectDelivery.MERGE,
        description='"merge" = merge the story branch into the root checkout; '
        '"push" = push the branch to the remote (optional Gitea PR)',
    )

    @field_validator("delivery")
    @classmethod
    def _delivery_must_be_known(cls, value: str) -> str:
        if value not in ProjectDelivery.ALL:
            raise ValueError(f"delivery must be one of {list(ProjectDelivery.ALL)}")
        return value

    pr_api_base: str | None = None
    pr_web_base: str | None = None
    pr_repo: str | None = None
    pr_base_branch: str | None = None
    pr_token_file: str | None = None
    ab_headless_share: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Share of new stories A/B'd to the headless CODE "
        "executor (0..1; None = leave unchanged)",
    )
    default_gate_spec: str | None = Field(
        default=None,
        description='Gate mode new stories start with at the SPEC gate ("human" | "auto")',
    )
    default_gate_code: str | None = Field(
        default=None,
        description='Gate mode new stories start with at the CODE gate ("human" | "auto")',
    )
    protected_globs: str | None = Field(
        default=None,
        max_length=2000,
        description="Comma-separated globs the auto code gate refuses to touch",
    )
    frozen_globs: str | None = Field(
        default=None,
        max_length=2000,
        description="Comma-separated globs no gate may ever touch",
    )
    auto_max_diff_lines: int | None = Field(
        default=None,
        ge=0,
        description="Max added+removed diff lines an auto code gate may land (0 = no cap)",
    )
    merge_train: bool | None = Field(
        default=None,
        description="Batch gate-approved stories into one batch PR per window",
    )
    train_window_s: int | None = Field(
        default=None,
        ge=1,
        description="Seconds a forming batch waits before flushing",
    )
    train_max: int | None = Field(
        default=None,
        ge=1,
        description="Members at which a forming batch flushes immediately",
    )
    brief_token_budget: int | None = Field(
        default=None,
        ge=1,
        description="Approximate token budget for the project brief (default 8000)",
    )
    brief_in_spec_prompt: bool | None = Field(
        default=None,
        description="Send the brief's Owner rules and Non-goals sections (plus "
        "the story's epic description) to the spec writer (default off)",
    )
    code_graph_enabled: bool | None = Field(
        default=None,
        description="code-review-graph: MCP server for CLI sessions and an "
        "impact report at every code review (default off)",
    )

    @field_validator("default_gate_spec", "default_gate_code")
    @classmethod
    def _gate_must_be_known(cls, value: str | None) -> str | None:
        if value is not None and value not in StoryGate.ALL:
            raise ValueError(f"gate must be one of {list(StoryGate.ALL)}")
        return value


class ProjectUpdate(BaseModel):
    name: str | None = None
    root: str | None = None
    extra_paths: list[str] | None = None
    verify_command: str | None = None
    test_command: str | None = Field(
        default=None,
        description="Test command the coder may run via run_tests; an explicit "
        "null/empty clears it (the tool is then not exposed)",
    )
    enabled: bool | None = None
    executor: str | None = Field(
        default=None,
        description="Default executor for the project's stories (read-time "
        "inheritance: existing stories are not rewritten)",
    )
    delivery: str | None = Field(
        default=None,
        description="merge (root checkout) or push (remote branch + optional PR)",
    )
    pr_api_base: str | None = None
    pr_web_base: str | None = None
    pr_repo: str | None = None
    pr_base_branch: str | None = None
    pr_token_file: str | None = None
    ab_headless_share: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Share of new stories A/B'd to the headless CODE "
        "executor (0..1; None = leave unchanged)",
    )
    default_gate_spec: str | None = Field(
        default=None,
        description="Gate mode new stories start with at the SPEC gate "
        "(human | auto); explicit null/empty resets to human",
    )
    default_gate_code: str | None = Field(
        default=None,
        description="Gate mode new stories start with at the CODE gate "
        "(human | auto); explicit null/empty resets to human",
    )
    protected_globs: str | None = Field(
        default=None,
        max_length=2000,
        description="Comma-separated globs the auto code gate refuses to "
        "touch; explicit null/empty clears the list",
    )
    frozen_globs: str | None = Field(
        default=None,
        max_length=2000,
        description="Comma-separated globs no gate may ever touch; explicit "
        "null/empty clears the list",
    )
    auto_max_diff_lines: int | None = Field(
        default=None,
        ge=0,
        description="Max added+removed diff lines an auto code gate may "
        "land (0 = no cap); None = leave unchanged",
    )
    merge_train: bool | None = Field(
        default=None,
        description="Turn the merge train on/off; None = leave unchanged",
    )
    train_window_s: int | None = Field(
        default=None,
        ge=1,
        description="Seconds a forming batch waits before flushing; "
        "None = leave unchanged",
    )
    train_max: int | None = Field(
        default=None,
        ge=1,
        description="Members at which a forming batch flushes immediately; "
        "None = leave unchanged",
    )
    brief_token_budget: int | None = Field(
        default=None,
        ge=1,
        description="Approximate token budget for the project brief; "
        "None = leave unchanged",
    )
    brief_in_spec_prompt: bool | None = Field(
        default=None,
        description="Send only the brief's Owner rules and Non-goals sections "
        "(plus the story's epic description) to the spec writer; "
        "None = leave unchanged",
    )
    code_graph_enabled: bool | None = Field(
        default=None,
        description="code-review-graph on or off for this project; "
        "None = leave unchanged",
    )

    @field_validator("default_gate_spec", "default_gate_code")
    @classmethod
    def _gate_must_be_known(cls, value: str | None) -> str | None:
        if value is not None and value not in StoryGate.ALL:
            raise ValueError(f"gate must be one of {list(StoryGate.ALL)}")
        return value


def _project_out(db: Session, project: Project) -> dict:
    story_count = len(
        db.exec(select(Story).where(Story.project_id == project.id)).all()
    )
    return {
        "id": project.id,
        "name": project.name,
        "root": project.root,
        "extra_paths": json.loads(project.extra_paths or "[]"),
        "verify_command": project.verify_command,
        "test_command": project.test_command,
        "enabled": project.enabled,
        "executor": project.executor or StoryExecution.INTERNAL,
        "delivery": project.delivery or ProjectDelivery.MERGE,
        "pr_api_base": project.pr_api_base,
        "pr_web_base": project.pr_web_base,
        "pr_repo": project.pr_repo,
        "pr_base_branch": project.pr_base_branch,
        "pr_token_file": project.pr_token_file,
        "ab_headless_share": project.ab_headless_share
        if project.ab_headless_share is not None
        else 0.0,
        "default_gate_spec": project.default_gate_spec or StoryGate.HUMAN,
        "default_gate_code": project.default_gate_code or StoryGate.HUMAN,
        "protected_globs": project.protected_globs or "",
        "frozen_globs": project.frozen_globs or "",
        "auto_max_diff_lines": project.auto_max_diff_lines
        if project.auto_max_diff_lines is not None
        else 400,
        "merge_train": bool(project.merge_train),
        "train_window_s": int(project.train_window_s or 300),
        "train_max": int(project.train_max or 6),
        "train_paused": bool(project.train_paused),
        "train_paused_by": project.train_paused_by if project.train_paused else None,
        "train_pause_reason": (
            project.train_pause_reason if project.train_paused else None
        ),
        "train_paused_at": (
            as_utc_iso(project.train_paused_at)
            if project.train_paused and project.train_paused_at
            else None
        ),
        "brief_token_budget": brief_module.brief_budget(project),
        "brief_in_spec_prompt": bool(project.brief_in_spec_prompt),
        "code_graph_enabled": bool(project.code_graph_enabled),
        "story_count": story_count,
        "root_exists": Path(project.root).exists(),
        "root_is_repo": (Path(project.root) / ".git").exists(),
    }


def _get_project(db: Session, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return project


@router.get("")
def list_projects(request: Request) -> list[dict]:
    with session_scope(request.app.state.engine) as db:
        projects = db.exec(select(Project).order_by(Project.name)).all()
        return [_project_out(db, p) for p in projects]


class FsBrowseOut(BaseModel):
    path: str
    parent: str | None
    dirs: list[dict]
    home: str


@router.get("/fs/browse", response_model=FsBrowseOut)
def browse_folders(path: str | None = None) -> FsBrowseOut:
    """Directory browser for the zcode-style 'set a folder' pickers: lists
    subdirectories of a path (read-only metadata; hidden dirs are listed so
    nothing is unreachable, but symlink cycles are not followed)."""
    home = str(Path.home())
    target = str(Path(path).resolve()) if path else home
    target_path = Path(target)
    dirs: list[dict[str, str]] = []
    if not target_path.is_dir():
        raise HTTPException(status_code=400, detail=f"not a directory: {target}")
    try:
        for entry in sorted(target_path.iterdir(), key=lambda e: e.name.lower()):
            try:
                if entry.is_dir() and not entry.is_symlink():
                    dirs.append({"name": entry.name, "path": str(entry)})
            except OSError:
                continue
    except PermissionError:
        # unreadable directory: fall through to an empty listing; the UI
        # guides the user to type the path instead
        log.warning("fs browse: permission denied listing %s", target)
    return FsBrowseOut(
        path=target,
        parent=str(target_path.parent) if target_path.parent != target_path else None,
        dirs=dirs[:500],
        home=home,
    )


@router.post("", status_code=status.HTTP_201_CREATED)
def create_project(payload: ProjectCreate, request: Request) -> dict:
    root = str(Path(payload.root).resolve())
    with session_scope(request.app.state.engine) as db:
        project = Project(
            name=payload.name,
            root=root,
            extra_paths=json.dumps([str(Path(p).resolve()) for p in payload.extra_paths]),
            verify_command=(payload.verify_command or None),
            test_command=(payload.test_command or None),
            executor=payload.executor or StoryExecution.INTERNAL,
            delivery=payload.delivery or ProjectDelivery.MERGE,
            pr_api_base=(payload.pr_api_base or None),
            pr_web_base=(payload.pr_web_base or None),
            pr_repo=(payload.pr_repo or None),
            pr_base_branch=(payload.pr_base_branch or None),
            pr_token_file=(payload.pr_token_file or None),
            ab_headless_share=(
                payload.ab_headless_share
                if payload.ab_headless_share is not None
                else 0.0
            ),
            default_gate_spec=payload.default_gate_spec or StoryGate.HUMAN,
            default_gate_code=payload.default_gate_code or StoryGate.HUMAN,
            protected_globs=payload.protected_globs or "",
            frozen_globs=payload.frozen_globs or "",
            auto_max_diff_lines=(
                payload.auto_max_diff_lines
                if payload.auto_max_diff_lines is not None
                else 400
            ),
            merge_train=bool(payload.merge_train),
            train_window_s=(
                payload.train_window_s if payload.train_window_s is not None else 300
            ),
            train_max=payload.train_max if payload.train_max is not None else 6,
            brief_token_budget=(
                payload.brief_token_budget
                if payload.brief_token_budget is not None
                else brief_module.DEFAULT_BRIEF_TOKEN_BUDGET
            ),
            brief_in_spec_prompt=bool(payload.brief_in_spec_prompt),
            code_graph_enabled=bool(payload.code_graph_enabled),
        )
        db.add(project)
        db.flush()
        if project.merge_train and not _train_ready(project):
            raise HTTPException(
                status_code=422,
                detail=_TRAIN_WIRING_ERROR,
            )
        return _project_out(db, project)


@router.patch("/{project_id}")
def update_project(project_id: str, payload: ProjectUpdate, request: Request) -> dict:
    """Partial update: omitted fields are left alone.

    brief_token_budget (>= 1) sets the brief's approximate token budget;
    brief_in_spec_prompt turns on sending ONLY the brief's Owner rules and
    Non-goals sections (plus the story's epic description) to the spec
    writer. The coder never receives the brief."""
    with session_scope(request.app.state.engine) as db:
        project = _get_project(db, project_id)
        if payload.executor is not None and payload.executor not in StoryExecution.ALL:
            raise HTTPException(
                status_code=400,
                detail=f"executor must be one of {list(StoryExecution.ALL)}",
            )
        if payload.delivery is not None and payload.delivery not in ProjectDelivery.ALL:
            raise HTTPException(
                status_code=400,
                detail=f"delivery must be one of {list(ProjectDelivery.ALL)}",
            )
        if payload.root is not None:
            project.root = str(Path(payload.root).resolve())
        if payload.name is not None:
            project.name = payload.name
        if payload.extra_paths is not None:
            project.extra_paths = json.dumps(
                [str(Path(p).resolve()) for p in payload.extra_paths]
            )
        if payload.verify_command is not None:
            project.verify_command = payload.verify_command or None
        if payload.test_command is not None:
            project.test_command = payload.test_command or None
        if payload.enabled is not None:
            project.enabled = payload.enabled
        # read-time inheritance: toggling the executor never rewrites the
        # project's stories — their NULL (inherit) rows resolve afresh on
        # every read, and explicit overrides are untouched.
        if payload.executor is not None:
            project.executor = payload.executor
        if payload.ab_headless_share is not None:
            project.ab_headless_share = min(
                1.0, max(0.0, float(payload.ab_headless_share))
            )
        if payload.delivery is not None:
            project.delivery = payload.delivery
        # ---- auto-gate check inputs ----------------------------------
        # The gate defaults accept explicit null/"" (reset to human); the
        # glob lists and the diff cap follow the pr_* pattern instead —
        # model_fields_set distinguishes "clear it" from "leave it alone".
        for gate_field in ("default_gate_spec", "default_gate_code"):
            if gate_field in payload.model_fields_set:
                value = getattr(payload, gate_field)
                setattr(project, gate_field, value or StoryGate.HUMAN)
        for glob_field in ("protected_globs", "frozen_globs"):
            if glob_field in payload.model_fields_set:
                value = getattr(payload, glob_field)
                setattr(project, glob_field, value or "")
        if "auto_max_diff_lines" in payload.model_fields_set:
            value = payload.auto_max_diff_lines
            project.auto_max_diff_lines = 400 if value is None else int(value)
        # ---- merge train ---------------------------------------------
        # train_ready is judged on the EFFECTIVE project after every field
        # in this PATCH has been applied, so the refusal catches a train
        # turned on without wiring AND wiring stripped from under a train.
        if "merge_train" in payload.model_fields_set:
            project.merge_train = bool(payload.merge_train)
        if "train_window_s" in payload.model_fields_set:
            project.train_window_s = (
                300 if payload.train_window_s is None else int(payload.train_window_s)
            )
        if "train_max" in payload.model_fields_set:
            project.train_max = 6 if payload.train_max is None else int(payload.train_max)
        if payload.brief_token_budget is not None:
            project.brief_token_budget = int(payload.brief_token_budget)
        if payload.brief_in_spec_prompt is not None:
            project.brief_in_spec_prompt = payload.brief_in_spec_prompt
        if payload.code_graph_enabled is not None:
            project.code_graph_enabled = payload.code_graph_enabled
        # The pr_* fields are nullable, and PATCH must be able to actually
        # clear them: an explicit `null` in the body unsets the column (a
        # project dropping its Gitea wiring — landing stories are then parked
        # at the human gate by the poller), while an omitted key leaves the
        # stored value alone. `value is not None` cannot tell those apart, so
        # this loop keys off model_fields_set instead. `value or None` keeps
        # parity with create_project: "" also means "unset".
        for field in ("pr_api_base", "pr_web_base", "pr_repo", "pr_base_branch", "pr_token_file"):
            if field in payload.model_fields_set:
                value = getattr(payload, field)
                setattr(project, field, value or None)
        # ---- merge train readiness ------------------------------------
        # Judged LAST, on the effective project after every field in this
        # PATCH has been applied: the refusal catches a train turned on
        # without wiring AND wiring stripped from under a running train.
        if bool(project.merge_train) and not _train_ready(project):
            raise HTTPException(status_code=422, detail=_TRAIN_WIRING_ERROR)
        project.updated_at = utcnow()
        db.add(project)
        db.flush()
        return _project_out(db, project)


@router.get("/{project_id}/train")
def train_report_endpoint(
    project_id: str,
    request: Request,
    limit: int | None = Query(default=None, ge=1),
    contexts: bool = False,
) -> list[dict]:
    """The project's merge-train batches: state, member stories, batch PR.

    Read-only board data — the train panel renders this. Batches that only
    ever formed (a story rejected mid-window) report their members too, so
    the operator sees what was dropped and why. `contexts=true` adds the
    per-context CI state to every open batch (one Gitea read each: opt-in,
    the board polls this route every few seconds)."""
    from .. import train as train_module

    with session_scope(request.app.state.engine) as db:
        _get_project(db, project_id)
        return train_module.train_report(db, project_id, limit, contexts)


class _TrainBy(BaseModel):
    by: str

    @field_validator("by")
    @classmethod
    def _by_required(cls, value: str) -> str:
        value = " ".join((value or "").split())
        if not value:
            raise ValueError("by is required")
        return value


class TrainPauseBody(_TrainBy):
    reason: str

    @field_validator("reason")
    @classmethod
    def _reason_required(cls, value: str) -> str:
        value = (value or "").strip()
        if not value:
            raise ValueError("reason is required")
        return value


class TrainResumeBody(_TrainBy):
    reason: str | None = None


class TrainBounceBody(_TrainBy):
    story_id: str
    reason: str | None = None


class TrainForceMergeBody(TrainPauseBody):
    pass


def _train_op(request: Request, project_id: str, op, *args, as_project: bool = False):
    """Run one train operator function; TrainOpError becomes its HTTP status."""
    from .. import train as train_module

    with session_scope(request.app.state.engine) as db:
        project = _get_project(db, project_id)
        try:
            result = op(db, project, *args)
        except train_module.TrainOpError as e:
            raise HTTPException(status_code=e.status, detail=e.detail) from e
        if as_project:
            db.refresh(project)
            return _project_out(db, project)
        return result


@router.post("/{project_id}/train/pause")
def pause_train(project_id: str, payload: TrainPauseBody, request: Request) -> dict:
    """Stop forming batches from departing. Joining and judging go on."""
    from .. import train as train_module

    return _train_op(
        request, project_id, train_module.pause_train,
        payload.by, payload.reason, as_project=True,
    )


@router.post("/{project_id}/train/resume")
def resume_train(project_id: str, payload: TrainResumeBody, request: Request) -> dict:
    from .. import train as train_module

    return _train_op(
        request, project_id, train_module.resume_train,
        payload.by, (payload.reason or "").strip() or None, as_project=True,
    )


@router.post("/{project_id}/train/batches/{batch_id}/rerun")
def rerun_train_batch(
    project_id: str, batch_id: str, payload: _TrainBy, request: Request
) -> dict:
    """Re-run CI on an open batch's unchanged head."""
    from .. import train as train_module

    return _train_op(
        request, project_id, train_module.rerun_batch, batch_id, payload.by
    )


@router.post("/{project_id}/train/batches/{batch_id}/bounce")
def bounce_train_member(
    project_id: str, batch_id: str, payload: TrainBounceBody, request: Request
) -> dict:
    """Send one member back to the human gate; the others re-form."""
    from .. import train as train_module

    return _train_op(
        request, project_id, train_module.bounce_member,
        batch_id, payload.story_id, payload.by,
        (payload.reason or "").strip() or None,
    )


@router.post("/{project_id}/train/batches/{batch_id}/land-now")
def land_train_batch_now(
    project_id: str, batch_id: str, payload: _TrainBy, request: Request
) -> dict:
    """Merge a green open batch PR now (409 when pending, red or touching .gitea/)."""
    from .. import train as train_module

    return _train_op(
        request, project_id, train_module.land_batch_now, batch_id, payload.by
    )


@router.post("/{project_id}/train/batches/{batch_id}/force-merge")
def force_merge_train_batch(
    project_id: str, batch_id: str, payload: TrainForceMergeBody, request: Request
) -> dict:
    """Merge a green open batch PR even when it touches .gitea/. By and reason
    are required and land on every member's note."""
    from .. import train as train_module

    return _train_op(
        request, project_id, train_module.force_merge_batch,
        batch_id, payload.by, payload.reason,
    )


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(project_id: str, request: Request) -> Response:
    with session_scope(request.app.state.engine) as db:
        project = _get_project(db, project_id)
        for story in db.exec(
            select(Story).where(Story.project_id == project_id)
        ).all():
            story.project_id = None
            db.add(story)
        for row in db.exec(
            select(ProjectBriefVersion).where(
                ProjectBriefVersion.project_id == project_id
            )
        ).all():
            db.delete(row)
        db.delete(project)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---- project brief -----------------------------------------------------------


class BriefWrite(BaseModel):
    body: str = Field(description="The full Markdown brief")
    base_version: int = Field(
        ge=0,
        description="The version this edit is based on (0 = no brief yet); "
        "a stale base is refused with 409",
    )
    author: str = Field(default="", max_length=200)


def _stale_brief(project: Project, db: Session) -> HTTPException:
    current = brief_module.brief_out(
        project, brief_module.current_brief(db, project.id)
    )
    return HTTPException(
        status_code=409,
        detail={
            "message": f"stale base_version: the current brief is v{current['version']}",
            "current": current,
        },
    )


@router.get("/{project_id}/brief")
def get_project_brief(
    project_id: str, request: Request, version: int | None = None
) -> dict:
    """The project's brief (current, or `?version=N`). An empty brief also
    carries `template`, the suggested eight-section skeleton."""
    with session_scope(request.app.state.engine) as db:
        project = _get_project(db, project_id)
        if version is None:
            row = brief_module.current_brief(db, project_id)
        else:
            row = brief_module.brief_version(db, project_id, version)
            if row is None:
                raise HTTPException(status_code=404, detail="brief version not found")
        out = brief_module.brief_out(project, row)
        if not out["body"]:
            out["template"] = brief_module.BRIEF_TEMPLATE
        return out


@router.put("/{project_id}/brief")
def put_project_brief(project_id: str, payload: BriefWrite, request: Request) -> dict:
    """Save a new brief version. Concurrency rule: `base_version` must equal
    the current version, else 409 with `detail.current` (the brief as it is
    now); re-read, merge, and retry against that version. History is
    append-only: a restore is a PUT of the old body."""
    engine = request.app.state.engine
    try:
        with session_scope(engine) as db:
            project = _get_project(db, project_id)
            current = brief_module.current_brief(db, project_id)
            current_version = current.version if current is not None else 0
            if payload.base_version != current_version:
                raise _stale_brief(project, db)
            row = ProjectBriefVersion(
                project_id=project_id,
                version=current_version + 1,
                body=payload.body,
                author=payload.author,
            )
            db.add(row)
            db.flush()
            out = brief_module.brief_out(project, row)
    except IntegrityError:
        # a concurrent writer took the same version number first
        with session_scope(engine) as db:
            raise _stale_brief(_get_project(db, project_id), db) from None
    return out


@router.get("/{project_id}/brief/versions")
def list_project_brief_versions(project_id: str, request: Request) -> list[dict]:
    """The brief's versions, newest first: metadata and tokens, no bodies."""
    with session_scope(request.app.state.engine) as db:
        _get_project(db, project_id)
        rows = db.exec(
            select(ProjectBriefVersion)
            .where(ProjectBriefVersion.project_id == project_id)
            .order_by(ProjectBriefVersion.version.desc())
        ).all()
        return [
            {
                "version": r.version,
                "author": r.author,
                "created_at": as_utc_iso(r.created_at),
                "tokens": brief_module.approx_tokens(r.body),
            }
            for r in rows
        ]


# ---- executor A/B report -----------------------------------------------------


@router.get("/{project_id}/executor-report")
def executor_report(project_id: str, request: Request) -> dict:
    """Read-only A/B comparison between the CODE executors.

    Per executor (internal pump vs headless), over the project's stories:
      runs              — CODE-stage runs (one `code_stage_ms` note each)
      wall_ms_total     — summed CODE-stage wall time
      wall_ms_avg       — wall_ms_total / runs (0.0 when no measured run)
      landed            — stories that reached `done`
      rounds_to_land_avg — CODE-stage runs per landed story (1.0 = first try)
      ai_rejections     — AI rejections (spec review, code review, or verify)
      human_rejections  — human gate rejections (spec or code gate)

    Attribution: a story's executor is its own override when set, else the
    project's executor at read time. Stories that never executed code are
    ignored, as are external ones (no CODE stage to compare).
    """
    import json as _json

    from ..models import StoryEvent, StoryStatus, effective_execution

    def _blank() -> dict:
        return {
            "executor": "",
            "runs": 0,
            "wall_ms_total": 0,
            "wall_ms_avg": 0.0,
            "landed": 0,
            "runs_of_landed": 0,
            "rounds_to_land_avg": 0.0,
            "ai_rejections": 0,
            "human_rejections": 0,
        }

    with session_scope(request.app.state.engine) as db:
        project = _get_project(db, project_id)
        stories = db.exec(
            select(Story).where(Story.project_id == project_id)
        ).all()
        project_executor = project.executor or StoryExecution.INTERNAL
        stats = {
            StoryExecution.INTERNAL: _blank(),
            StoryExecution.HEADLESS: _blank(),
        }
        for story in stories:
            executor = effective_execution(story, project_executor)
            if executor not in stats:
                continue  # external stories have no CODE stage to compare
            row = stats[executor]
            row["executor"] = executor
            runs = 0
            events = db.exec(
                select(StoryEvent)
                .where(StoryEvent.story_id == story.id)
                .order_by(StoryEvent.id)
            ).all()
            for event in events:
                try:
                    data = _json.loads(event.data or "{}")
                except ValueError:
                    continue
                if not isinstance(data, dict):
                    continue
                kind = event.kind or ""
                if kind == "note" and isinstance(data.get("code_stage_ms"), int):
                    runs += 1
                    row["runs"] += 1
                    row["wall_ms_total"] += data["code_stage_ms"]
                elif kind == "note" and data.get("code_review_reject") is True:
                    row["ai_rejections"] += 1
                elif kind == "note" and data.get("gate") == "code":
                    row["human_rejections"] += 1
                elif kind == "ai_review" and data.get("verdict") == "rejected":
                    row["ai_rejections"] += 1
                elif (
                    kind == "status"
                    and data.get("from") == StoryStatus.HUMAN_REVIEW
                    and data.get("to") == StoryStatus.QUEUED
                ):
                    # POST /reject: the only human_review -> queued move
                    row["human_rejections"] += 1
            if story.status == StoryStatus.DONE:
                row["landed"] += 1
                row["runs_of_landed"] += runs
        executors = []
        for label in (StoryExecution.INTERNAL, StoryExecution.HEADLESS):
            row = stats[label]
            row["executor"] = label
            row["wall_ms_avg"] = (
                round(row["wall_ms_total"] / row["runs"], 1) if row["runs"] else 0.0
            )
            row["rounds_to_land_avg"] = (
                round(row["runs_of_landed"] / row["landed"], 2)
                if row["landed"]
                else 0.0
            )
            row.pop("runs_of_landed", None)
            executors.append(row)
        return {
            "project_id": project_id,
            "project_name": project.name,
            "ab_headless_share": project.ab_headless_share or 0.0,
            "executors": executors,
        }


# ---- permissions ------------------------------------------------------------


permissions_router = APIRouter(prefix="/permissions", tags=["permissions"])


class DecisionBody(BaseModel):
    decision: str = Field(pattern="^(approve|deny)$")
    remember: bool = Field(
        default=True,
        description="Also add the path to the project's allowed roots (durable)",
    )


@permissions_router.get("/pending")
def pending_permissions(request: Request) -> list[dict]:
    with Session(request.app.state.engine) as db:
        rows = db.exec(
            select(PermissionRequest)
            .where(PermissionRequest.status == "pending")
            .order_by(PermissionRequest.created_at.desc())
        ).all()
        return [
            {
                "id": r.id,
                "kind": r.kind,
                "value": r.value,
                "reason": r.reason,
                "story_id": r.story_id,
                "session_id": r.session_id,
                "created_at": as_utc_iso(r.created_at),
            }
            for r in rows
        ]


@permissions_router.post("/{request_id}/decide")
def decide(request_id: str, payload: DecisionBody, request: Request) -> dict:
    engine = request.app.state.engine
    with session_scope(engine) as db:
        row = db.get(PermissionRequest, request_id)
        if row is None:
            raise HTTPException(status_code=404, detail="permission request not found")
        if row.status != "pending":
            return {"id": row.id, "status": row.status}
        row.status = "approved" if payload.decision == "approve" else "denied"
        row.decided_at = utcnow()
        db.add(row)
        value, project_id, status = row.value, row.project_id, row.status

    # durable remember: extend the project (or global allow-list)
    if payload.decision == "approve" and payload.remember:
        if project_id:
            with session_scope(engine) as db:
                project = db.get(Project, project_id)
                if project is not None:
                    extras = json.loads(project.extra_paths or "[]")
                    resolved = str(Path(value).resolve())
                    if resolved not in extras and resolved != project.root:
                        extras.append(resolved)
                    project.extra_paths = json.dumps(extras)
                    project.updated_at = utcnow()
                    db.add(project)
        else:
            settings = load_settings(engine)
            extras = [
                p for p in (settings.get("allowed_paths") or "").split(",") if p
            ]
            resolved = str(Path(value).resolve())
            if resolved not in extras:
                extras.append(resolved)
            save_settings(engine, {"allowed_paths": ",".join(extras)})

    bus.publish("permission_decided", {"id": request_id, "status": status})
    return {"id": request_id, "status": status}
