"""API request/response schemas."""

from datetime import UTC, datetime
from typing import Annotated

from pydantic import AliasChoices, BaseModel, Field, PlainSerializer, field_validator

from .models import StoryExecution, StoryGate, StoryKind


def as_utc_iso(dt: datetime) -> str:
    """Serialize a datetime as ISO-8601 with an explicit UTC offset.

    SQLite round trips hand back naive datetimes (the stored values are UTC
    but the tzinfo is lost), so naive values are *assumed* UTC — never
    shifted. Aware values pass through untouched. This one helper backs both
    the Pydantic response models (via `UtcDatetime`) and the hand-built
    payloads (story/note SSE dicts), so REST and `/stories/events` emit the
    same shape."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


def _utc_iso(dt: datetime) -> str:
    return as_utc_iso(dt)


UtcDatetime = Annotated[datetime, PlainSerializer(_utc_iso)]
"""Datetime field type that always serializes with an explicit UTC offset."""


class SessionCreate(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    project_id: str | None = None
    model: str | None = Field(
        default=None, description="Canonical model id; falls back to the server default"
    )


class SessionUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    model: str | None = None
    project_id: str | None = None


class SessionOut(BaseModel):
    id: str
    title: str
    model: str
    project_id: str | None = None
    created_at: UtcDatetime
    updated_at: UtcDatetime


class MessageOut(BaseModel):
    id: int
    session_id: str
    role: str
    content: str
    model: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    finish_reason: str | None
    created_at: UtcDatetime


class SessionDetailOut(SessionOut):
    messages: list[MessageOut] = []


class MessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=100_000)
    model: str | None = Field(
        default=None, description="Override the session model for this one message"
    )


class ModelInfo(BaseModel):
    id: str = Field(description="Canonical model id: provider/model")
    model_id: str = Field(description="Provider-local model id; may contain slashes")
    skill: str = "medium"
    cost: str = "medium"
    enabled: bool = True


class ProviderOut(BaseModel):
    id: str
    display_name: str
    kind: str
    configured: bool
    enabled: bool = True
    protected: bool = False
    base_url: str | None = None
    models: list[ModelInfo]


class ModelsOut(BaseModel):
    default_model: str
    providers: list[ProviderOut]


class HealthOut(BaseModel):
    """Probe payload, shared by /health and /health/live. `status` is the
    answering endpoint's verdict: /health stays 'ok'/200 while the process
    serves (its stall signal is body-only, for the board banner);
    /health/live answers 'unhealthy'/503 when the last completed tick is
    older than max(600 s, 10x the poll interval) — a really dead loop, never
    a legitimately long tick."""

    status: str
    version: str
    # last COMPLETED scheduler tick (ISO UTC); None before the runner's
    # first stamp. The board banner renders it as "stalled since".
    scheduler_last_tick: str | None = None
    scheduler_stalled: bool = False


class VersionOut(BaseModel):
    version: str


# ---- stories ---------------------------------------------------------


class StoryCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=20_000)
    acceptance: str = Field(default="", max_length=20_000)
    project_id: str | None = Field(default=None, description="Workspace this story runs in")
    epic_id: str | None = Field(default=None)
    depends_on: list[str] = Field(default_factory=list, description="Story ids this waits on")
    model: str | None = Field(
        default=None, description="Canonical model id; falls back to skill routing or default"
    )
    skill: str | None = Field(
        default=None, description="low | medium | high — cost-aware model pick at queue time"
    )
    execution: str | None = Field(
        default=None,
        validation_alias=AliasChoices("execution", "execution_override"),
        description='"internal" = server runner pipeline; "external" = an '
        'outside agent claims/completes it (code lands via PR/CI). Omit/None '
        "= inherit the project's executor. `execution_override` is accepted "
        "as an alias (the field StoryOut exposes for the raw override).",
    )
    kind: str = Field(
        default=StoryKind.CODE,
        description='"code" = full pipeline (worktree + merge); "info" = document '
        "deliverable, approval finishes it; " + '"prep" = child stories',
    )
    max_tool_calls: int | None = Field(
        default=None,
        ge=1,
        le=160,
        description="Per-run tool-call budget for this story's coder; "
        "None = inherit the app's tools_max_calls setting",
    )
    gate_spec: str | None = Field(
        default=None,
        description='"human" (default) = the operator approves the spec; '
        '"auto" = machine checks only, a failed check parks the story at '
        "the gate. None = the project's default_gate_spec.",
    )
    gate_code: str | None = Field(
        default=None,
        description='"human" (default) = the operator approves the code; '
        '"auto" = machine checks only, a failed check parks the story at '
        "the gate. None = the project's default_gate_code.",
    )

    @field_validator("gate_spec", "gate_code")
    @classmethod
    def _gate_must_be_known(cls, value: str | None) -> str | None:
        if value is not None and value not in StoryGate.ALL:
            raise ValueError(f"gate must be one of {list(StoryGate.ALL)}")
        return value


class StoryUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=20_000)
    acceptance: str | None = Field(default=None, max_length=20_000)
    model: str | None = None
    epic_id: str | None = None
    depends_on: list[str] | None = None
    execution: str | None = Field(
        default=None,
        description="Switch internal/external while the story is still editable",
    )
    kind: str | None = Field(
        default=None,
        description="Switch code/info/prep while the story is still editable",
    )
    max_tool_calls: int | None = Field(
        default=None,
        ge=1,
        le=160,
        description="Per-run tool-call budget; null clears the override "
        "(inherit the app setting). Explicit key = explicit intent.",
    )
    gate_spec: str | None = Field(
        default=None,
        description="Switch the SPEC gate human/auto while the story is "
        "still editable; an explicit null clears back to the project default",
    )
    gate_code: str | None = Field(
        default=None,
        description="Switch the CODE gate human/auto while the story is "
        "still editable; an explicit null clears back to the project default",
    )

    @field_validator("gate_spec", "gate_code")
    @classmethod
    def _gate_must_be_known(cls, value: str | None) -> str | None:
        if value is not None and value not in StoryGate.ALL:
            raise ValueError(f"gate must be one of {list(StoryGate.ALL)}")
        return value


class StoryOut(BaseModel):
    id: str
    title: str
    description: str
    acceptance: str
    status: str
    project_id: str | None
    epic_id: str | None
    depends_on: list[str] = []
    model: str | None
    skill: str | None
    created_by: str
    execution: str = StoryExecution.INTERNAL
    execution_override: str | None = Field(
        default=None,
        description="Raw per-story override (null = inherit Project.executor); "
        "`execution` above is the resolved/effective value",
    )
    kind: str = StoryKind.CODE
    claimed_by: str | None = None
    claimed_at: UtcDatetime | None = None
    spec: str | None
    spec_version: int
    spec_rejects: int = Field(
        default=0,
        description="Consecutive AI-review rejections since the last approval",
    )
    spec_history: list[dict] = Field(
        default_factory=list,
        description="[{version, verdict, reason, timestamp}] per spec version",
    )
    base_ref: str | None
    branch: str | None = Field(
        default=None,
        description=(
            "fubala/<id> branch carrying the story's executed work — set for "
            "internal stories with a pinned base_ref; survives cancellation "
            "(a wip commit preserves the worktree's changes)"
        ),
    )
    max_tool_calls: int | None = Field(
        default=None,
        description="Per-story coder tool budget; null = the app default applies",
    )
    gate_spec: str = Field(
        default=StoryGate.HUMAN,
        description="SPEC gate mode: human (default) or auto (machine checks)",
    )
    gate_code: str = Field(
        default=StoryGate.HUMAN,
        description="CODE gate mode: human (default) or auto (machine checks)",
    )
    human_rejected: bool = Field(
        default=False,
        description="True once a human rejected this story at any gate; "
        "permanently disables both auto gates",
    )
    verify_result: dict | None = Field(
        default=None, description="Verify stage outcome (passed/command/attempts/output)"
    )
    ai_review: str | None
    ai_verdict: str | None
    approved_by: str | None
    error: str | None
    pr_url: str | None = Field(
        default=None,
        description="Pull request URL (push+PR delivery; set once a PR is open)",
    )
    pr_index: int | None = Field(
        default=None,
        description="Gitea PR number; key for land polling and retry-land",
    )
    pushed_sha: str | None = Field(
        default=None,
        description="Branch head pushed to the remote at approve time",
    )
    created_at: UtcDatetime
    updated_at: UtcDatetime
    queue_position: int | None = Field(
        default=None,
        description=(
            "1-based pickup position among queued internal stories with "
            "dependencies met; None for deps-blocked/external queued stories "
            "and for SSE snapshots published before a list fetch"
        ),
    )
    running_tools: list[dict] = Field(
        default_factory=list,
        description=(
            "Open coder tool calls of an executing story, oldest first: "
            "{call_id, name, args, started_at, timeout_s}; empty otherwise "
            "and in SSE snapshots"
        ),
    )
    waiting_behind: int | None = Field(
        default=None,
        description=(
            "How many internal slots are busy (in_progress/executing/ai_review)"
            " — the N in 'waiting for a slot (N busy)'"
        ),
    )


class StoryEventOut(BaseModel):
    id: int
    # kinds include run-transparency activity events (round | tool_call |
    # tool_result); `data` stays a JSON string so each kind carries its own
    # shape — clients parse per kind (see frontend StoryActivityEvent)
    kind: str = Field(
        description="created | status | spec | error | note | verify | round | "
        "tool_call | tool_result"
    )
    data: str = Field(description="JSON payload; shape varies by kind")
    created_at: UtcDatetime


class BlockingStory(BaseModel):
    id: str
    title: str
    status: str


class StoryNoteCreate(BaseModel):
    body: str = Field(min_length=1)
    author: str | None = Field(default=None, max_length=100)
    as_feedback: bool = Field(
        default=False,
        description=(
            "Mark this note as operator feedback: a reason-shaped note event is "
            "recorded too, so the next spec/coder run sees it without a reject"
        ),
    )


class StoryNoteOut(BaseModel):
    id: int
    story_id: str
    body: str
    author: str
    created_at: UtcDatetime
    as_feedback: bool = False
    seen_by_coder: bool = False


class EvidenceVerifyIn(BaseModel):
    """How the external executor checked its own work."""

    passed: bool
    command: str = Field(default="", max_length=2_000)
    output: str = Field(default="", max_length=200_000)


class EvidenceIn(BaseModel):
    """Outcome evidence from an external executor: what changed and whether
    it verified. Overwrites whatever was stored before (latest wins)."""

    code_diff: str = Field(default="", max_length=2_000_000)
    verify: EvidenceVerifyIn | None = None
    summary: str | None = Field(default=None, max_length=20_000)


class EvidenceOut(BaseModel):
    """Mirrors the stored JSON shape on Story.evidence."""

    code_diff: str = ""
    verify: EvidenceVerifyIn | None = None
    summary: str | None = None


class StoryDetailOut(StoryOut):
    events: list[StoryEventOut] = []
    notes: list[StoryNoteOut] = []
    code_diff: str | None = Field(default=None, description="Worktree diff once executed")
    evidence: EvidenceOut | None = Field(
        default=None,
        description=(
            "Externally submitted outcome evidence; code_diff falls back to it "
            "when the story has no internal worktree"
        ),
    )
    blocking: list[BlockingStory] = Field(
        default_factory=list, description="Unfinished dependencies"
    )


class ApproveBody(BaseModel):
    by: str = Field(default="local", max_length=100)
    model: str | None = Field(
        default=None,
        description=(
            "Coder model switch at the SPEC gate only: allowed while the "
            "story is in human review with no executed work (no base_ref) "
            "and no external execution; 409 at the code gate. Validated "
            "against the model registry (400 on an unknown model)."
        ),
    )
    override_verify: bool = Field(
        default=False,
        description=(
            "Operator override of a failed verify stage: requires a "
            "justification, and is recorded in the story's events"
        ),
    )
    justification: str = Field(
        default="",
        max_length=4_000,
        description="Why the failed verify is acceptable (required with override_verify)",
    )
    max_tool_calls: int | None = Field(
        default=None,
        ge=1,
        le=160,
        description=(
            "Per-story tool budget bump at the SPEC gate only: allowed while "
            "the story is in human review with no executed work (no base_ref) "
            "and no external execution; 409 at the code gate. Omitted/None "
            "leaves the stored value untouched."
        ),
    )


class RejectBody(BaseModel):
    by: str = Field(default="local", max_length=100)
    reason: str = Field(min_length=1, max_length=2_000)


class CancelBody(BaseModel):
    by: str = Field(default="local", max_length=100)


class RetryBody(BaseModel):
    """Operator retry: pick the pipeline stage a failed/backlog/queued story
    resumes at. `stage` is validated by the endpoint (it maps onto StoryStatus
    values, but only the retryable subset is accepted)."""

    stage: str = Field(
        description="ai_review | human_review | executing | queued | landing",
    )
    by: str = Field(default="local", max_length=100)


class ReSpecBody(BaseModel):
    """Operator re-spec: wipe the generated spec and worktree pin, back to
    queued so the pipeline regenerates the spec from scratch."""

    by: str = Field(default="local", max_length=100)


class RetryLandBody(BaseModel):
    """Operator retry of a push+PR landing: Gitea updates the PR branch from
    its base and the story goes back to `landing` under the poller."""

    by: str = Field(default="local", max_length=100)


class SupersedeBody(BaseModel):
    """Operator close of a failed or human-review story as superseded: its
    work was made
    irrelevant by another story (a duplicate, a hand-landed fix, an abandoned
    direction). `by` names the superseding story id OR a free-text note —
    whichever the operator has; both land in the event trail."""

    by: str = Field(
        default="local",
        max_length=100,
        description="Operator identity closing the story",
    )
    superseded_by: str = Field(
        default="",
        max_length=200,
        description="Story id or note naming what made this work irrelevant",
    )


class ReleaseBody(BaseModel):
    """Who is releasing an external claim — self-reported, no auth yet."""

    by: str = Field(default="local", max_length=100)


class ClaimBody(BaseModel):
    claimed_by: str = Field(min_length=1, max_length=100)


class CompleteBody(BaseModel):
    note: str = Field(default="", max_length=4_000)


class PingOut(BaseModel):
    ping: str
