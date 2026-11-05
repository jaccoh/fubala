/**
 * Store-level tests for URL ⇄ view-state persistence.
 *
 * Covers the acceptance criteria:
 *  - `applyLocation("/board?story=…&project=…&epic=…")` → view + filters set,
 *    story panel opened through the normal `openStory` path (getStory called)
 *  - `applyLocation("/")` → defaults, zero getStory calls
 *  - `applyLocation("/board?story=ghost")` → param ignored, no empty panel
 *  - after a restore, a board `story` SSE event still triggers exactly one
 *    debounced getStory (the restore keeps the shared-EventSource guards)
 *  - deleteStory/closeStory clear openStoryId, so the `story` param drops out
 *    of buildUrl(state) — what the App.tsx writer serialises
 *  - parseLocation/buildUrl round trips
 *
 * Same node-env pattern as store.sse.test.ts: mocked `@/api`, FakeEventSource.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Story, StoryDetail } from "@/types";
import { DETAIL_REFRESH_MS } from "@/store";
import { buildUrl, parseLocation } from "@/url";

vi.mock("@/api", () => ({
  api: {
    listStories: vi.fn().mockResolvedValue([]),
    listEpics: vi.fn().mockResolvedValue([]),
    getStory: vi.fn(),
    deleteStory: vi.fn().mockResolvedValue(undefined),
    // refreshed by the story-event handler (provider limit holds)
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
}

const STORY_ID = "story-1";

function makeStory(overrides: Partial<Story> = {}): Story {
  return {
    id: STORY_ID,
    title: "URL restore",
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

/** Re-imported in beforeEach (vi.resetModules) so module-level guards (the
 * board EventSource) don't leak between tests. */
let useStore = (await import("@/store")).useStore;
let getStory = vi.mocked((await import("@/api")).api.getStory);

async function flush(): Promise<void> {
  await vi.advanceTimersByTimeAsync(DETAIL_REFRESH_MS);
  await Promise.resolve();
  await Promise.resolve();
}

beforeEach(async () => {
  vi.resetModules();
  useStore = (await import("@/store")).useStore;
  getStory = vi.mocked((await import("@/api")).api.getStory);
  vi.useFakeTimers();
  FakeEventSource.instances = [];
  vi.stubGlobal("EventSource", FakeEventSource);
  getStory.mockReset().mockResolvedValue(makeDetail());
  useStore.setState({
    stories: [makeStory()],
    openStoryId: null,
    storyDetail: null,
    storyLogs: {},
    pendingToastId: null,
    projectFilter: null,
    epicFilter: null,
    urlRestored: false,
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

/** What the App.tsx writer would serialise for the current state. */
function currentUrl(): string {
  const { view, openStoryId, projectFilter, epicFilter } = useStore.getState();
  return buildUrl(view, openStoryId, projectFilter, epicFilter);
}

describe("parseLocation / buildUrl (pure)", () => {
  it("maps each path segment to a view", () => {
    expect(parseLocation("/board", "").view).toBe("board");
    expect(parseLocation("/projects", "").view).toBe("projects");
    expect(parseLocation("/settings", "").view).toBe("settings");
    expect(parseLocation("/chat", "").view).toBe("chat");
  });

  it("falls back to chat for unknown or absent segments", () => {
    expect(parseLocation("/", "").view).toBe("chat");
    expect(parseLocation("", "").view).toBe("chat");
    expect(parseLocation("/nope", "").view).toBe("chat");
    expect(parseLocation("/board/extra", "").view).toBe("board");
  });

  it("reads story/project/epic params and omits absent ones", () => {
    expect(parseLocation("/board", "?story=s1&project=p1&epic=e1")).toEqual({
      view: "board",
      story: "s1",
      project: "p1",
      epic: "e1",
    });
    expect(parseLocation("/board", "")).toEqual({ view: "board" });
    expect(parseLocation("/board", "?story=&project=p1")).toEqual({
      view: "board",
      project: "p1",
    });
  });

  it("buildUrl omits empty params and round-trips with parseLocation", () => {
    expect(buildUrl("board", "s1", "p1", "e1")).toBe("/board?story=s1&project=p1&epic=e1");
    expect(buildUrl("chat", null, null, null)).toBe("/chat");
    expect(buildUrl("projects", null, "p1", null)).toBe("/projects?project=p1");

    const built = buildUrl("board", "s1", "p1", "e1");
    const q = built.indexOf("?");
    expect(parseLocation(built.slice(0, q), built.slice(q))).toEqual({
      view: "board",
      story: "s1",
      project: "p1",
      epic: "e1",
    });
  });
});

describe("applyLocation", () => {
  it("restores view, filters and the open story panel", async () => {
    await useStore.getState().applyLocation({
      pathname: "/board",
      search: `?story=${STORY_ID}&project=proj-9&epic=epic-3`,
    });

    const st = useStore.getState();
    expect(st.view).toBe("board");
    expect(st.projectFilter).toBe("proj-9");
    expect(st.epicFilter).toBe("epic-3");
    expect(st.openStoryId).toBe(STORY_ID);
    expect(st.storyDetail).toEqual(makeDetail());
    expect(getStory).toHaveBeenCalledTimes(1);
    expect(getStory).toHaveBeenCalledWith(STORY_ID);
    expect(st.urlRestored).toBe(true);
    expect(currentUrl()).toBe(`/board?story=${STORY_ID}&project=proj-9&epic=epic-3`);
  });

  it("yields defaults for '/' and never calls getStory", async () => {
    await useStore.getState().applyLocation({ pathname: "/", search: "" });

    const st = useStore.getState();
    expect(st.view).toBe("chat");
    expect(st.projectFilter).toBeNull();
    expect(st.epicFilter).toBeNull();
    expect(st.openStoryId).toBeNull();
    expect(getStory).not.toHaveBeenCalled();
    expect(currentUrl()).toBe("/chat");
  });

  it("ignores a story id that is not in the loaded stories list", async () => {
    await useStore.getState().applyLocation({ pathname: "/board", search: "?story=ghost" });

    const st = useStore.getState();
    expect(st.view).toBe("board");
    expect(st.openStoryId).toBeNull();
    expect(st.storyDetail).toBeNull();
    expect(getStory).not.toHaveBeenCalled();
    // unknown story is dropped from the serialised URL too
    expect(currentUrl()).toBe("/board");
  });

  it("clears filters and the open panel a previous location had set", async () => {
    await useStore
      .getState()
      .applyLocation({ pathname: "/board", search: `?story=${STORY_ID}&project=p1&epic=e1` });
    expect(useStore.getState().openStoryId).toBe(STORY_ID);

    await useStore.getState().applyLocation({ pathname: "/settings", search: "" });
    const st = useStore.getState();
    expect(st.view).toBe("settings");
    expect(st.projectFilter).toBeNull();
    expect(st.epicFilter).toBeNull();
    expect(st.openStoryId).toBeNull();
    expect(currentUrl()).toBe("/settings");
  });
});

describe("URL state and the shared board SSE", () => {
  it("still refreshes a restored panel exactly once per debounced burst", async () => {
    const es = board();
    await useStore.getState().applyLocation({ pathname: "/board", search: `?story=${STORY_ID}` });
    expect(getStory).toHaveBeenCalledTimes(1); // the restore itself

    for (let i = 0; i < 3; i++) {
      es.emit("story", { story: makeStory({ status: "in_progress" }) });
      es.emit("story_note", { story_id: STORY_ID });
    }
    await flush();

    expect(getStory).toHaveBeenCalledTimes(2); // burst collapsed into one
    expect(getStory).toHaveBeenLastCalledWith(STORY_ID);
  });

  it("drops the story param when the story is deleted", async () => {
    await useStore.getState().applyLocation({ pathname: "/board", search: `?story=${STORY_ID}` });
    expect(currentUrl()).toBe(`/board?story=${STORY_ID}`);

    await useStore.getState().deleteStory(STORY_ID);
    expect(useStore.getState().openStoryId).toBeNull();
    expect(currentUrl()).toBe("/board");
  });

  it("drops the story param when the panel is closed", async () => {
    await useStore.getState().applyLocation({ pathname: "/board", search: `?story=${STORY_ID}&project=p1` });

    useStore.getState().closeStory();
    expect(useStore.getState().openStoryId).toBeNull();
    expect(currentUrl()).toBe("/board?project=p1");
  });
});
