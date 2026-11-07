import type { AiVerdict, StoryStatus } from "@/types";

/**
 * Board column model — extracted from BoardView so the status → column
 * assignment is a pure, unit-testable function.
 *
 * The mapping MUST stay status-only: `execution`, `claimed_by`, `ai_verdict`,
 * … are card badges, never column inputs. A story always renders in exactly
 * one column: the one implied by its `status` field (the board's own
 * `GET /api/v1/stories` is authoritative).
 */
export interface BoardColumn {
  status: StoryStatus;
  label: string;
  dot: string;
}

export const COLUMNS: BoardColumn[] = [
  { status: "backlog", label: "Backlog", dot: "bg-zinc-500" },
  { status: "queued", label: "Queue", dot: "bg-amber-400" },
  { status: "in_progress", label: "In progress", dot: "bg-sky-400" },
  { status: "ai_review", label: "AI review", dot: "bg-fuchsia-400" },
  { status: "executing", label: "Executing", dot: "bg-orange-400" },
  { status: "human_review", label: "Human review", dot: "bg-violet-400" },
  { status: "landing", label: "Landing", dot: "bg-teal-400" },
  { status: "done", label: "Done", dot: "bg-emerald-400" },
  { status: "failed", label: "Failed", dot: "bg-red-400" },
  // Terminal close of a failed story: its work was made irrelevant by
  // another. Kept OUT of the failed lane (that one holds only actionable
  // failures) and rendered collapsed — an archive, not a work queue.
  { status: "superseded", label: "Superseded", dot: "bg-zinc-700" },
];

/** Columns that render as a collapsed strip until the operator expands it. */
export const COLLAPSIBLE_COLUMNS: readonly StoryStatus[] = ["superseded"];

/**
 * True when the lane renders collapsed. Collapsed lanes still hold their
 * cards — the strip expands on click, so no story is ever unreachable —
 * but the default board is not dominated by dead work.
 */
export function isCollapsedLane(status: StoryStatus): boolean {
  return COLLAPSIBLE_COLUMNS.includes(status);
}

/** The column a story belongs to — derived from `status` and nothing else. */
export function columnForStatus(status: StoryStatus): BoardColumn {
  return COLUMNS.find((c) => c.status === status) ?? COLUMNS[0];
}

/**
 * True when `story` renders inside the column for `status`.
 *
 * BoardView buckets cards with this predicate (previously an inline
 * `visible.filter((s) => s.status === status)`); keeping it next to
 * `columnForStatus` makes the "status-only" contract impossible to drift.
 */
export function belongsToColumn(
  story: { status: StoryStatus },
  status: StoryStatus,
): boolean {
  return story.status === status;
}

export interface VerdictBadge {
  label: string;
  tone: string;
}

const VERDICT_BADGES: Record<AiVerdict, VerdictBadge> = {
  approved: { label: "✓ approved", tone: "bg-emerald-950/80 text-emerald-300" },
  rejected: { label: "✗ rejected", tone: "bg-red-950/80 text-red-300" },
  incomplete: { label: "⚠ incomplete", tone: "bg-amber-950/80 text-amber-300" },
};

/** Badge for the story detail and outcome panels; null without a known verdict. */
export function verdictBadge(verdict: string | null | undefined): VerdictBadge | null {
  return verdict && verdict in VERDICT_BADGES ? VERDICT_BADGES[verdict as AiVerdict] : null;
}

/** Board card chip: only the verdicts that need a human look (not approved). */
export function cardVerdictChip(verdict: string | null | undefined): VerdictBadge | null {
  if (verdict === "rejected") return { label: "AI rejected", tone: VERDICT_BADGES.rejected.tone };
  if (verdict === "incomplete") {
    return { label: "AI incomplete", tone: VERDICT_BADGES.incomplete.tone };
  }
  return null;
}
