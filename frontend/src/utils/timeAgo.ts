/**
 * Compact relative time ("just now", "3m ago", "2h ago", "5d ago") for card
 * and panel meta lines. Extracted from BoardView so StoryDetail (and any
 * future surface) can share one implementation.
 */
import { parseServerTime } from "./parseServerTime";

export function timeAgo(iso: string): string {
  const s = Math.max(0, (Date.now() - parseServerTime(iso).getTime()) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}
