import { useEffect, useState } from "react";

import { useStore } from "@/store";
import type { RunningTool } from "@/types";

function elapsedSeconds(tool: RunningTool, now: number): number {
  const started = Date.parse(tool.started_at);
  if (Number.isNaN(started)) return 0;
  return Math.max(0, Math.floor((now - started) / 1000));
}

/** `running: run_verify, 4 min 12 s`; minutes are left out when 0. */
export function runningToolLabel(tool: RunningTool, now: number): string {
  const total = elapsedSeconds(tool, now);
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `running: ${tool.name}, ${m > 0 ? `${m} min ` : ""}${s} s`;
}

/** Slow: running for longer than half of the tool's own timeout. */
export function isToolSlow(tool: RunningTool, now: number): boolean {
  return elapsedSeconds(tool, now) > tool.timeout_s / 2;
}

export function RunningToolBadge({
  storyId,
  compact,
}: {
  storyId: string;
  compact?: boolean;
}) {
  const tools = useStore((s) => s.storyRunningTools[storyId]);
  const [now, setNow] = useState(() => Date.now());
  const active = !!tools && tools.length > 0;
  useEffect(() => {
    if (!active) return;
    setNow(Date.now());
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [active]);
  return <RunningToolList tools={tools ?? []} compact={compact} now={now} />;
}

/** Presentational part of the badge; `null` when no tool is open. */
export function RunningToolList({
  tools,
  compact,
  now,
}: {
  tools: RunningTool[];
  compact?: boolean;
  now: number;
}) {
  if (tools.length === 0) return null;
  const shown = compact ? tools.slice(0, 1) : tools;
  return (
    <div className="flex flex-col gap-0.5">
      {shown.map((t) => (
        <span
          key={t.call_id}
          className={`truncate font-mono text-[10px] ${
            isToolSlow(t, now) ? "text-amber-300" : "text-zinc-400"
          }`}
        >
          {runningToolLabel(t, now)}
          {compact && tools.length > 1 ? ` +${tools.length - 1}` : ""}
        </span>
      ))}
    </div>
  );
}
