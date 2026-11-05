/** Fallback tab title — matches `<title>` in index.html. */
export const APP_TITLE = "Fubala";

/**
 * Tab title shown while a story-detail panel is open:
 * `status · title`, so a glance at the tab tells you where a story is.
 */
export function storyTabTitle(status: string, title: string): string {
  return `${status} · ${title}`;
}
