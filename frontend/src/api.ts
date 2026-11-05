import type {
  AppSettings,
  BriefSaveResult,
  DoneEvent,
  Epic,
  PermissionRequestInfo,
  Project,
  ProjectBrief,
  ProjectBriefVersionInfo,
  ProjectDelivery,
  ModelsResponse,
  PresetInfo,
  ProviderInfo,
  ProviderLimit,
  SessionDetail,
  SessionInfo,
  Story,
  StoryDetail,
  StoryExecution,
  StoryGate,
  StoryNote,
  Tier,
  TrainBatch,
} from "@/types";

const BASE = "/api/v1";

/** /api/v1/health payload. The endpoint answers 200 whenever the process
 * serves; `scheduler_stalled` true means the story scheduler's heartbeat
 * went stale (server-side bound: ~2 min): HTTP answers, but nothing is
 * moving stories. `scheduler_last_tick` is the last COMPLETED tick (ISO
 * UTC), i.e. "stalled since". */
export interface HealthStatus {
  status: string;
  version?: string;
  scheduler_last_tick?: string | null;
  scheduler_stalled: boolean;
}

/** Raw download URL for the uncapped story diff (attachment disposition). */
export const diffDownloadUrl = (id: string) =>
  `${BASE}/stories/${id}/diff?download=1`;


/** The caller is not authenticated (401): no valid session cookie, and the
 * silent loopback mint did not help. The sign-in gate takes over. */
export class UnauthorizedError extends Error {
  constructor(message = "authentication required") {
    super(message);
    this.name = "UnauthorizedError";
  }
}

/** POST the API token once to mint the `fubala_session` cookie.
 * Resolves on 200; throws UnauthorizedError on 401 (wrong/missing token).
 * The token never leaves this call: no web storage, no React state beyond
 * the sign-in form's own uncontrolled value. */
export async function signInWithToken(token: string): Promise<void> {
  const res = await fetch(`${BASE}/auth/session`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}` },
  });
  if (res.status === 200) return;
  if (res.status === 401) throw new UnauthorizedError();
  throw new Error(`${res.status} ${res.statusText}`);
}

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const body = await res.json().catch(() => ({}) as { detail?: string });
    if (res.status === 401) throw new UnauthorizedError(body.detail ?? `${res.status} ${res.statusText}`);
    throw new Error(body.detail ?? `${res.status} ${res.statusText}`);
  }
  return res.json() as Promise<T>;
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  let res = await fetch(`${BASE}${path}`, init);
  if (res.status === 401) {
    // One silent attempt to mint the session cookie (loopback needs no
    // credential), then one retry. When that still 401s the caller sees an
    // UnauthorizedError so the sign-in gate can take over.
    try {
      await fetch(`${BASE}/auth/session`, { method: "POST" });
    } catch {
      /* offline / network gone: fall through to the error below */
    }
    res = await fetch(`${BASE}${path}`, init);
  }
  return json<T>(res);
}

export const api = {
  /** The serving probe. It answers 200 with the HealthStatus body whenever
   * the process serves (the stall signal is body-only), so a plain fetch is
   * enough — but it bypasses req() all the same: /health is auth-exempt, so
   * the 401 session-mint retry there never applies. */
  health: async (): Promise<HealthStatus> => {
    const res = await fetch(`${BASE}/health`);
    return (await res.json()) as HealthStatus;
  },
  models: () => req<ModelsResponse>("/models"),
  listSessions: () => req<SessionInfo[]>("/chat/sessions"),
  createSession: (body: { title?: string; model?: string; project_id?: string }) =>
    req<SessionInfo>("/chat/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  getSession: (id: string) => req<SessionDetail>(`/chat/sessions/${id}`),
  patchSession: (id: string, body: { title?: string; model?: string; project_id?: string | null }) =>
    req<SessionInfo>(`/chat/sessions/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  deleteSession: async (id: string) => {
    const res = await fetch(`${BASE}/chat/sessions/${id}`, { method: "DELETE" });
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  },

  // ---- stories -------------------------------------------------------

  listStories: () => req<Story[]>("/stories"),
  createStory: (body: {
    title: string;
    description?: string;
    acceptance?: string;
    model?: string;
    skill?: string;
    project_id?: string;
    epic_id?: string;
    depends_on?: string[];
    /** Explicit per-story override; omit the key to inherit Project.executor. */
    execution?: "internal" | "external";
    kind?: string;
    gate_spec?: StoryGate;
    gate_code?: StoryGate;
  }) =>
    req<Story>("/stories", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  addStoryNote: (id: string, body: string) =>
    req<StoryNote>(`/stories/${id}/notes`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ body, author: "human" }),
    }),
  getStory: (id: string) => req<StoryDetail>(`/stories/${id}`),
  /** Uncapped live worktree diff (plain text). Throws Error with the server's
   * `detail` on 404 (no worktree / no base_ref / not yet executing). */
  getStoryDiff: async (id: string, download = false): Promise<string> => {
    const res = await fetch(
      `${BASE}/stories/${id}/diff${download ? "?download=1" : ""}`,
    );
    if (!res.ok) {
      const body = (await res.json().catch(() => ({}))) as { detail?: string };
      throw new Error(body.detail ?? `${res.status} ${res.statusText}`);
    }
    return res.text();
  },
  /** Raw download URL for the uncapped diff (Content-Disposition: attachment). */
  diffDownloadUrl,
  patchStory: (id: string, body: {
    title?: string;
    description?: string;
    acceptance?: string;
    epic_id?: string | null;
    depends_on?: string[];
    /** Explicit per-story executor; null clears the override (inherit project). */
    execution?: StoryExecution | null;
    kind?: string;
    /** Gate mode switches; only editable while the story is editable. */
    gate_spec?: StoryGate;
    gate_code?: StoryGate;
  }) =>
    req<Story>(`/stories/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  deleteStory: async (id: string) => {
    const res = await fetch(`${BASE}/stories/${id}`, { method: "DELETE" });
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  },
  storyAction: (
    id: string,
    action: "queue" | "unqueue" | "approve" | "reject" | "discard" | "claim" | "complete" | "release",
    /** JSON body; `max_tool_calls` (number) is accepted on the spec-gate approve. */
    body?: Record<string, string | number>,
  ) =>
    req<Story>(`/stories/${id}/${action}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body ?? {}),
    }),
  /** Close a FAILED or HUMAN_REVIEW story as superseded (work made irrelevant by another).
   * Gate stories get their worktree discarded and an open PR commented + closed, best-effort.
   * Throws Error with the server's `detail` on 409 (still running, done, backlog or queued). */
  supersedeStory: (id: string, by: string, supersededBy: string) =>
    req<Story>(`/stories/${id}/supersede`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ by, superseded_by: supersededBy }),
    }),
  /** Re-arm the landing poller for a failed push+PR story (POST /retry with
   * stage "landing"). 409 when the story has no pull request. */
  retryLanding: (id: string, by: string) =>
    req<Story>(`/stories/${id}/retry`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ stage: "landing", by }),
    }),

  // ---- projects & permissions -------------------------------------------

  browseFolders: (path?: string) =>
    req<{ path: string; parent: string | null; dirs: { name: string; path: string }[]; home: string }>(
      `/projects/fs/browse${path ? `?path=${encodeURIComponent(path)}` : ""}`,
    ),
  listProjects: () => req<Project[]>("/projects"),
  createProject: (body: {
    name: string;
    root: string;
    extra_paths?: string[];
    verify_command?: string;
    executor?: StoryExecution;
    delivery?: ProjectDelivery;
    default_gate_spec?: StoryGate;
    default_gate_code?: StoryGate;
    protected_globs?: string;
    frozen_globs?: string;
    auto_max_diff_lines?: number;
    merge_train?: boolean;
    train_window_s?: number;
    train_max?: number;
    pr_api_base?: string;
    pr_web_base?: string;
    pr_repo?: string;
    pr_base_branch?: string;
    pr_token_file?: string;
  }) =>
    req<Project>("/projects", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  patchProject: (
    id: string,
    body: {
      name?: string;
      root?: string;
      extra_paths?: string[];
      verify_command?: string;
      enabled?: boolean;
      executor?: StoryExecution;
      delivery?: ProjectDelivery;
      default_gate_spec?: StoryGate;
      default_gate_code?: StoryGate;
      protected_globs?: string;
      frozen_globs?: string;
      auto_max_diff_lines?: number;
      ab_headless_share?: number;
      merge_train?: boolean;
      train_window_s?: number;
      train_max?: number;
      brief_token_budget?: number;
      brief_in_spec_prompt?: boolean;
      pr_api_base?: string;
    pr_web_base?: string;
      pr_repo?: string;
      pr_base_branch?: string;
      pr_token_file?: string;
    },
  ) =>
    req<Project>(`/projects/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  deleteProject: async (id: string) => {
    const res = await fetch(`${BASE}/projects/${id}`, { method: "DELETE" });
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  },
  /** The project's brief: current, or an old `version`. */
  getProjectBrief: (id: string, version?: number) =>
    req<ProjectBrief>(
      `/projects/${id}/brief${version === undefined ? "" : `?version=${version}`}`,
    ),
  /** Save a new brief version against `baseVersion`; a 409 (someone saved
   *  in between) comes back as `ok: false` with the server's current brief. */
  putProjectBrief: async (
    id: string,
    body: { body: string; base_version: number; author: string },
  ): Promise<BriefSaveResult> => {
    const init: RequestInit = {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    };
    let res = await fetch(`${BASE}/projects/${id}/brief`, init);
    if (res.status === 401) {
      await fetch(`${BASE}/auth/session`, { method: "POST" }).catch(() => undefined);
      res = await fetch(`${BASE}/projects/${id}/brief`, init);
    }
    if (res.status === 409) {
      const detail = (await res.json()).detail as { message: string; current: ProjectBrief };
      return { ok: false, current: detail.current, message: detail.message };
    }
    return { ok: true, brief: await json<ProjectBrief>(res) };
  },
  listProjectBriefVersions: (id: string) =>
    req<ProjectBriefVersionInfo[]>(`/projects/${id}/brief/versions`),
  /** The project's merge-train batches (board train panel). */
  projectTrain: (id: string) => req<TrainBatch[]>(`/projects/${id}/train`),
  pendingPermissions: () => req<PermissionRequestInfo[]>("/permissions/pending"),
  decidePermission: (id: string, decision: "approve" | "deny", remember = true) =>
    req<{ id: string; status: string }>(`/permissions/${id}/decide`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decision, remember }),
    }),

  // ---- epics -------------------------------------------------------------

  listEpics: () => req<Epic[]>("/epics"),
  createEpic: (body: { title: string; description?: string }) =>
    req<Epic>("/epics", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  patchEpic: (id: string, body: { title?: string; description?: string; status?: string }) =>
    req<Epic>(`/epics/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  deleteEpic: async (id: string) => {
    const res = await fetch(`${BASE}/epics/${id}`, { method: "DELETE" });
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  },

  // ---- providers -------------------------------------------------------

  listProviders: () => req<ProviderInfo[]>("/providers"),
  listPresets: () => req<PresetInfo[]>("/providers/presets"),
  createProvider: (body: {
    preset: string;
    id?: string;
    display_name?: string;
    base_url?: string;
    env_key?: string;
    api_key?: string;
  }) =>
    req<ProviderInfo>("/providers", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  patchProvider: (
    id: string,
    body: {
      display_name?: string;
      base_url?: string;
      env_key?: string;
      litellm_prefix?: string;
      enabled?: boolean;
    },
  ) =>
    req<ProviderInfo>(`/providers/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  deleteProvider: async (id: string) => {
    const res = await fetch(`${BASE}/providers/${id}`, { method: "DELETE" });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}) as { detail?: string });
      throw new Error(body.detail ?? `${res.status} ${res.statusText}`);
    }
  },
  setProviderKey: (id: string, key: string) =>
    req<ProviderInfo>(`/providers/${id}/key`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key }),
    }),
  addProviderModel: (id: string, body: { model_id: string; skill?: Tier; cost?: Tier }) =>
    req<ProviderInfo>(`/providers/${id}/models`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  updateProviderModel: (
    id: string,
    rowId: number,
    body: { skill?: Tier; cost?: Tier; enabled?: boolean },
  ) =>
    req<ProviderInfo>(`/providers/${id}/models/${rowId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  getSettings: () => req<AppSettings>("/settings"),
  patchSettings: (body: Partial<AppSettings>) =>
    req<AppSettings>("/settings", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  getPromptDefaults: () =>
    req<{ spec_writer_prompt: string; ai_reviewer_prompt: string }>("/settings/prompt_defaults"),

  // ---- auth -------------------------------------------------------------

  /** Mint/rotate the API token. The full token appears exactly once, here. */
  rotateApiToken: () =>
    req<{ token: string; hint: string | null; rotated: boolean }>("/auth/token", {
      method: "POST",
    }),
  deleteApiToken: () =>
    req<{ hint: string | null }>("/auth/token", { method: "DELETE" }),
  /** Ask the server to set the HttpOnly session cookie (loopback only). */
  mintSession: () => req<{ ok: boolean; cookie: string }>("/auth/session", { method: "POST" }),
  listRemoteModels: (id: string) =>
    req<{ model_id: string; skill: "low" | "medium" | "high"; cost: "low" | "medium" | "high" }[]>(
      `/providers/${id}/remote_models`,
    ),
  refreshProviderModels: (id: string, removeMissing = false) =>
    req<ProviderInfo & { sync?: { added: number; removed: number; seen: number } }>(
      `/providers/${id}/refresh_models`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ remove_missing: removeMissing }),
      },
    ),
  deleteProviderModel: async (id: string, rowId: number) => {
    const res = await fetch(`${BASE}/providers/${id}/models/${rowId}`, { method: "DELETE" });
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  },
  /** Active provider usage-limit holds (headless 429/usage-limit answers). */
  fetchProviderLimits: () => req<ProviderLimit[]>("/providers/limits"),
  /** Forget a provider's usage-limit hold so pickup resumes. 404 when none. */
  clearProviderLimit: async (id: string) => {
    const res = await fetch(`${BASE}/providers/${id}/limits/clear`, { method: "POST" });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}) as { detail?: string });
      throw new Error(body.detail ?? `${res.status} ${res.statusText}`);
    }
  },
};

export interface StreamHandlers {
  onDelta: (text: string) => void;
  onTool?: (event: { name: string; arguments: string; preview: string }) => void;
  onDone: (event: DoneEvent) => void;
  onError: (message: string) => void;
}

/** POST a chat message and consume the SSE reply stream. */
export async function streamMessage(
  sessionId: string,
  content: string,
  model: string | null | undefined,
  handlers: StreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(`${BASE}/chat/sessions/${sessionId}/messages`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content, ...(model ? { model } : {}) }),
    signal,
  });
  if (!res.ok || !res.body) {
    const body = await res.json().catch(() => ({}) as { detail?: string });
    throw new Error(body.detail ?? `${res.status} ${res.statusText}`);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  const dispatch = (block: string) => {
    let event = "";
    let data = "";
    for (const line of block.split("\n")) {
      if (line.startsWith("event: ")) event = line.slice(7);
      else if (line.startsWith("data: ")) data += line.slice(6);
    }
    if (!event) return;
    const payload = data ? JSON.parse(data) : {};
    if (event === "delta") handlers.onDelta(payload.text as string);
    else if (event === "tool") handlers.onTool?.(payload);
    else if (event === "done") handlers.onDone(payload as DoneEvent);
    else if (event === "error") handlers.onError(payload.message as string);
  };

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let idx: number;
    while ((idx = buffer.indexOf("\n\n")) !== -1) {
      dispatch(buffer.slice(0, idx));
      buffer = buffer.slice(idx + 2);
    }
  }
  if (buffer.trim()) dispatch(buffer);
}
