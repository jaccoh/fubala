"""Database models (SQLModel + SQLite)."""

import hashlib
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return uuid4().hex


class ChatSession(SQLModel, table=True):
    __tablename__ = "chat_sessions"

    id: str = Field(default_factory=new_id, primary_key=True, description="Hex uuid")
    title: str = Field(default="New chat")
    model: str = Field(description="Canonical model id: provider/model")
    project_id: str | None = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class ChatMessage(SQLModel, table=True):
    __tablename__ = "chat_messages"

    id: int | None = Field(default=None, primary_key=True)
    session_id: str = Field(foreign_key="chat_sessions.id", index=True)
    role: str = Field(description="user | assistant")
    content: str
    model: str | None = Field(default=None, description="Model that produced this message")
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    finish_reason: str | None = Field(
        default=None, description="null when complete; 'error' | 'stopped' otherwise"
    )
    created_at: datetime = Field(default_factory=utcnow)


class StoryStatus:
    """Story lifecycle. The runner owns in_progress -> ai_review ->
    human_review (or back to queued when the AI reviewer rejects); humans own
    queue/approve/reject. Push+PR delivery adds landing: approve pushes the
    branch and opens the PR, the poller watches it, and done only follows a
    real merge — a failed land comes back to human_review with the reason.
    External stories borrow the same statuses but the in_progress phase is
    owned by whatever outside agent claimed them (see Story.execution)."""

    BACKLOG = "backlog"
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    AI_REVIEW = "ai_review"
    HUMAN_REVIEW = "human_review"
    EXECUTING = "executing"
    # push+PR delivery: the branch is pushed and the pull request is open,
    # but the PR has not merged yet. done only after Gitea reports merged.
    LANDING = "landing"
    DONE = "done"
    FAILED = "failed"
    # operator-owned terminal state: this story's work was made irrelevant by
    # another one (a duplicate, a hand-landed fix, an abandoned direction).
    # Kept OUT of the failed lane and every pickup/retry set — it exists so
    # the failed lane holds only actionable failures.
    SUPERSEDED = "superseded"

    ALL = (
        BACKLOG, QUEUED, IN_PROGRESS, AI_REVIEW, HUMAN_REVIEW, EXECUTING,
        LANDING, DONE, FAILED, SUPERSEDED,
    )


class StoryExecution:
    """Who runs the story: the server's own pipeline, or an external agent.

    The *project* owns the default (Project.executor); a story's own
    execution value is an explicit override and NULL means "inherit the
    project's executor" (see effective_execution)."""

    INTERNAL = "internal"
    EXTERNAL = "external"
    # the CODE stage runs via the Claude Code CLI (headless) in the story's
    # worktree; everything else in the pipeline is identical. Like external,
    # accepted wherever "external" is — but unlike external it stays fully
    # owned by the server runner (the scheduler still picks it up).
    HEADLESS = "headless"
    ALL = (INTERNAL, EXTERNAL, HEADLESS)


def effective_execution(story: "Story", project_executor: str | None) -> str:
    """Resolve who runs a story: the story's explicit override wins, else the
    project's executor, else internal.

    Read-time resolution on purpose: toggling a project to external must
    immediately affect already-queued inherit stories. Legacy rows are not
    affected — the v0.7 migration stamped every pre-existing story with a
    concrete "internal", so they behave as explicit overrides and the
    project-level backfill only flips the project default, never a story.
    """
    if story.execution is not None:
        return story.execution
    return project_executor or StoryExecution.INTERNAL


def headless_assignment(story_id: str, share: float) -> bool:
    """Deterministic A/B: does this story id land in the headless bucket?

    `int(sha256(story_id), 16) % 1000 / 1000 < share` — stable per story id
    across restarts and processes (no RNG state), so the same story always
    resolves the same way no matter how often the server reboots or how many
    workers evaluate it. share <= 0 never assigns; share >= 1 always does.
    """
    if share <= 0:
        return False
    if share >= 1:
        return True
    bucket = int(hashlib.sha256(story_id.encode("utf-8")).hexdigest(), 16) % 1000
    return (bucket / 1000) < share


class StoryPriority:
    """Queue priority: urgent stories go ahead of all normal ones."""

    NORMAL = "normal"
    URGENT = "urgent"
    ALL = (NORMAL, URGENT)


class StoryGate:
    """Who may pass a gate: a human, or the machine checks only.

    human — the default: the story parks at the human gate and a person
    approves/rejects (today's behaviour).
    auto — the gate passes without a human ONLY when every machine check is
    green (verify, AI review verdict, path globs, diff size, and "no human
    has ever rejected this story"); any failed check parks the story at the
    human gate with the failed checks named in a note."""

    HUMAN = "human"
    AUTO = "auto"
    ALL = (HUMAN, AUTO)


def effective_gate(story_gate: str | None, project_default: str | None) -> str:
    """Resolve a gate value: the story's explicit value wins, else the
    project's default, else human. Create-time resolution stamps a concrete
    value on every new story, so NULL only survives on legacy rows (which
    the migration stamps to 'human' anyway)."""
    if story_gate in StoryGate.ALL:
        return story_gate
    if project_default in StoryGate.ALL:
        return project_default
    return StoryGate.HUMAN


class StoryKind:
    """What the story delivers, which decides what approval means.

    code — the default; the existing pipeline: spec → AI review → approve →
    coder agent in a worktree → merge.
    info — the spec/document IS the deliverable; approval finishes the story,
    no worktree is ever created.
    prep — the deliverable is child stories; approval finishes the story only
    once at least one child links back via depends_on."""

    CODE = "code"
    INFO = "info"
    PREP = "prep"
    ALL = (CODE, INFO, PREP)


class Story(SQLModel, table=True):
    __tablename__ = "stories"

    id: str = Field(default_factory=new_id, primary_key=True)
    title: str = Field(max_length=200)
    description: str = Field(default="")
    acceptance: str = Field(default="", description="Acceptance criteria")
    status: str = Field(default=StoryStatus.BACKLOG, index=True)
    project_id: str | None = Field(default=None, index=True)
    epic_id: str | None = Field(default=None, index=True)
    depends_on: str = Field(
        default="[]", description="JSON list of story ids this story waits on"
    )
    model: str | None = Field(
        default=None, description="Canonical model id; stamped when skill routing resolves"
    )
    skill: str | None = Field(default=None, description="Requested skill tier (low|medium|high)")
    queued_at: datetime | None = Field(
        default=None,
        description="When the story last entered queued; fairness clock for the scheduler",
    )
    priority: str = Field(
        default=StoryPriority.NORMAL,
        sa_column_kwargs={"server_default": StoryPriority.NORMAL},
        description="normal | urgent; urgent queued stories are picked up first",
    )
    created_by: str = Field(default="local", description="Client identity; tokens in phase 3")
    execution: str | None = Field(
        default=None,
        index=True,
        description="internal = server runner; external = claimed by an outside "
        "agent. NULL = inherit Project.executor (see effective_execution); "
        "claiming materializes it to 'external'.",
    )
    # Gate mode per story: 'human' (default, the operator approves) or
    # 'auto' (machine checks only — see StoryGate). Nullable like execution:
    # legacy DBs get the column via ALTER TABLE and the migration stamps
    # 'human'; new stories are stamped at create from the project default.
    gate_spec: str | None = Field(
        default=None,
        description="Spec gate mode: human (default) or auto "
        "(machine-checked; any failed check parks the story at the gate)",
    )
    gate_code: str | None = Field(
        default=None,
        description="Code gate mode: human (default) or auto "
        "(machine-checked; any failed check parks the story at the gate)",
    )
    # Set by POST /reject at EITHER gate and never cleared again: a story a
    # human rejected can never pass an auto gate later — the machine checks
    # include "no human rejection on this story".
    human_rejected: bool = Field(
        default=False,
        sa_column_kwargs={"server_default": "0"},
        description="True once a human rejected this story at any gate; "
        "permanently disables both auto gates",
    )

    kind: str = Field(
        default=StoryKind.CODE,
        description="code = full pipeline (worktree + merge); info = document "
        "deliverable, approval finishes it; prep = child stories, approval "
        "finishes it once a child links via depends_on",
    )
    claimed_by: str | None = Field(
        default=None,
        max_length=100,
        description="Self-declared identity of the external agent holding the claim",
    )
    claimed_at: datetime | None = Field(
        default=None, description="When the external agent claimed the story"
    )
    spec: str | None = Field(default=None, description="Generated implementation spec (markdown)")
    spec_version: int = Field(default=0)
    spec_rejects: int = Field(
        default=0,
        sa_column_kwargs={"server_default": "0"},
        description=(
            "Consecutive AI-review rejections of the spec since the last "
            "approval (AI or human); reaching spec_reject_limit parks the story "
            "at the human gate instead of re-queueing it"
        ),
    )
    spec_history: str | None = Field(
        default=None,
        description=(
            "JSON list of {version, verdict, reason, timestamp}: the verdict "
            "history of every generated spec version"
        ),
    )
    base_ref: str | None = Field(
        default=None, description="Worktree base commit once execution started"
    )
    code_reject_head: str | None = Field(
        default=None,
        description=(
            "Branch head recorded when a human rejected at the CODE gate; the "
            "next coder round must land at least one non-merge commit after "
            "it or the story parks at the human gate"
        ),
    )
    verify_result: str | None = Field(
        default=None,
        description="JSON: {passed, command, attempts, output} from the verify stage",
    )
    # Per-story tool-call budget for the coder run; None = inherit the app's
    # `tools_max_calls` setting. Nullable on purpose: legacy DBs get the
    # column via ALTER TABLE and read as "no override".
    max_tool_calls: int | None = Field(
        default=None,
        ge=1,
        le=160,
        description=(
            "Per-run tool-call budget overriding the app setting for this "
            "story's coder (big stories need more rounds); None = inherit "
            "the app default"
        ),
    )
    evidence: str | None = Field(
        default=None,
        description=(
            "JSON: {code_diff, verify, summary} submitted by an external "
            "executor; latest submission wins"
        ),
    )
    ai_review: str | None = Field(default=None, description="Latest AI reviewer output (markdown)")
    ai_verdict: str | None = Field(default=None, description="approved | rejected")
    approved_by: str | None = None
    error: str | None = None
    # ---- push+PR delivery (landing) ------------------------------------
    # Stamped at approve, read by the land poller on every restart. All
    # nullable: legacy rows never read as landing.
    pr_url: str | None = Field(
        default=None,
        description="HTML URL of the pull request opened for this story",
    )
    pr_index: int | None = Field(
        default=None,
        description="Gitea PR number; the poller's key for get_pull/update_branch",
    )
    pushed_sha: str | None = Field(
        default=None,
        description="Branch head pushed to the remote at approve time",
    )
    land_state: str | None = Field(
        default=None,
        description=(
            "Poller bookkeeping while landing: last observed PR state "
            "(e.g. 'merge_conflict:<n>') so a second consecutive observation "
            "can be told apart from a transient one"
        ),
    )
    # merge train: the batch this story rides in (null = not on a train).
    # Nullable so legacy rows never read as train members.
    train_batch_id: str | None = Field(
        default=None,
        description="Train batch this story was batched into (push delivery only)",
    )
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class TrainBatchState:
    """Lifecycle of a merge-train batch.

    forming — members are joining, no branch/PR yet (the window is ticking)
    open    — flushed: branch train/<id> pushed, ONE pull request open
    merged  — the batch PR merged; members were judged on main
    red     — terminal for this row: red twice, and its members were split
              into child batches (or bounced, when it held a single story)"""

    FORMING = "forming"
    OPEN = "open"
    MERGED = "merged"
    RED = "red"

    ALL = (FORMING, OPEN, MERGED, RED)


class TrainBatch(SQLModel, table=True):
    """One merge-train batch: N gate-approved stories, one branch, one PR."""

    __tablename__ = "train_batches"

    id: str = Field(default_factory=new_id, primary_key=True)
    project_id: str = Field(foreign_key="projects.id", index=True)
    state: str = Field(
        default=TrainBatchState.FORMING,
        description="forming | open | merged | red",
    )
    # member story ids as a JSON list, in gate-approval order. A snapshot on
    # purpose: the split during a bisect must not be re-ordered by reads.
    story_ids: str = Field(default="[]", description="JSON list of member story ids")
    pr_index: int | None = Field(
        default=None, description="Gitea PR number of the batch pull request"
    )
    pr_url: str | None = Field(
        default=None, description="HTML URL of the batch pull request"
    )
    head_sha: str | None = Field(
        default=None,
        description=(
            "The batch branch head CI is judged on. Refreshed from the remote "
            "before every verdict so statuses on a moved head never count."
        ),
    )
    base_sha: str | None = Field(
        default=None,
        description=(
            "origin/<base> at flush time. The done gate bounces a member whose "
            "pushed_sha is already an ancestor of it: the branch added nothing "
            "of its own, so the merged batch carries no story work for it."
        ),
    )
    ci_status: str | None = Field(
        default=None,
        description=(
            "Last combined CI status read on the batch head (pending | success "
            "| failure). 'pending' from flush/re-run until a verdict is read; "
            "the scheduler holds new work while it is pending (train_ci_hold)."
        ),
    )
    parent_batch_id: str | None = Field(
        default=None,
        description="Set on the children of a bisected (red) batch",
    )
    red_runs: int = Field(
        default=0,
        description=(
            "Confirmed reds on the stored head (a flake re-run is consumed by "
            "the first one); 2 means the batch is bisected"
        ),
    )
    not_before: datetime | None = Field(
        default=None,
        description=(
            "A replacement batch (bisect child, infra retry) does not flush "
            "before this moment; it also ignores the window and quiet checks"
        ),
    )
    infra_retries: int = Field(
        default=0,
        description="CI pod timeouts this batch lineage was retried after",
    )
    failed_tests: str | None = Field(
        default=None,
        description="JSON list of failing pytest test ids from the CI log",
    )
    created_at: datetime = Field(default_factory=utcnow)


class StoryNote(SQLModel, table=True):
    __tablename__ = "story_notes"

    id: int | None = Field(default=None, primary_key=True)
    story_id: str = Field(foreign_key="stories.id", index=True)
    body: str = Field(description="Append-only, secret-redacted work-log text")
    author: str = Field(default="local", max_length=100)
    created_at: datetime = Field(default_factory=utcnow)


class Provider(SQLModel, table=True):
    __tablename__ = "providers"

    id: str = Field(primary_key=True, description="Slug; becomes the model-id prefix")
    display_name: str
    kind: str = Field(description="litellm | openai_compatible | claude_code | echo")
    litellm_prefix: str | None = None
    base_url: str | None = None
    env_key: str | None = Field(default=None, description="Env var read as key fallback")
    api_key: str | None = Field(default=None, description="Key stored in DB (local tool tradeoff)")
    protected: bool = Field(default=False, description="Protected rows cannot be deleted")
    enabled: bool = Field(default=True)
    # IANA zone the provider states usage-limit reset times in (NULL = the
    # zone default: Asia/Shanghai for zai*, else UTC — registry.limit_reset_zone)
    limit_reset_tz: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class ProviderModel(SQLModel, table=True):
    __tablename__ = "provider_models"

    id: int | None = Field(default=None, primary_key=True)
    provider_id: str = Field(foreign_key="providers.id", index=True)
    model_id: str = Field(description="Provider-local model id; may contain slashes")
    skill: str = Field(default="medium", description="low | medium | high")
    cost: str = Field(default="medium", description="low | medium | high")
    enabled: bool = Field(default=True)


class Epic(SQLModel, table=True):
    __tablename__ = "epics"

    id: str = Field(default_factory=new_id, primary_key=True)
    title: str = Field(max_length=200)
    description: str = Field(default="")
    status: str = Field(default="open")  # open | done | archived
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class ProjectDelivery:
    """How approving code in a project lands the changes.

    merge — the story branch is merged into the project root checkout (the
    original behavior); push — the branch is pushed to the remote and an
    optional pull request is opened there."""

    MERGE = "merge"
    PUSH = "push"
    ALL = (MERGE, PUSH)


class Project(SQLModel, table=True):
    __tablename__ = "projects"

    id: str = Field(default_factory=new_id, primary_key=True)
    name: str = Field(max_length=120)
    root: str = Field(description="Absolute path to the project directory")
    extra_paths: str = Field(
        default="[]", description="JSON list of additional allowed absolute paths"
    )
    verify_command: str | None = Field(
        default=None,
        description="Verify command override for this project's stories (empty = app default)",
    )
    # The project's test command for the coder's `run_tests` tool. Unlike
    # verify_command there is NO app-wide default: unset -> the tool is not
    # exposed at all. The Python version lives INSIDE the command
    # (`uv run --python 3.15 ... pytest -q`) — one source of truth.
    test_command: str | None = Field(
        default=None,
        description="Test command the coder may run via the run_tests tool "
        "(empty = the tool is not exposed)",
    )
    enabled: bool = Field(default=True)
    # Nullable on purpose: legacy DBs get the column via ALTER TABLE (NULL
    # until the backfill below stamps a concrete value), and the v0.10
    # backfill only flips projects that already own external stories. Every
    # read path resolves NULL through `or StoryExecution.INTERNAL`.
    executor: str | None = Field(
        default=StoryExecution.INTERNAL,
        description="Who runs this project's stories by default: the internal "
        'runner ("internal") or an outside agent ("external"). Stories may '
        "override per-story via Story.execution.",
    )
    # Nullable like executor: legacy DBs get the column via ALTER TABLE (NULL
    # until the backfill stamps 'merge'); read paths resolve NULL as "merge".
    delivery: str | None = Field(
        default=ProjectDelivery.MERGE,
        description="How approval lands code: 'merge' into the root checkout, "
        "or 'push' the story branch to the remote",
    )
    # Pull-request settings — all four set => approving also opens a PR.
    # pr_token_file is a PATH (the token itself never enters the DB).
    pr_api_base: str | None = Field(
        default=None,
        description="Gitea API base URL, e.g. https://git.example.com",
    )
    pr_web_base: str | None = Field(
        default=None,
        description="Optional public Gitea URL used for stored PR links, "
        "e.g. https://git.example.com; API calls still use pr_api_base",
    )
    pr_repo: str | None = Field(
        default=None,
        description="Gitea 'owner/name' repo to open the pull request in",
    )
    pr_base_branch: str | None = Field(
        default=None,
        description="Branch the pull request targets (e.g. main)",
    )
    pr_token_file: str | None = Field(
        default=None,
        description="Path to a file holding the Gitea API token (read at use)",
    )
    # A/B split for NEW stories entering queued with no explicit execution
    # override: 0 = never (default), 1 = always headless, else the share of
    # stories routed to the headless CODE executor. Deterministic per story
    # id (see models.headless_assignment) so restarts never flip a story.
    ab_headless_share: float | None = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Share of this project's new stories A/B'd to the "
        "headless CODE executor (0..1, default 0)",
    )
    # ---- auto gates (per-project defaults + the checks' inputs) ---------
    # Nullable + backfilled to 'human' like executor above.
    default_gate_spec: str | None = Field(
        default=StoryGate.HUMAN,
        description="Gate mode new stories in this project start with "
        "(human | auto) at the SPEC gate",
    )
    default_gate_code: str | None = Field(
        default=StoryGate.HUMAN,
        description="Gate mode new stories in this project start with "
        "(human | auto) at the CODE gate",
    )
    # Comma-separated glob list: an auto code gate refuses to land a diff
    # touching any of these (empty = no protection).
    protected_globs: str = Field(
        default="", description="Comma-separated globs the auto code gate never touches"
    )
    frozen_globs: str = Field(
        default="", description="Comma-separated globs no gate may ever touch"
    )
    # Hard cap on the diff an auto code gate may land (added+removed lines
    # vs base_ref); 0 disables the cap.
    auto_max_diff_lines: int = Field(
        default=400,
        ge=0,
        description="Max added+removed diff lines an auto code gate may land (0 = no cap)",
    )
    # ---- merge train ----------------------------------------------------
    # Nullable like delivery: legacy DBs get the columns via ALTER TABLE and
    # the backfill below stamps off/300/6. Train ON requires push delivery
    # with the full pr_* wiring (enforced at project patch time).
    merge_train: bool | None = Field(
        default=False,
        description="Batch gate-approved stories into one batch PR per window",
    )
    train_window_s: int | None = Field(
        default=300,
        ge=1,
        description="Seconds a forming batch waits for more members before flushing",
    )
    train_max: int | None = Field(
        default=6,
        ge=1,
        description="Members at which a forming batch flushes immediately",
    )
    # operator pause of the train's forming flush (joining and judging go on)
    train_paused: bool | None = Field(
        default=False, description="Operator pause: forming batches do not flush"
    )
    train_paused_by: str | None = Field(default=None)
    train_pause_reason: str | None = Field(default=None)
    train_paused_at: datetime | None = Field(default=None)
    # ---- project brief ----------------------------------------------------
    # Nullable like the train columns: legacy DBs get them via ALTER TABLE and
    # read paths resolve NULL as 8000 / off.
    brief_token_budget: int | None = Field(
        default=8000,
        ge=1,
        description="Approximate token budget for the project brief",
    )
    brief_in_spec_prompt: bool | None = Field(
        default=False,
        description="Send the brief's Owner rules and Non-goals to the spec writer",
    )
    # NULL = off. On: CLI sessions get a code-review-graph MCP server for the
    # story workspace and every code review gets an impact report.
    code_graph_enabled: bool | None = Field(
        default=None,
        description="code-review-graph for this project's stories (NULL = off)",
    )

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class ProjectBriefVersion(SQLModel, table=True):
    """One saved version of a project's Markdown brief (append-only).

    The current brief is the row with the highest version; Project keeps no
    copy, so the two can never drift apart. The unique constraint is what
    refuses a concurrent writer that read the same base version."""

    __tablename__ = "project_brief_versions"
    __table_args__ = (UniqueConstraint("project_id", "version"),)

    id: int | None = Field(default=None, primary_key=True)
    project_id: str = Field(index=True)
    version: int
    body: str = Field(default="")
    author: str = Field(default="")
    created_at: datetime = Field(default_factory=utcnow)


class PermissionRequest(SQLModel, table=True):
    __tablename__ = "permission_requests"

    id: str = Field(default_factory=new_id, primary_key=True)
    kind: str = Field(default="path")
    value: str = Field(description="The path (or command) being requested")
    reason: str = Field(default="")
    status: str = Field(default="pending", index=True)  # pending|approved|denied
    story_id: str | None = Field(default=None, index=True)
    session_id: str | None = Field(default=None, index=True)
    project_id: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=utcnow)
    decided_at: datetime | None = None


class AppSetting(SQLModel, table=True):
    __tablename__ = "app_settings"

    key: str = Field(primary_key=True)
    value: str = Field(description="JSON blob of the settings it stores")


class StoryEvent(SQLModel, table=True):
    __tablename__ = "story_events"

    id: int | None = Field(default=None, primary_key=True)
    story_id: str = Field(foreign_key="stories.id", index=True)
    kind: str = Field(
        description="created | status | spec | error | note | verify | round | "
        "tool_call | tool_result"
    )
    data: str = Field(default="", description="JSON payload")
    created_at: datetime = Field(default_factory=utcnow)
