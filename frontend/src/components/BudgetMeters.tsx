import type { StoryProgress } from "@/types";

/** ok / near-limit (>=80%) / exhausted (>=100%) — colour-coded, never gates anything. */
export type MeterState = "ok" | "near" | "exhausted";

function meterState(used: number, max: number | null | undefined): MeterState {
  if (max === null || max === undefined || max <= 0) return "ok";
  const ratio = used / max;
  if (ratio >= 1) return "exhausted";
  if (ratio >= 0.8) return "near";
  return "ok";
}

const STATE_TEXT: Record<MeterState, string> = {
  ok: "text-zinc-400",
  near: "text-amber-300",
  exhausted: "text-red-300",
};
const STATE_BAR: Record<MeterState, string> = {
  ok: "bg-zinc-500",
  near: "bg-amber-400",
  exhausted: "bg-red-500",
};

function Meter({
  label,
  used,
  max,
  display,
}: {
  label: string;
  used: number;
  max: number | null;
  display: string;
}) {
  const state = meterState(used, max);
  const pct =
    max && max > 0 ? Math.min(100, Math.round((used / max) * 100)) : 0;
  return (
    <div className="min-w-0 flex-1">
      <div className="mb-1 flex items-baseline justify-between gap-1">
        <span className="text-[9px] font-semibold tracking-wide text-zinc-500 uppercase">
          {label}
        </span>
        <span className={`font-mono text-[10px] ${STATE_TEXT[state]}`}>{display}</span>
      </div>
      <div className="h-1 overflow-hidden rounded-full bg-zinc-800">
        <div className={`h-full ${STATE_BAR[state]}`} style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

function fmtSeconds(s: number): string {
  if (s < 60) return `${Math.round(s)}s`;
  const m = Math.floor(s / 60);
  return `${m}m${String(Math.round(s % 60)).padStart(2, "0")}s`;
}

function fmtTokens(n: number): string {
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
}

/** Per-run budget meters: tool calls, rounds, agent time, tokens. */
export function BudgetMeters({ progress }: { progress: StoryProgress }) {
  const tokens = progress.prompt_tokens + progress.completion_tokens;
  // max tokens is not budgeted by the policy — the bar shows a soft scale so
  // the number is visible without implying a hard cap
  const tokenScale = Math.max(200_000, tokens * 1.2);
  return (
    <div className="flex gap-3 rounded-lg border border-zinc-800/80 bg-zinc-900/40 px-3 py-2">
      <Meter
        label="tools"
        used={progress.tool_calls}
        max={progress.max_tool_calls}
        display={
          progress.max_tool_calls
            ? `${progress.tool_calls}/${progress.max_tool_calls}`
            : `${progress.tool_calls}`
        }
      />
      <Meter
        label="rounds"
        used={progress.round}
        max={progress.max_rounds}
        display={`${progress.round}/${progress.max_rounds}`}
      />
      <Meter
        label="agent time"
        used={progress.elapsed_s}
        max={progress.timeout_s}
        display={
          progress.timeout_s
            ? `${fmtSeconds(progress.elapsed_s)}/${fmtSeconds(progress.timeout_s)}`
            : fmtSeconds(progress.elapsed_s)
        }
      />
      <Meter
        label="tokens"
        used={tokens}
        max={tokenScale}
        display={fmtTokens(tokens)}
      />
    </div>
  );
}

/** Compact board-card badge: tool-call budget state while the story runs. */
export function BudgetBadge({ progress }: { progress: StoryProgress }) {
  const state = meterState(progress.tool_calls, progress.max_tool_calls);
  const cls: Record<MeterState, string> = {
    ok: "bg-zinc-800 text-zinc-400",
    near: "bg-amber-950/80 text-amber-300",
    exhausted: "bg-red-950/80 text-red-300",
  };
  return (
    <span
      className={`shrink-0 rounded px-1 font-mono text-[10px] ${cls[state]}`}
      title={`round ${progress.round}/${progress.max_rounds} · tools ${progress.tool_calls}${
        progress.max_tool_calls ? `/${progress.max_tool_calls}` : ""
      } · ${Math.round(progress.elapsed_s)}s · ${
        progress.prompt_tokens + progress.completion_tokens
      } tokens`}
    >
      r{progress.round} · t{progress.tool_calls}
      {progress.max_tool_calls ? `/${progress.max_tool_calls}` : ""}
    </span>
  );
}
