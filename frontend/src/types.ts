export interface SessionInfo {
  id: string;
  title: string;
  model: string;
  project_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface Message {
  id: number;
  session_id: string;
  role: "user" | "assistant";
  content: string;
  model: string | null;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  finish_reason: string | null;
  created_at: string;
}

export interface SessionDetail extends SessionInfo {
  messages: Message[];
}

export type Tier = "low" | "medium" | "high";

interface ModelInfo {
  id: string;
  row_id: number;
  model_id: string;
  skill: Tier;
  cost: Tier;
  enabled: boolean;
}

export interface ProviderInfo {
  id: string;
  display_name: string;
  kind: string;
  litellm_prefix: string | null;
  base_url: string | null;
  env_key: string | null;
  has_key: boolean;
  key_hint: string | null;
  protected: boolean;
  enabled: boolean;
  /** IANA zone the provider states usage-limit reset times in (null = default). */
  limit_reset_tz: string | null;
  configured: boolean;
  models: ModelInfo[];
}

/** An active provider usage-limit hold (GET /providers/limits): a headless
 * 429/usage-limit answer holds the provider until `until` (UTC ISO string). */
export interface ProviderLimit {
  provider_id: string;
  until: string;
  reason: string;
}

export interface ModelsResponse {
  default_model: string;
  providers: ProviderInfo[];
}

export interface PresetInfo {
  id: string;
  display_name: string;
  kind: string;
  base_url: string | null;
  env_key: string | null;
  models: string[];
}

export interface DoneEvent {
  message_id: number;
  session_id: string;
  model: string;
  prompt_tokens: number | null;
  completion_tokens: number | null;
}

// ---- stories ---------------------------------------------------------

export type StoryStatus =
  | "backlog"
  | "queued"
  | "in_progress"
  | "ai_review"
  | "human_review"
  | "executing"
  | "landing"
  | "done"
  | "failed"
  | "superseded";

export type StoryExecution = "internal" | "external" | "headless";

/** Who decides at a story gate: a human, or machine checks only. */
export type StoryGate = "human" | "auto";

/** How approving a code story lands the work in this project. */
export type ProjectDelivery = "merge" | "push";

export type StoryKind = "code" | "info" | "prep";

/** Where a story stands in the slot pools (in memory on the server):
 *  working = holds a model slot, waiting_tests = queued for or running its
 *  verify, waiting_slot = queued for a model slot. */
export type SlotState = "working" | "waiting_tests" | "waiting_slot";

export interface Story {
  id: string;
  title: string;
  description: string;
  acceptance: string;
  status: StoryStatus;
  project_id: string | null;
  epic_id: string | null;
  depends_on: string[];
  model: string | null;
  skill: string | null;
  created_by: string;
  /** Effective executor (explicit override, else the project's executor, else
   *  internal) — what the board renders and branches on. */
  execution: StoryExecution;
  /** Raw per-story override; null = inherit the project's executor. */
  execution_override: StoryExecution | null;
  /** Who decides at the SPEC gate (after AI review). Stamped at create from
   *  the payload, else the project's default, else "human". */
  gate_spec: StoryGate;
  /** Who decides at the CODE gate (after verify + AI code review). */
  gate_code: StoryGate;
  /** Queue priority; urgent queued stories are picked up ahead of normal ones. */
  priority: "normal" | "urgent";
  /** True once a human rejected this story at any gate; permanently disables
   *  both auto gates. Never cleared by any write path. */
  human_rejected: boolean;
  kind: StoryKind;
  /** Per-story coder tool budget; null/absent = inherit the app's
   *  tools_max_calls. Optional because SSE snapshots built from story_payload
   *  and hand-built fixtures may omit it — readers must default to null. */
  max_tool_calls?: number | null;
  claimed_by: string | null;
  claimed_at: string | null;
  spec: string | null;
  spec_version: number;
  base_ref: string | null;
  verify_result: VerifyResult | null;
  ai_review: string | null;
  ai_verdict: string | null;
  approved_by: string | null;
  error: string | null;
  // push+PR delivery: set when approval opened a pull request (null until
  // then). landing = waiting for that PR to merge.
  pr_url: string | null;
  pr_index: number | null;
  pushed_sha: string | null;
  created_at: string;
  updated_at: string;
  // Optional: present only on list/detail fetches — SSE snapshots are built
  // from story_payload and may lack them, so every reader must guard null.
  queue_position?: number | null;
  waiting_behind?: number | null;
  /** null when the story is in no slot pool (parked, queued, done, …) */
  slot_state?: SlotState | null;
  /** open coder tool calls, oldest first; list/detail fetches only */
  running_tools?: RunningTool[];
}

/** A coder tool call that has started and not yet finished. */
export interface RunningTool {
  call_id: string;
  name: string;
  args: string;
  started_at: string;
  timeout_s: number;
}

interface VerifyResult {
  /** false for info/prep stories: no verify stage ever ran, so `passed` is
   *  meaningless — check this BEFORE any !passed logic. */
  applicable?: boolean;
  passed: boolean;
  command: string;
  attempts: number;
  exit_code?: number;
  output: string;
  counts?: VerifyCounts | null;
  mismatch?: string | null;
  baseline?: { main_ran: number; flag: string } | null;
}

export interface VerifyLaneCounts {
  passed?: number;
  failed?: number;
  skipped?: number;
  errors?: number;
  [word: string]: number | undefined;
}

export interface VerifyCounts {
  lanes: VerifyLaneCounts[];
  totals: VerifyLaneCounts;
  ran: number;
}

export interface StoryEvent {
  id: number;
  kind: string;
  data: string;
  created_at: string;
}

/** Budget snapshot emitted at each round start while an agent runs. */
export interface StoryProgress {
  round: number;
  max_rounds: number;
  tool_calls: number;
  max_tool_calls: number | null;
  elapsed_s: number;
  timeout_s: number | null;
  prompt_tokens: number;
  completion_tokens: number;
  phase?: string;
}

/** One relayed headless (Claude Code CLI) stream line, live while the CODE
 * stage runs. `type` mirrors the stream-json event it came from. */
export type HeadlessStreamEvent =
  | { type: "assistant"; text: string }
  | { type: "tool_use"; tool: string; input: string }
  | { type: "tool_result"; status: "ok" | "error"; chars: number; preview?: string };

export type StoryActivityEvent =
  | { kind: "round"; data: StoryProgress }
  | { kind: "tool_call"; data: { name: string; args: string; path?: string; command?: string } }
  | { kind: "tool_result"; data: { status: "ok" | "error" | "truncated"; chars: number; error?: string } }
  | { kind: "headless_stream"; data: HeadlessStreamEvent & { phase?: string; executor?: string } };

export function parseActivityEvent(
  kind: string,
  raw: string | undefined,
): StoryActivityEvent | null {
  if (
    kind !== "round" &&
    kind !== "tool_call" &&
    kind !== "tool_result" &&
    kind !== "headless_stream"
  )
    return null;
  let data: unknown;
  try {
    data = raw ? JSON.parse(raw) : {};
  } catch {
    data = {};
  }
  return { kind, data } as StoryActivityEvent;
}

export interface StoryNote {
  id: number;
  story_id: string;
  body: string;
  author: string;
  created_at: string;
  as_feedback?: boolean;
  seen_by_coder?: boolean;
}

/** How the external executor checked its own work (outcome evidence).
 *  `applicable: false` means no verify stage ever ran (info/prep story) —
 *  check it BEFORE any !passed logic. */
export interface EvidenceVerify {
  applicable?: boolean;
  passed: boolean;
  command: string;
  output: string;
  counts?: VerifyCounts | null;
  mismatch?: string | null;
  baseline?: { main_ran: number; flag: string } | null;
}

/** Outcome evidence submitted by an external executor for a story. */
interface StoryEvidence {
  code_diff: string;
  verify: EvidenceVerify | null;
  summary: string | null;
}

export interface StoryDetail extends Story {
  events: StoryEvent[];
  notes: StoryNote[];
  code_diff: string | null;
  evidence: StoryEvidence | null;
}

export interface AppSettings {
  default_model: string | null;
  /** max LLM calls generating at once (any number of stories in flight) */
  model_slots: number;
  /** max verify runs at once */
  test_slots: number;
  /** @deprecated alias of model_slots, served for one release */
  max_parallel_stories: number;
  story_poll_interval: number;
  auto_approve_after_ai_review: boolean;
  chat_system_prompt: string | null;
  project_root: string | null;
  chat_tools_enabled: boolean;
  spec_tools_enabled: boolean;
  tools_max_calls: number;
  tools_timeout_seconds: number;
  tools_deny_globs: string;
  write_tools_enabled: boolean;
  permission_mode: "ask" | "skip" | "strict";
  allowed_paths: string;
  allowed_commands: string;
  context_files: string;
  verify_command: string;
  verify_fix_rounds: number;
  verify_timeout: number;
  /** bound on the spec -> ai_review reject loop: after this many consecutive
   * AI-review rejections the story parks at human_review (0 = never park) */
  spec_reject_limit: number;
  /** per-round completion budget for the spec writer; raise when specs fail
   *  with "spec phase produced no spec" (model burned it all thinking);
   *  null = the gateway default (FUBALA_MAX_OUTPUT_TOKENS) */
  spec_max_output_tokens: number | null;
  /** same, for the coder phase */
  coder_max_output_tokens: number | null;
  /** ceiling for cap-hit escalations inside a single run */
  agent_max_output_tokens_cap: number;
  self_update_enabled: boolean;
  spec_writer_prompt: string | null;
  ai_reviewer_prompt: string | null;
  max_external_wip_per_project: number;
  api_auth_enabled: boolean;
  /** wall-clock bound for ONE headless Claude Code child process (the CODE
   *  stage of a headless story); tools_timeout_seconds is untouched by it */
  headless_timeout_seconds: number;
  /** worker pod resources, Kubernetes quantities; applies to the next run */
  worker_cpu_request: string;
  worker_cpu_limit: string;
  worker_memory_request: string;
  worker_memory_limit: string;
  /** last 4 chars of the API token; null when none is configured */
  api_token_hint: string | null;
}

export interface Project {
  id: string;
  name: string;
  root: string;
  extra_paths: string[];
  verify_command: string | null;
  enabled: boolean;
  /** Who runs this project's stories by default: the internal runner or an
   *  outside agent. Stories without an explicit override inherit this. */
  executor: StoryExecution;
  /** How approving code lands the work: merge into the root checkout, or
   *  push the story branch to the remote (optional Gitea PR). */
  delivery: ProjectDelivery;
  /** Default gate mode this project's new stories inherit at create. */
  default_gate_spec: StoryGate;
  default_gate_code: StoryGate;
  /** Comma-separated glob lists the CODE auto gate checks the diff against:
   *  a diff touching any of these paths fails no_protected_paths /
   *  no_frozen_paths and parks the story at the human gate. */
  protected_globs: string;
  frozen_globs: string;
  /** Cap on added+removed diff lines for the CODE auto gate. */
  auto_max_diff_lines: number;
  pr_api_base: string | null;
  pr_web_base: string | null;
  pr_repo: string | null;
  pr_base_branch: string | null;
  /** Path to the file holding the Gitea API token — never the token itself. */
  pr_token_file: string | null;
  /** Merge train on: gate-approved stories are batched into one PR per window. */
  merge_train: boolean;
  /** Seconds a forming batch waits before flushing. */
  train_window_s: number;
  /** Members at which a forming batch flushes immediately. */
  train_max: number;
  /** Operator pause: forming batches do not depart (joining/judging go on). */
  train_paused?: boolean;
  train_paused_by?: string | null;
  train_pause_reason?: string | null;
  train_paused_at?: string | null;
  /** Share of new stories A/B'd to the headless CODE executor (0..1).
   *  Deterministic per story id; 0 (default) assigns none. */
  ab_headless_share: number;
  /** Approximate token budget for the project brief (default 8000). */
  brief_token_budget: number;
  /** Send only the brief's Owner rules and Non-goals (plus the story's epic
   *  description) to the spec writer. Off by default. */
  brief_in_spec_prompt: boolean;
  /** code-review-graph: an MCP server for CLI sessions plus an impact report
   *  at every code review. Off by default. */
  code_graph_enabled: boolean;
  story_count: number;
  root_exists: boolean;
  root_is_repo: boolean;
}

/** A project's versioned Markdown brief (version 0 = none saved yet). */
export interface ProjectBrief {
  project_id: string;
  version: number;
  body: string;
  author: string;
  updated_at: string | null;
  tokens: number;
  budget: number;
  over_budget: boolean;
  /** Only present when the body is empty: the suggested skeleton. */
  template?: string;
}

export interface ProjectBriefVersionInfo {
  version: number;
  author: string;
  created_at: string;
  tokens: number;
}

/** PUT result: saved, or refused because base_version was stale. */
export type BriefSaveResult =
  | { ok: true; brief: ProjectBrief }
  | { ok: false; current: ProjectBrief; message: string };

/** One merge-train batch as served by GET /projects/{id}/train. */
export interface TrainBatch {
  id: string;
  /** forming | open | merged | red */
  state: string;
  pr_url: string | null;
  pr_index: number | null;
  head_sha: string | null;
  red_runs: number;
  infra_retries?: number;
  /** Failing pytest test ids from the CI log (red batches). */
  failed_tests?: string[];
  ci_status?: string | null;
  next_attempt_at?: string | null;
  age_s?: number | null;
  /** Latest state per CI context; only with ?contexts=true on open batches. */
  ci_contexts?: Record<string, string> | null;
  parent_batch_id: string | null;
  created_at: string | null;
  members: { id: string; title: string | null; status: string | null }[];
}

export interface PermissionRequestInfo {
  id: string;
  kind: string;
  value: string;
  reason: string;
  story_id: string | null;
  session_id: string | null;
  created_at: string;
}

export interface Epic {
  id: string;
  title: string;
  description: string;
  status: string;
  story_count: number;
  done_count: number;
}
