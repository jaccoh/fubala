/**
 * URL ⇄ view-state mapping for the SPA (there is no router).
 *
 * The whole addressable state of the app is:
 *   /<view>?story=<open story id>&project=<project filter>&epic=<epic filter>
 *
 * Everything here is pure (no window/history access) so it can be unit tested
 * in the node env; the actual history writing lives in App.tsx.
 */

/** The four top-level views; also the set of routable path segments. */
export type View = "chat" | "board" | "settings" | "projects";

const VIEWS: readonly View[] = ["chat", "board", "settings", "projects"];

/** View used when the path segment is absent or not one of `VIEWS`. */
const DEFAULT_VIEW: View = "chat";

/** Result of `parseLocation`: only present params are set. */
export interface ParsedLocation {
  /** Always resolved (never undefined): unknown/absent segment → `chat`. */
  view: View;
  story?: string;
  project?: string;
  epic?: string;
}

/**
 * Parse a location into view + query params.
 *
 * - `/board` → `{ view: "board" }`
 * - `/nope`, `/`, `` → `{ view: "chat" }`
 * - unknown/empty query params are simply omitted
 */
export function parseLocation(pathname: string, search: string): ParsedLocation {
  const segment = pathname.replace(/^\/+/, "").split("/")[0] ?? "";
  const view = (VIEWS as readonly string[]).includes(segment) ? (segment as View) : DEFAULT_VIEW;

  const params = new URLSearchParams(search);
  const story = params.get("story") ?? undefined;
  const project = params.get("project") ?? undefined;
  const epic = params.get("epic") ?? undefined;

  return { view, ...(story ? { story } : {}), ...(project ? { project } : {}), ...(epic ? { epic } : {}) };
}

/**
 * Build the URL for a given state. Empty params are omitted, so the plain
 * default view serialises as `/chat` (no query string).
 */
export function buildUrl(
  view: View,
  openStoryId: string | null,
  projectFilter: string | null,
  epicFilter: string | null,
): string {
  const params = new URLSearchParams();
  if (openStoryId) params.set("story", openStoryId);
  if (projectFilter) params.set("project", projectFilter);
  if (epicFilter) params.set("epic", epicFilter);
  const qs = params.toString();
  return `/${view}${qs ? `?${qs}` : ""}`;
}
