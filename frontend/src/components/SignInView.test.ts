/**
 * Sign-in gate tests: 401 during init renders the gate; the token is POSTed
 * as a Bearer header and reloads on 200; a wrong token surfaces "Token is not
 * valid"; and nothing is ever written to web storage.
 *
 * Same node-env pattern as store.sse.test.ts: stubbed fetch (so the REAL
 * `@/api` module runs — that is where the retry-and-throw logic lives), a
 * fake `window.location`, a fake `document.location` for the success path.
 * No jsdom.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { performSignIn, INVALID_TOKEN_MESSAGE } from "@/components/SignInView";
import { UnauthorizedError } from "@/api";

// keep Web Audio out of the node test env (same as store.sse.test.ts)
vi.mock("@/utils/softDing", () => ({ playReviewDing: vi.fn() }));

type Listener = (event: { data: string }) => void;

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  constructor(_url: string) {
    FakeEventSource.instances.push(this);
  }
  addEventListener(_kind: string, _fn: Listener): void {}
  removeEventListener(_kind: string, _fn: Listener): void {}
  close(): void {}
}

let useStore = (await import("@/store")).useStore;

const fetchCalls: { url: string; init?: RequestInit }[] = [];

function fetchResponse(status: number): Response {
  return {
    ok: status < 400,
    status,
    statusText: status === 200 ? "OK" : status === 401 ? "Unauthorized" : "Error",
    json: async () =>
      status < 400
        ? { default_model: "m", stories: [], sessions: [], providers: [] }
        : { detail: "authentication required" },
    text: async () => "",
  } as unknown as Response;
}

beforeEach(async () => {
  vi.resetModules();
  useStore = (await import("@/store")).useStore;
  FakeEventSource.instances = [];
  fetchCalls.length = 0;
  vi.stubGlobal("EventSource", FakeEventSource);
  setTestWindow();
  setTestFetch(async () => fetchResponse(401));
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

/** Node has no `window`; give the modules the two things they touch. */
function setTestWindow(): void {
  const reload = vi.fn();
  vi.stubGlobal("window", { location: { reload }, history: {}, addEventListener: () => {} });
  vi.stubGlobal("document", {
    location: { pathname: "/", search: "", href: "http://localhost/" },
    addEventListener: () => {},
    removeEventListener: () => {},
  });
}

function reloadMock(): ReturnType<typeof vi.fn> {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  return (globalThis.window as any).location.reload;
}

function setTestFetch(impl: (input: string | URL | Request, init?: RequestInit) => Promise<Response>): void {
  const wrapper = (input: string | URL | Request, init?: RequestInit) => {
    const url = typeof input === "string" ? input : String(input);
    fetchCalls.push({ url, init });
    return impl(input, init);
  };
  vi.stubGlobal("fetch", wrapper);
}

describe("store: 401 during init → sign-in gate", () => {
  it("sets authRequired when every API call 401s", async () => {
    await useStore.getState().init().catch((e: unknown) => e);
    expect(useStore.getState().authRequired).toBe(true);
    // and no board SSE stream was opened while signed out
    expect(FakeEventSource.instances).toHaveLength(0);
  });

  it("leaves authRequired false on a normal (200) init", async () => {
    setTestFetch(async () => fetchResponse(200));
    useStore.setState({ urlRestored: false });
    await useStore.getState().init();
    expect(useStore.getState().authRequired).toBe(false);
  });

  it("does not flag authRequired for a non-401 init failure", async () => {
    setTestFetch(async () => fetchResponse(500));
    await useStore.getState().init().catch((e: unknown) => e);
    expect(useStore.getState().authRequired).toBe(false);
  });
});

describe("performSignIn: token exchange", () => {
  it("POSTs /api/v1/auth/session with the Bearer header and reloads on 200", async () => {
    setTestFetch(async (input) => {
      const url = String(input);
      if (url.endsWith("/auth/session")) return fetchResponse(200);
      return fetchResponse(401);
    });
    const outcome = await performSignIn("fubal_good_token");
    expect(outcome).toBe("ok");
    const call = fetchCalls.find((c) => c.url.endsWith("/auth/session"));
    expect(call).toBeDefined();
    expect(call!.init?.method).toBe("POST");
    expect(call!.init?.headers).toEqual({ Authorization: "Bearer fubal_good_token" });
    expect(reloadMock()).toHaveBeenCalledTimes(1);
  });

  it("reports an invalid token (401) without reloading", async () => {
    setTestFetch(async () => fetchResponse(401));
    const outcome = await performSignIn("fubal_wrong");
    expect(outcome).toBe("invalid");
    expect(INVALID_TOKEN_MESSAGE).toBe("Token is not valid");
    expect(reloadMock()).not.toHaveBeenCalled();
  });

  it("reports an unreachable backend as a plain error, not a token problem", async () => {
    setTestFetch(async () => {
      throw new Error("network down");
    });
    const outcome = await performSignIn("fubal_anything");
    expect(outcome).toBe("error");
    expect(reloadMock()).not.toHaveBeenCalled();
  });
});

describe("no web storage, ever", () => {
  it("never touches localStorage or sessionStorage during sign-in", async () => {
    const setItem = vi.fn();
    vi.stubGlobal("localStorage", { setItem });
    vi.stubGlobal("sessionStorage", { setItem });
    setTestFetch(async () => fetchResponse(401));
    await performSignIn("fubal_wrong").catch(() => {});
    setTestFetch(async (input) =>
      String(input).endsWith("/auth/session") ? fetchResponse(200) : fetchResponse(401),
    );
    await performSignIn("fubal_right");
    expect(setItem).not.toHaveBeenCalled();
  });
});

describe("UnauthorizedError", () => {
  it("is distinguishable from a generic Error", () => {
    const e = new UnauthorizedError();
    expect(e).toBeInstanceOf(Error);
    expect(e.name).toBe("UnauthorizedError");
    expect(e.message).toBe("authentication required");
    expect(e instanceof UnauthorizedError).toBe(true);
  });
});
