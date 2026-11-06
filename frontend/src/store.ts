import { create } from "zustand";

import { api, streamMessage, UnauthorizedError } from "@/api";
import { playReviewDing } from "@/utils/softDing";
import { parseActivityEvent } from "@/types";
import { parseLocation } from "@/url";
import type {
  AppSettings,
  Epic,
  Message,
  ModelsResponse,
  PermissionRequestInfo,
  Project,
  ProjectDelivery,
  ProviderInfo,
  ProviderLimit,
  SessionInfo,
  Story,
  StoryActivityEvent,
  StoryDetail,
  StoryExecution,
  StoryGate,
  RunningTool,
  StoryProgress,
  Tier,
} from "@/types";

let controller: AbortController | null = null;
let boardEvents: EventSource | null = null;
let healthTimer: ReturnType<typeof setInterval> | null = null;

const MAX_LOG = 16_000;
const MAX_ACTIVITY = 500;

/** How often the board polls /api/v1/health for the scheduler heartbeat.
 * Slow on purpose: this is a liveness signal, not live data — the server
 * flags a stall after ~2 min (3x the poll interval, whichever is longer). */
export const HEALTH_POLL_MS = 15_000;

/** One health poll: stamp or clear `schedulerStalledSince`. Everything is
 * inside the promise chain (a missing `api.health` in tests or a dropped
 * network just rejects into the catch), so a failing poll can never throw
 * into the interval. */
function pollSchedulerHealth(): void {
  Promise.resolve()
    .then(() => api.health())
    .then((h) => {
      useStore.setState({
        schedulerStalledSince: h.scheduler_stalled
          ? h.scheduler_last_tick ?? null
          : null,
      });
    })
    .catch(() => {
      /* unreachable server: keep the last known banner state */
    });
}

/** Debounce window for story-detail refetches triggered by board SSE events. */
export const DETAIL_REFRESH_MS = 300;

/** Single pending detail refresh (one shared board EventSource → one timer). */
let detailRefreshTimer: ReturnType<typeof setTimeout> | null = null;
let detailRefreshId: string | null = null;

/** Cancel any pending detail refresh (used when the panel closes). */
function clearDetailRefresh(): void {
  if (detailRefreshTimer !== null) clearTimeout(detailRefreshTimer);
  detailRefreshTimer = null;
  detailRefreshId = null;
}

/**
 * Debounced refresh of the open story detail.
 *
 * SSE can burst (story + story_note + future run-activity kinds), so every
 * event that can change detail calls this instead of hitting `GET /stories/:id`
 * directly; a burst collapses into a single request. The guard is re-checked at
 * fire time so a stale timer never writes a detail for a closed/other story.
 * Best-effort: errors are swallowed and the panel keeps its previous state.
 */
function scheduleDetailRefresh(id: string): void {
  if (useStore.getState().openStoryId !== id) return;
  if (detailRefreshTimer !== null) clearTimeout(detailRefreshTimer);
  detailRefreshId = id;
  detailRefreshTimer = setTimeout(() => {
    detailRefreshTimer = null;
    const target = detailRefreshId;
    detailRefreshId = null;
    if (target === null || useStore.getState().openStoryId !== target) return;
    api
      .getStory(target)
      .then((detail) => {
        if (useStore.getState().openStoryId !== target) return;
        useStore.setState((st) => ({
          storyDetail: detail,
          // replay the run from persisted events (dedupes live SSE additions)
          storyActivity: {
            ...st.storyActivity,
            [target]: activityFromEvents(detail.events),
          },
          stories: st.stories.some((s) => s.id === detail.id)
            ? st.stories.map((s) => (s.id === detail.id ? { ...s, ...detail } : s))
            : st.stories,
        }));
      })
      .catch(() => {
        /* detail refresh is best-effort */
      });
  }, DETAIL_REFRESH_MS);
}

interface FubalaState {
  view: "chat" | "board" | "settings" | "projects";
  /**
   * True when init's first API call came back 401: no session cookie and not
   * loopback. App.tsx renders the sign-in gate instead of the shell (which
   * also keeps its SSE streams unmounted while signed out).
   */
  authRequired: boolean;
  /**
   * True once the URL has been read into state (init → applyLocation).
   * Guards the history writer in App.tsx so init defaults never clobber
   * deep-link params before the restore has happened.
   */
  urlRestored: boolean;
  sessions: SessionInfo[];
  currentId: string | null;
  messages: Message[];
  streaming: boolean;
  streamText: string;
  streamTools: { name: string; arguments: string }[];
  streamError: string | null;
  models: ModelsResponse | null;
  /** model for the next message; kept in sync with the open session's model */
  selectedModel: string | null;

  stories: Story[];
  openStoryId: string | null;
  storyDetail: StoryDetail | null;
  /** live runner output per story id */
  storyLogs: Record<string, string>;
  /** latest budget snapshot per story id (from story_progress round events) */
  storyProgress: Record<string, StoryProgress>;
  /** open coder tool calls per story id, oldest first */
  storyRunningTools: Record<string, RunningTool[]>;
  /** live activity events per story id, appended from story_progress SSE */
  storyActivity: Record<string, StoryActivityEvent[]>;

  providers: ProviderInfo[];
  /** active provider usage-limit holds (headless 429s); refreshed on init,
   * on every board story event and by the board's badge poll */
  providerLimits: ProviderLimit[];
  appSettings: AppSettings | null;
  projects: Project[];
  epics: Epic[];
  epicFilter: string | null;
  projectFilter: string | null;
  setProjectFilter: (id: string | null) => void;
  pendingPermissions: PermissionRequestInfo[];

  /** ID of the story whose toast is currently showing (null = no toast) */
  pendingToastId: string | null;

  /** When the scheduler heartbeat last went stale (ISO UTC from
   * /api/v1/health), or null while it is healthy. The board renders a stall
   * banner from this; polled every HEALTH_POLL_MS. */
  schedulerStalledSince: string | null;

  init: () => Promise<void>;
  /**
   * Read a location into state: sets `view` and filters, and opens the story
   * named by `?story=` — but only if it exists in the already-loaded `stories`
   * list (stale/unknown ids are ignored so the panel never opens empty).
   * A location without a resolvable `?story=` closes any open panel, so state
   * always converges to what the URL describes. Injectable loc = testable
   * without jsdom.
   */
  applyLocation: (loc: { pathname: string; search: string }) => Promise<void>;
  setView: (view: "chat" | "board" | "settings" | "projects") => void;
  newSession: (projectId?: string) => Promise<void>;
  openSession: (id: string) => Promise<void>;
  deleteSession: (id: string) => Promise<void>;
  setSelectedModel: (model: string) => Promise<void>;
  send: (content: string) => Promise<void>;
  stop: () => void;

  refreshStories: () => Promise<void>;
  openStory: (id: string) => Promise<void>;
  closeStory: () => void;
  createStory: (form: {
    title: string;
    description?: string;
    acceptance?: string;
    model?: string;
    skill?: string;
    project_id?: string;
    execution?: "internal" | "external";
    gate_spec?: StoryGate;
    gate_code?: StoryGate;
    priority?: "normal" | "urgent";
  }) => Promise<void>;
  deleteStory: (id: string) => Promise<void>;
  storyAction: (
    id: string,
    action: "queue" | "unqueue" | "approve" | "reject" | "discard" | "claim" | "complete" | "release",
    /** JSON body; `max_tool_calls` (number) is accepted on the spec-gate approve. */
    body?: Record<string, string | number>,
  ) => Promise<void>;
  /** Close a FAILED or HUMAN_REVIEW story as superseded (dedicated endpoint, 409 otherwise). */
  supersedeStory: (id: string, by: string, supersededBy: string) => Promise<void>;
  /** Re-arm the landing poller for a failed push+PR story (retry to `landing`). */
  retryLanding: (id: string, by: string) => Promise<void>;
  connectBoard: () => void;
  /** Dismiss the current toast (if any) */
  dismissToast: () => void;

  refreshProviders: () => Promise<void>;
  refreshProviderLimits: () => Promise<void>;
  clearProviderLimit: (id: string) => Promise<void>;
  createProvider: (form: {
    preset: string;
    id?: string;
    display_name?: string;
    base_url?: string;
    env_key?: string;
    api_key?: string;
  }) => Promise<void>;
  patchProvider: (
    id: string,
    body: {
      display_name?: string;
      base_url?: string;
      env_key?: string;
      enabled?: boolean;
    },
  ) => Promise<void>;
  deleteProvider: (id: string) => Promise<void>;
  setProviderKey: (id: string, key: string) => Promise<void>;
  addProviderModel: (id: string, model_id: string, skill: Tier, cost: Tier) => Promise<void>;
  updateProviderModel: (
    id: string,
    rowId: number,
    body: { skill?: Tier; cost?: Tier; enabled?: boolean },
  ) => Promise<void>;
  deleteProviderModel: (id: string, rowId: number) => Promise<void>;
  refreshProviderModels: (id: string, removeMissing?: boolean) => Promise<void>;
  patchAppSettings: (body: Partial<AppSettings>) => Promise<void>;
  refreshSettings: () => Promise<void>;

  refreshProjects: () => Promise<void>;
  refreshEpics: () => Promise<void>;
  setEpicFilter: (epicId: string | null) => void;
  createEpic: (title: string) => Promise<Epic | null>;
  createProject: (body: {
    name: string;
    root: string;
    extra_paths?: string[];
    verify_command?: string;
    executor?: StoryExecution;
    delivery?: ProjectDelivery;
    merge_train?: boolean;
    train_window_s?: number;
    train_max?: number;
    pr_api_base?: string;
    pr_web_base?: string;
    pr_repo?: string;
    pr_base_branch?: string;
    pr_token_file?: string;
  }) => Promise<void>;
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
      code_graph_enabled?: boolean;
      pr_api_base?: string;
    pr_web_base?: string;
      pr_repo?: string;
      pr_base_branch?: string;
      pr_token_file?: string;
    },
  ) => Promise<void>;
  deleteProject: (id: string) => Promise<void>;
  refreshPermissions: () => Promise<void>;
  decidePermission: (id: string, decision: "approve" | "deny") => Promise<void>;
  patchStoryAction: (
    id: string,
    body: {
      title?: string;
      description?: string;
      acceptance?: string;
      epic_id?: string | null;
      depends_on?: string[];
      execution?: StoryExecution | null;
      kind?: string;
    },
  ) => Promise<void>;
}

function runningToolsOf(stories: Story[]): Record<string, RunningTool[]> {
  const out: Record<string, RunningTool[]> = {};
  for (const s of Array.isArray(stories) ? stories : []) {
    if (s.running_tools && s.running_tools.length > 0) out[s.id] = s.running_tools;
  }
  return out;
}

function upsertStory(stories: Story[], story: Story): Story[] {
  const idx = stories.findIndex((s) => s.id === story.id);
  if (idx === -1) return [story, ...stories];
  const existing = stories[idx];
  // Stale-event guard: the SSE bus can deliver out of order (or replay after
  // a reconnect). An older snapshot must never overwrite a newer one — the
  // card would jump back to a previous column. Strict `<` so an equal
  // `updated_at` (same-second double publish) still replaces, matching the
  // pre-guard behavior.
  if (story.updated_at < existing.updated_at) return stories;
  const next = stories.slice();
  next[idx] = story;
  return next;
}

/** Activity events (round / tool_call / tool_result) seeded from a story
 * detail's persisted events — the base the live SSE feed appends to. */
function activityFromEvents(
  events: { kind: string; data: string; created_at?: string }[],
): StoryActivityEvent[] {
  const out: StoryActivityEvent[] = [];
  for (const ev of events) {
    const parsed = parseActivityEvent(ev.kind, ev.data);
    if (parsed) out.push(parsed);
  }
  return out.slice(-MAX_ACTIVITY);
}

export const useStore = create<FubalaState>()((set, get) => ({
  view: "chat",
  urlRestored: false,
  sessions: [],
  currentId: null,
  messages: [],
  streaming: false,
  streamText: "",
  streamTools: [],
  streamError: null,
  models: null,
  selectedModel: null,
  stories: [],
  openStoryId: null,
  storyDetail: null,
  storyLogs: {},
  storyProgress: {},
  storyRunningTools: {},
  storyActivity: {},
  providers: [],
  providerLimits: [],
  appSettings: null,
  projects: [],
  epics: [],
  epicFilter: null,
  projectFilter: null,
  pendingPermissions: [],
  pendingToastId: null,
  schedulerStalledSince: null,
  authRequired: false,

  init: async () => {
    const [
      models,
      sessions,
      stories,
      providers,
      providerLimits,
      appSettings,
      projects,
      epics,
      perms,
    ] = await Promise.all([
        api.models(),
        api.listSessions(),
        api.listStories(),
        api.listProviders(),
        api.fetchProviderLimits(),
        api.getSettings(),
        api.listProjects(),
        api.listEpics(),
        api.pendingPermissions(),
      ]).catch((e: unknown) => {
        // A 401 here means the silent loopback mint failed too: the browser
        // is remote and has no session cookie. Show the sign-in gate rather
        // than a generic failure; the error still rejects so App.tsx skips
        // its normal error log for this expected case.
        if (e instanceof UnauthorizedError) set({ authRequired: true });
        throw e;
      });
    set({
      models,
      sessions,
      stories,
      storyRunningTools: runningToolsOf(stories),
      providers,
      providerLimits,
      appSettings,
      projects,
      epics,
      pendingPermissions: perms,
      selectedModel: models.default_model,
    });
    get().connectBoard();
    if (sessions.length > 0) await get().openSession(sessions[0].id);
    await get().applyLocation(document.location);
  },

  applyLocation: async (loc) => {
    const { view, story, project, epic } = parseLocation(loc.pathname, loc.search);
    set({
      view,
      projectFilter: project ?? null,
      epicFilter: epic ?? null,
    });
    // Only restore a story we actually know about — a stale `?story=` (deleted
    // elsewhere, wrong board) is ignored rather than opening an empty panel.
    const target = story && get().stories.some((s) => s.id === story) ? story : null;
    if (target !== null) {
      if (get().openStoryId !== target) await get().openStory(target);
    } else if (get().openStoryId !== null) {
      get().closeStory();
    }
    set({ urlRestored: true });
  },

  setView: (view) => set({ view }),

  newSession: async (projectId) => {
    const { selectedModel } = get();
    const session = await api.createSession({
      model: selectedModel ?? undefined,
      ...(projectId ? { project_id: projectId } : {}),
    });
    set({
      currentId: session.id,
      sessions: [session, ...get().sessions],
      messages: [],
      streamError: null,
      view: "chat",
    });
  },

  openSession: async (id) => {
    if (get().streaming) get().stop();
    const detail = await api.getSession(id);
    set({
      currentId: detail.id,
      messages: detail.messages,
      streaming: false,
      streamText: "",
      streamTools: [],
      streamError: null,
    });
  },

  deleteSession: async (id) => {
    await api.deleteSession(id);
    const sessions = get().sessions.filter((s) => s.id !== id);
    set({ sessions });
    if (get().currentId === id) {
      if (sessions.length > 0) {
        await get().openSession(sessions[0].id);
      } else {
        set({ currentId: null, messages: [] });
      }
    }
  },

  setSelectedModel: async (model) => {
    set({ selectedModel: model });
    const { currentId } = get();
    if (currentId) {
      await api.patchSession(currentId, { model });
      set({
        sessions: get().sessions.map((s) =>
          s.id === currentId ? { ...s, model } : s,
        ),
      });
    }
  },

  send: async (content) => {
    if (get().streaming) return;
    let sessionId = get().currentId;
    const { selectedModel } = get();

    if (!sessionId) {
      const session = await api.createSession({ model: selectedModel ?? undefined });
      sessionId = session.id;
      set({ currentId: sessionId, sessions: [session, ...get().sessions] });
    }

    const optimistic: Message = {
      id: -Date.now(),
      session_id: sessionId,
      role: "user",
      content,
      model: null,
      prompt_tokens: null,
      completion_tokens: null,
      finish_reason: null,
      created_at: new Date().toISOString(),
    };
    set((st) => ({
      messages: [...st.messages, optimistic],
      streaming: true,
      streamText: "",
      streamTools: [],
      streamError: null,
    }));

    controller = new AbortController();
    const sid = sessionId;
    try {
      await streamMessage(
        sid,
        content,
        selectedModel,
        {
          onDelta: (text) => set((st) => ({ streamText: st.streamText + text })),
          onTool: (tool) =>
            set((st) => ({
              streamTools: [...st.streamTools, { name: tool.name, arguments: tool.arguments }],
            })),
          onDone: () => {},
          onError: (message) => set({ streamError: message }),
        },
        controller.signal,
      );
    } catch (e) {
      if ((e as Error).name !== "AbortError") set({ streamError: (e as Error).message });
    } finally {
      controller = null;
      set({ streaming: false, streamText: "", streamTools: [] });
      // re-sync with server truth (ids, usage, partial/error persistence)
      try {
        const [detail, sessions] = await Promise.all([api.getSession(sid), api.listSessions()]);
        set({ messages: detail.messages, sessions });
        const s = sessions.find((x) => x.id === sid);
        if (s && s.title === "New chat") {
          const title = content.length > 48 ? `${content.slice(0, 48)}…` : content;
          await api.patchSession(sid, { title });
          set({ sessions: await api.listSessions() });
        }
      } catch {
        /* refresh is best-effort; UI keeps optimistic state */
      }
    }
  },

  stop: () => {
    controller?.abort();
    set({ streaming: false });
  },

  refreshStories: async () => {
    const stories = await api.listStories();
    set({ stories, storyRunningTools: runningToolsOf(stories) });
  },

  openStory: async (id) => {
    clearDetailRefresh(); // drop any pending refresh for the previously open story
    set({ openStoryId: id });
    try {
      const detail = await api.getStory(id);
      set({
        storyDetail: detail,
        // replay the run from persisted events (refresh mid-run)
        storyActivity: { ...get().storyActivity, [id]: activityFromEvents(detail.events) },
      });
    } catch {
      set({ storyDetail: null });
    }
  },

  closeStory: () => {
    clearDetailRefresh();
    set({ openStoryId: null, storyDetail: null });
  },

  createStory: async (form) => {
    const story = await api.createStory(form);
    set({ stories: upsertStory(get().stories, story), view: "board" });
  },

  deleteStory: async (id) => {
    await api.deleteStory(id);
    set({
      stories: get().stories.filter((s) => s.id !== id),
      ...(get().openStoryId === id ? { openStoryId: null, storyDetail: null } : {}),
    });
  },

  storyAction: async (id, action, body) => {
    const story = await api.storyAction(id, action, body);
    set({ stories: upsertStory(get().stories, story) });
    if (get().openStoryId === id) {
      try {
        set({ storyDetail: await api.getStory(id) });
      } catch {
        /* detail refresh is best-effort */
      }
    }
  },

  // Supersede is a dedicated endpoint (only failed or human-review stories
  // may be closed this way), but board handling is identical to any other
  // status change.
  supersedeStory: async (id, by, supersededBy) => {
    const story = await api.supersedeStory(id, by, supersededBy);
    set({ stories: upsertStory(get().stories, story) });
    if (get().openStoryId === id) {
      try {
        set({ storyDetail: await api.getStory(id) });
      } catch {
        /* detail refresh is best-effort */
      }
    }
  },

  // Re-arm the landing poller: failed push+PR stories re-enter `landing`
  // from the failed lane (retry stage "landing"); board handling is the
  // same upsert + best-effort detail refresh as every other action.
  retryLanding: async (id, by) => {
    const story = await api.retryLanding(id, by);
    set({ stories: upsertStory(get().stories, story) });
    if (get().openStoryId === id) {
      try {
        set({ storyDetail: await api.getStory(id) });
      } catch {
        /* detail refresh is best-effort */
      }
    }
  },

  dismissToast: () => set({ pendingToastId: null }),

  connectBoard: () => {
    if (boardEvents) return;
    boardEvents = new EventSource("/api/v1/stories/events");
    // the scheduler heartbeat rides along with the board's live connection:
    // one immediate check (a stall already in progress shows at load) and
    // then the slow HEALTH_POLL_MS interval
    if (healthTimer === null) {
      pollSchedulerHealth();
      healthTimer = setInterval(pollSchedulerHealth, HEALTH_POLL_MS);
    }
    // EventSource auto-reconnects (server restart / self-update SIGTERM /
    // dropped connection). The bus replays nothing on subscribe, so without
    // a resync the board would silently freeze on pre-restart state until a
    // manual reload. Pull authoritative state on every (re)open.
    boardEvents.onopen = () => {
      get()
        .refreshStories()
        .catch(() => {
          /* resync is best-effort: the next event or open retries it */
        });
    };
    boardEvents.addEventListener("story", (e) => {
      const { story } = JSON.parse((e as MessageEvent).data) as { story: Story };
      const currentPendingId = get().pendingToastId;

      // Check if story just transitioned to human_review
      if (story.status === "human_review" && story.id !== currentPendingId) {
        // Play ding and set toast
        playReviewDing();
        set({ pendingToastId: story.id });
      }

      set((st) => ({ stories: upsertStory(st.stories, story) }));
      api.listEpics().then((epics) => set({ epics })).catch(() => {});
      // a requeued story is how a hold STARTS; the board's badge poll is how
      // it clears (holds expire server-side without any story event)
      api.fetchProviderLimits().then((providerLimits) => set({ providerLimits })).catch(
        () => {},
      );
      if (get().openStoryId === story.id) {
        scheduleDetailRefresh(story.id); // re-seeds activity from persisted events
        if (story.status !== "in_progress") {
          set((st) => ({ storyLogs: { ...st.storyLogs, [story.id]: "" } }));
        }
      } else if (
        story.status === "human_review" ||
        story.status === "landing" ||
        story.status === "done" ||
        story.status === "failed"
      ) {
        // Omit the finished story's entry; `_drop` only destructures it away.
        const { [story.id]: _drop, ...rest } = get().storyProgress;
        const { [story.id]: _dropTools, ...restTools } = get().storyRunningTools;
        set({ storyProgress: rest, storyRunningTools: restTools });
      }
    });
    boardEvents.addEventListener("story_progress", (e) => {
      const { story_id, kind, data } = JSON.parse((e as MessageEvent).data) as {
        story_id: string;
        kind: string;
        data: unknown;
      };
      if (kind === "round") {
        set((st) => ({
          storyProgress: { ...st.storyProgress, [story_id]: data as StoryProgress },
        }));
      }
      if (kind === "tool_started") {
        const tool = data as RunningTool;
        set((st) => ({
          storyRunningTools: {
            ...st.storyRunningTools,
            [story_id]: [
              ...(st.storyRunningTools[story_id] ?? []).filter((t) => t.call_id !== tool.call_id),
              tool,
            ],
          },
        }));
      } else if (kind === "tool_finished") {
        const { call_id } = data as { call_id: string };
        set((st) => ({
          storyRunningTools: {
            ...st.storyRunningTools,
            [story_id]: (st.storyRunningTools[story_id] ?? []).filter((t) => t.call_id !== call_id),
          },
        }));
      }
      if (kind === "round" || kind === "tool_call" || kind === "tool_result" || kind === "headless_stream") {
        set((st) => {
          const current = st.storyActivity[story_id] ?? [];
          const next = [...current, { kind, data } as StoryActivityEvent].slice(-MAX_ACTIVITY);
          return { storyActivity: { ...st.storyActivity, [story_id]: next } };
        });
      }
    });
    boardEvents.addEventListener("permission", () => {
      api
        .pendingPermissions()
        .then((pendingPermissions) => set({ pendingPermissions }))
        .catch(() => {});
    });
    boardEvents.addEventListener("permission_decided", () => {
      api
        .pendingPermissions()
        .then((pendingPermissions) => set({ pendingPermissions }))
        .catch(() => {});
    });
    boardEvents.addEventListener("story_log", (e) => {
      const { story_id, text } = JSON.parse((e as MessageEvent).data) as {
        story_id: string;
        text: string;
      };
      set((st) => {
        const current = st.storyLogs[story_id] ?? "";
        const next = (current + text).slice(-MAX_LOG);
        return { storyLogs: { ...st.storyLogs, [story_id]: next } };
      });
    });
    boardEvents.addEventListener("story_note", (e) => {
      const { story_id } = JSON.parse((e as MessageEvent).data) as {
        story_id: string;
      };
      if (get().openStoryId === story_id) {
        scheduleDetailRefresh(story_id);
      }
    });
  },

  // ---- providers ---------------------------------------------------------

  refreshProviders: async () => {
    const [providers, models] = await Promise.all([api.listProviders(), api.models()]);
    set({ providers, models });
  },

  refreshProviderLimits: async () => {
    set({ providerLimits: await api.fetchProviderLimits() });
  },

  clearProviderLimit: async (id) => {
    await api.clearProviderLimit(id);
    await get().refreshProviderLimits();
  },

  createProvider: async (form) => {
    await api.createProvider(form);
    await get().refreshProviders();
  },

  patchProvider: async (id, body) => {
    await api.patchProvider(id, body);
    await get().refreshProviders();
  },

  deleteProvider: async (id) => {
    await api.deleteProvider(id);
    await get().refreshProviders();
  },

  setProviderKey: async (id, key) => {
    await api.setProviderKey(id, key);
    await get().refreshProviders();
    if (key) get().refreshProviderModels(id).catch(() => {}); // auto-sync live models
  },

  addProviderModel: async (id, model_id, skill, cost) => {
    await api.addProviderModel(id, { model_id, skill, cost });
    await get().refreshProviders();
  },

  updateProviderModel: async (id, rowId, body) => {
    await api.updateProviderModel(id, rowId, body);
    await get().refreshProviders();
  },

  deleteProviderModel: async (id, rowId) => {
    await api.deleteProviderModel(id, rowId);
    await get().refreshProviders();
  },

  refreshProviderModels: async (id, removeMissing = false) => {
    await api.refreshProviderModels(id, removeMissing);
    await get().refreshProviders();
  },

  patchAppSettings: async (body) => {
    set({ appSettings: await api.patchSettings(body) });
  },
  refreshSettings: async () => {
    set({ appSettings: await api.getSettings() });
  },

  refreshProjects: async () => {
    set({ projects: await api.listProjects() });
  },

  refreshEpics: async () => {
    set({ epics: await api.listEpics() });
  },

  setEpicFilter: (epicId) => set({ epicFilter: epicId }),
  setProjectFilter: (id) => set({ projectFilter: id }),

  createEpic: async (title) => {
    try {
      const epic = await api.createEpic({ title });
      await get().refreshEpics();
      return epic;
    } catch {
      return null;
    }
  },

  createProject: async (body) => {
    await api.createProject(body);
    await get().refreshProjects();
  },

  patchProject: async (id, body) => {
    await api.patchProject(id, body);
    await get().refreshProjects();
  },

  deleteProject: async (id) => {
    await api.deleteProject(id);
    await Promise.all([get().refreshProjects(), get().refreshStories()]);
  },

  refreshPermissions: async () => {
    set({ pendingPermissions: await api.pendingPermissions() });
  },

  decidePermission: async (id, decision) => {
    await api.decidePermission(id, decision);
    await get().refreshPermissions();
  },

  patchStoryAction: async (id, body) => {
    try {
      await api.patchStory(id, body);
      const detail = await api.getStory(id);
      set({
        stories: upsertStory(get().stories, detail),
        storyDetail: get().openStoryId === id ? detail : get().storyDetail,
      });
      api.listEpics().then((epics) => set({ epics })).catch(() => {});
    } catch (e) {
      alert((e as Error).message);
    }
  },
}));
