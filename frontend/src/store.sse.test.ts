/**
 * Store-level tests for live-refreshing story detail via board SSE.
 *
 * Covers the acceptance criteria:
 *  - `story` event → storyDetail replaced (debounced, no manual refetch path)
 *  - `story_note` event → notes timeline data updated
 *  - burst of events within DETAIL_REFRESH_MS → exactly one getStory call
 *  - after closeStory → zero getStory calls, no leaked timer
 *  - document.title behaviour of the tab-title helper
 *
 * No jsdom/@testing-library: we assert on the store state StoryDetail renders.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { COLUMNS, belongsToColumn } from "@/components/boardColumns";
import type { Story, StoryDetail } from "@/types";
import { APP_TITLE, storyTabTitle } from "@/utils/tabTitle";
import { DETAIL_REFRESH_MS } from "@/store";

vi.mock("@/api", () => ({
  api: {
    listStories: vi.fn().mockResolvedValue([]),
    listEpics: vi.fn().mockResolvedValue([]),
    getStory: vi.fn(),
    // the story-event handler refreshes provider limit holds alongside the
    // story list; an unmocked method would throw inside the SSE handler
    fetchProviderLimits: vi.fn().mockResolvedValue([]),
  },
  streamMessage: vi.fn(),
}));

// keep Web Audio out of the node test env
vi.mock("@/utils/softDing", () => ({ playReviewDing: vi.fn() }));

type Listener = (event: { data: string }) => void;

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  url: string;
  closed = false;
  listeners = new Map<string, Listener[]>();
  /** set by the store's `onopen` handler (reconnect resync) */
  onopen: (() => void) | null = null;

  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }

  addEventListener(kind: string, fn: Listener): void {
    const list = this.listeners.get(kind) ?? [];
    list.push(fn);
    this.listeners.set(kind, list);
  }

  removeEventListener(kind: string, fn: Listener): void {
    const list = (this.listeners.get(kind) ?? []).filter((f) => f !== fn);
    this.listeners.set(kind, list);
  }

  close(): void {
    this.closed = true;
  }

  /** Test helper: dispatch an event of `kind` with a JSON payload. */
  emit(kind: string, payload: unknown): void {
    for (const fn of this.listeners.get(kind) ?? []) {
      fn({ data: JSON.stringify(payload) });
    }
  }

  /** Test helper: simulate an EventSource (re)open / auto-reconnect. */
  emitOpen(): void {
    this.onopen?.();
  }
}

const STORY_ID = "story-1";

function makeStory(overrides: Partial<Story> = {}): Story {
  return {
    id: STORY_ID,
    title: "Live refresh",
    description: "",
    acceptance: "",
    status: "queued",
    project_id: null,
    epic_id: null,
    depends_on: [],
    model: null,
    skill: null,
    created_by: "tester",
    // push+PR delivery fields: null until approval opens a pull request
    pr_url: null,
    pr_index: null,
    pushed_sha: null,
    execution: "internal",
    execution_override: null,
    gate_spec: "human",
    gate_code: "human",
    human_rejected: false,
    kind: "code",
    claimed_by: null,
    claimed_at: null,
    spec: null,
    spec_version: 1,
    base_ref: null,
    verify_result: null,
    ai_review: null,
    ai_verdict: null,
    approved_by: null,
    error: null,
    created_at: "2025-01-01T00:00:00Z",
    updated_at: "2025-01-01T00:00:00Z",
    ...overrides,
  };
}

function makeDetail(overrides: Partial<StoryDetail> = {}): StoryDetail {
  return {
    ...makeStory(overrides),
    events: [],
    notes: [],
    code_diff: null,
    evidence: null,
    ...overrides,
  };
}

/** Re-imported in beforeEach (vi.resetModules) so the module-level board
 *  EventSource guard doesn't leak a connection between tests. */
let useStore = (await import("@/store")).useStore;
let getStory = vi.mocked((await import("@/api")).api.getStory);
let listStories = vi.mocked((await import("@/api")).api.listStories);

async function flush(): Promise<void> {
  await vi.advanceTimersByTimeAsync(DETAIL_REFRESH_MS);
  await Promise.resolve();
  await Promise.resolve();
}

beforeEach(async () => {
  vi.resetModules();
  useStore = (await import("@/store")).useStore;
  getStory = vi.mocked((await import("@/api")).api.getStory);
  listStories = vi.mocked((await import("@/api")).api.listStories);
  vi.useFakeTimers();
  FakeEventSource.instances = [];
  vi.stubGlobal("EventSource", FakeEventSource);
  getStory.mockReset().mockResolvedValue(makeDetail());
  listStories.mockReset().mockResolvedValue([]);
  useStore.setState({
    stories: [makeStory()],
    openStoryId: null,
    storyDetail: null,
    storyLogs: {},
    pendingToastId: null,
  });
});

afterEach(() => {
  useStore.getState().closeStory();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

function board(): FakeEventSource {
  useStore.getState().connectBoard();
  const es = FakeEventSource.instances.at(-1);
  if (!es) throw new Error("connectBoard did not create an EventSource");
  return es;
}

describe("board SSE → live story detail", () => {
  it("refreshes storyDetail after a `story` event (debounced)", async () => {
    const es = board();
    await useStore.getState().openStory(STORY_ID);
    expect(getStory).toHaveBeenCalledTimes(1); // initial open

    const detail = makeDetail({ status: "in_progress", title: "Live refresh" });
    getStory.mockResolvedValue(detail);
    es.emit("story", { story: makeStory({ status: "in_progress" }) });

    expect(getStory).toHaveBeenCalledTimes(1); // nothing immediate
    await flush();
    expect(getStory).toHaveBeenCalledTimes(2);
    expect(getStory).toHaveBeenLastCalledWith(STORY_ID);
    expect(useStore.getState().storyDetail).toBe(detail);
  });

  it("updates the notes timeline after a `story_note` event", async () => {
    const es = board();
    await useStore.getState().openStory(STORY_ID);

    const withNotes = makeDetail({
      notes: [
        {
          id: 7,
          story_id: STORY_ID,
          body: "coder: tests green",
          author: "coder",
          created_at: "2025-01-01T00:01:00Z",
        },
      ],
    });
    getStory.mockResolvedValue(withNotes);
    es.emit("story_note", { story_id: STORY_ID });

    await flush();
    expect(getStory).toHaveBeenCalledTimes(2);
    expect(useStore.getState().storyDetail?.notes).toHaveLength(1);
    expect(useStore.getState().storyDetail?.notes[0]?.body).toBe("coder: tests green");
  });

  it("collapses a burst of events into one getStory call", async () => {
    const es = board();
    await useStore.getState().openStory(STORY_ID);
    getStory.mockClear();

    for (let i = 0; i < 5; i++) {
      es.emit("story", { story: makeStory({ status: "in_progress" }) });
      es.emit("story_note", { story_id: STORY_ID });
    }

    expect(getStory).not.toHaveBeenCalled();
    await flush();
    expect(getStory).toHaveBeenCalledTimes(1);
  });

  it("does nothing for events about other stories", async () => {
    const es = board();
    await useStore.getState().openStory(STORY_ID);
    getStory.mockClear();

    es.emit("story", { story: makeStory({ id: "other" }) });
    es.emit("story_note", { story_id: "other" });

    await flush();
    expect(getStory).not.toHaveBeenCalled();
  });

  it("fires zero getStory calls and no timer after closeStory", async () => {
    const es = board();
    await useStore.getState().openStory(STORY_ID);
    getStory.mockClear();

    es.emit("story", { story: makeStory({ status: "in_progress" }) });
    useStore.getState().closeStory();
    getStory.mockClear();

    // fire every pending fake timer — none of them may fetch
    await vi.advanceTimersByTimeAsync(DETAIL_REFRESH_MS * 10);
    await Promise.resolve();
    expect(getStory).not.toHaveBeenCalled();
    expect(useStore.getState().storyDetail).toBeNull();

    // and the panel stays quiet when more events arrive afterwards
    es.emit("story", { story: makeStory({ status: "in_progress" }) });
    es.emit("story_note", { story_id: STORY_ID });
    await flush();
    expect(getStory).not.toHaveBeenCalled();
  });

  it("keeps the shared board EventSource open when the panel closes", () => {
    const es = board();
    useStore.getState().closeStory();
    expect(es.closed).toBe(false);
  });
});

describe("tab title helper", () => {
  it("formats status and title", () => {
    expect(storyTabTitle("in_progress", "Fix login")).toBe("in_progress · Fix login");
  });

  it("uses a non-empty app default to restore to", () => {
    expect(APP_TITLE).toBe("Fubala");
    // StoryDetail restores `previous || APP_TITLE`; the default must exist so
    // a panel closed from a blank title still restores something sane.
    expect(storyTabTitle("done", "Ship it")).not.toBe(APP_TITLE);
  });
});

// ---- board column correctness (defect 2026-09-27) ----------------------
//
// The report: cards rendered outside the column implied by `status` after
// SSE glitches. These tests pin the store-side guarantees the fix rests on:
// a stale snapshot never wins, a full external cycle lands in Done, and a
// reconnect pulls authoritative state instead of freezing on stale state.

const T1 = "2026-09-27T10:00:00Z";
const T2 = "2026-09-27T10:00:05Z";

describe("board SSE stale-event guard", () => {
  it("an older `story` snapshot never overwrites a newer one", () => {
    const es = board();
    // newest first (the reconnect/replay glitch the report describes)
    es.emit("story", { story: makeStory({ status: "done", updated_at: T2 }) });
    expect(useStore.getState().stories[0].status).toBe("done");
    es.emit("story", { story: makeStory({ status: "in_progress", updated_at: T1 }) });

    const stored = useStore.getState().stories.find((s) => s.id === STORY_ID);
    expect(stored?.status).toBe("done");
  });

  it("an equal `updated_at` still replaces (same-second double publish)", () => {
    const es = board();
    es.emit("story", { story: makeStory({ status: "in_progress", updated_at: T1 }) });
    es.emit("story", { story: makeStory({ status: "human_review", updated_at: T1 }) });

    expect(useStore.getState().stories.find((s) => s.id === STORY_ID)?.status).toBe(
      "human_review",
    );
  });

  it("a newer snapshot still replaces a stale stored one", () => {
    const es = board();
    es.emit("story", { story: makeStory({ status: "queued", updated_at: T1 }) });
    es.emit("story", { story: makeStory({ status: "human_review", updated_at: T2 }) });

    expect(useStore.getState().stories.find((s) => s.id === STORY_ID)?.status).toBe(
      "human_review",
    );
  });

  it("a brand-new story is still prepended", () => {
    const es = board();
    es.emit("story", { story: makeStory({ id: "fresh", updated_at: T1 }) });

    const stories = useStore.getState().stories;
    expect(stories.some((s) => s.id === "fresh")).toBe(true);
    expect(stories[0].id).toBe("fresh");
  });
});

describe("board SSE → external cycle", () => {
  it("queued → claim → complete → approve ends `done`, exclusively in the Done column", () => {
    const es = board();

    // exactly the payloads the server publishes for the external cycle
    const chain: Story[] = [
      makeStory({ status: "queued", execution: "external", updated_at: "2026-09-27T10:00:00Z" }),
      makeStory({
        status: "in_progress",
        execution: "external",
        claimed_by: "agent-x",
        updated_at: "2026-09-27T10:00:01Z",
      }),
      makeStory({ status: "human_review", execution: "external", updated_at: "2026-09-27T10:00:02Z" }),
      makeStory({ status: "done", execution: "external", updated_at: "2026-09-27T10:00:03Z" }),
    ];
    for (const story of chain) es.emit("story", { story });

    const { stories } = useStore.getState();
    const final = stories.find((s) => s.id === STORY_ID);
    expect(final?.status).toBe("done");

    // appears exclusively in Done — no other column claims it
    for (const column of COLUMNS) {
      const members = stories.filter((s) => belongsToColumn(s, column.status));
      if (column.status === "done") {
        expect(members.map((s) => s.id)).toContain(STORY_ID);
      } else {
        expect(members.map((s) => s.id)).not.toContain(STORY_ID);
      }
    }
  });
});

describe("board SSE reconnect (onopen)", () => {
  it("resyncs by pulling authoritative state via refreshStories", async () => {
    listStories.mockResolvedValueOnce([
      makeStory({ id: "authoritative", status: "done", updated_at: T2 }),
    ]);

    const es = board();
    expect(listStories).not.toHaveBeenCalled();
    es.emitOpen();
    await flush();

    expect(listStories).toHaveBeenCalledTimes(1);
    expect(useStore.getState().stories[0].id).toBe("authoritative");
  });

  it("swallows refresh errors so a failed resync never breaks the bus", async () => {
    listStories.mockRejectedValueOnce(new Error("server restarting"));

    const es = board();
    expect(() => es.emitOpen()).not.toThrow();
    await flush();

    // the bus is still alive: subsequent events still land
    es.emit("story", { story: makeStory({ status: "done", updated_at: T2 }) });
    expect(useStore.getState().stories.find((s) => s.id === STORY_ID)?.status).toBe("done");
  });
});


describe("board SSE → running tools", () => {
  const tool = (id: string, started_at: string) => ({
    call_id: id,
    name: "run_verify",
    args: "{}",
    started_at,
    timeout_s: 600,
  });

  it("fills from listStories running_tools", async () => {
    const t = tool("a", "2025-01-01T00:00:00.000Z");
    listStories.mockResolvedValue([makeStory({ status: "executing", running_tools: [t] })]);
    await useStore.getState().refreshStories();
    expect(useStore.getState().storyRunningTools[STORY_ID]).toEqual([t]);
  });

  it("appends tool_started, removes by call_id, ignores story updates", () => {
    const es = board();
    es.emit("story_progress", {
      story_id: STORY_ID,
      kind: "tool_started",
      data: tool("a", "2025-01-01T00:00:00.000Z"),
    });
    es.emit("story_progress", {
      story_id: STORY_ID,
      kind: "tool_started",
      data: tool("b", "2025-01-01T00:00:01.000Z"),
    });
    expect(useStore.getState().storyRunningTools[STORY_ID]).toHaveLength(2);
    es.emit("story", { story: makeStory({ status: "executing" }) });
    expect(useStore.getState().storyRunningTools[STORY_ID]).toHaveLength(2);
    es.emit("story_progress", { story_id: STORY_ID, kind: "tool_finished", data: { call_id: "a" } });
    expect(useStore.getState().storyRunningTools[STORY_ID]?.map((t) => t.call_id)).toEqual(["b"]);
  });
});
