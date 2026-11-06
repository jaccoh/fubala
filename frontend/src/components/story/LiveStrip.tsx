import type { SlotState, StoryStatus } from "@/types";

/** Slot-state chip: label + colors, shared by the live strip and the board
 *  card. */
export const SLOT_STATES: Record<SlotState, { label: string; chip: string }> = {
  working: { label: "working", chip: "bg-emerald-950/80 text-emerald-300" },
  waiting_tests: { label: "waiting on tests", chip: "bg-amber-950/80 text-amber-300" },
  waiting_slot: { label: "waiting on slot", chip: "bg-zinc-800 text-zinc-400" },
};

export function SlotChip({ slotState }: { slotState: SlotState | null | undefined }) {
  const slot = slotState ? SLOT_STATES[slotState] : undefined;
  if (!slot) return null;
  return (
    <span
      className={`shrink-0 rounded px-1 text-[10px] ${slot.chip}`}
      title="slot state: working = generating, waiting on tests = verify, waiting on slot = queued for a model slot"
    >
      {slot.label}
    </span>
  );
}

/**
 * Live-strip stage map: label + colors for the statuses where an agent is
 * actively working. Colors mirror the pulsing spans in the actions row and
 * the board columns (sky = spec, fuchsia = AI review, orange = coder).
 */
const LIVE_STAGES: Partial<
  Record<StoryStatus, { label: string; dot: string; text: string }>
> = {
  in_progress: { label: "generating spec", dot: "bg-sky-400", text: "text-sky-300" },
  ai_review: { label: "AI review", dot: "bg-fuchsia-400", text: "text-fuchsia-300" },
  executing: { label: "coder executing", dot: "bg-orange-400", text: "text-orange-300" },
};

/** Matches tool lines streamed as `story_log` (backend `tool_log_line`). */
const TOOL_LINE_RE = /🔧\s*(.+?)\((.*)\)\s*→/;

/**
 * Last 🔧 tool call in a log tail, scanned bottom-up; `null` when the log
 * has no tool line yet (e.g. right after a status transition wipes it).
 * Pure so it is unit-testable without rendering the panel.
 */
export function extractLastTool(log: string): { name: string; args: string } | null {
  const lines = log.split("\n");
  for (let i = lines.length - 1; i >= 0; i--) {
    const m = TOOL_LINE_RE.exec(lines[i]);
    if (m) {
      const args = m[2].length > 48 ? `${m[2].slice(0, 48)}…` : m[2];
      return { name: m[1], args };
    }
  }
  return null;
}

/**
 * Prominent live strip shown while an agent works this story, pinned above
 * the scrollable body so it stays on screen (and above the fold) even when
 * the reader has scrolled deep into spec/diff. Renders dot + label only
 * when the log is empty (freshly transitioned stage) — never assumes lines
 * or a tool chip exist.
 */
export function LiveStrip({
  status,
  execution,
  log,
  slotState,
}: {
  status: StoryStatus;
  execution: string;
  log: string;
  slotState?: SlotState | null;
}) {
  if (status === "in_progress" && execution === "external") return null;
  const stage = LIVE_STAGES[status];
  if (!stage) return null;
  const tail = log
    .split("\n")
    .filter((l) => l.trim())
    .slice(-3);
  const tool = extractLastTool(log);
  return (
    <div className="shrink-0 border-b border-zinc-800/70 bg-zinc-900/60 px-5 py-2.5">
      <div className="flex items-center gap-2">
        <span className={`h-2 w-2 animate-pulse rounded-full ${stage.dot}`} />
        <span className={`text-xs font-medium ${stage.text}`}>{stage.label}</span>
        <SlotChip slotState={slotState} />
        <span className="truncate font-mono text-[10px] text-zinc-600">live</span>
      </div>
      {tail.length > 0 && (
        <div className="mt-1.5 space-y-0.5 font-mono text-[11px] leading-tight text-zinc-400">
          {tail.map((line, i) => (
            <div key={`${i}-${line}`} className="truncate whitespace-nowrap">
              {line}
            </div>
          ))}
        </div>
      )}
      {tool && (
        <div className="mt-1.5 flex">
          <span
            title={`🔧 ${tool.name}(${tool.args})`}
            className="inline-flex max-w-full items-center overflow-hidden rounded-md border border-zinc-700/70 bg-zinc-950 px-2 py-0.5 font-mono text-[10px] text-zinc-300"
          >
            <span className="truncate whitespace-nowrap">
              🔧 {tool.name}({tool.args})
            </span>
          </span>
        </div>
      )}
    </div>
  );
}
