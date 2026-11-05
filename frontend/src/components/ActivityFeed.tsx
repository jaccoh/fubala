import { useEffect, useRef } from "react";

import type { StoryActivityEvent } from "@/types";

function ActivityRow({ event }: { event: StoryActivityEvent }) {
  if (event.kind === "round") {
    const p = event.data;
    return (
      <li className="flex gap-2 font-mono text-[11px] text-zinc-500">
        <span className="shrink-0 text-sky-400/70">── round {p.round}</span>
        <span className="text-zinc-600">
          tools {p.tool_calls}
          {p.max_tool_calls ? `/${p.max_tool_calls}` : ""}
          {typeof p.elapsed_s === "number" ? ` · ${Math.round(p.elapsed_s)}s` : ""}
          {p.prompt_tokens || p.completion_tokens
            ? ` · ${p.prompt_tokens + p.completion_tokens} tok`
            : ""
          }
          {p.phase ? ` · ${p.phase}` : ""}
        </span>
      </li>
    );
  }
  if (event.kind === "tool_call") {
    const d = event.data as { name?: string; args?: string; path?: string; command?: string };
    const where = d.path || d.command || "";
    return (
      <li className="flex min-w-0 gap-2 font-mono text-[11px]">
        <span className="shrink-0 text-zinc-600">🔧</span>
        <span className="shrink-0 text-zinc-400">{d.name || "tool"}</span>
        {where && (
          <span className="min-w-0 truncate text-zinc-500" title={where}>
            {where}
          </span>
        )}
        {!where && d.args && (
          <span className="min-w-0 truncate text-zinc-600" title={d.args}>
            {d.args}
          </span>
        )}
      </li>
    );
  }
  if (event.kind === "headless_stream") {
    const d = event.data;
    if (d.type === "assistant") {
      return (
        <li className="flex min-w-0 gap-2 font-mono text-[11px]">
          <span className="shrink-0 text-zinc-600">▸</span>
          <span className="min-w-0 truncate whitespace-pre-wrap text-zinc-400" title={d.text}>
            {d.text}
          </span>
        </li>
      );
    }
    if (d.type === "tool_use") {
      return (
        <li className="flex min-w-0 gap-2 font-mono text-[11px]">
          <span className="shrink-0 text-zinc-600">🔧</span>
          <span className="shrink-0 text-zinc-400">{d.tool}</span>
          <span className="min-w-0 truncate text-zinc-500" title={d.input}>
            {d.input}
          </span>
        </li>
      );
    }
    return (
      <li className="flex gap-2 pl-4 font-mono text-[11px]">
        <span className={d.status === "error" ? "text-red-400" : "text-zinc-600"}>
          ↳ {d.status}
          {typeof d.chars === "number" ? ` · ${d.chars} chars` : ""}
        </span>
      </li>
    );
  }
  const r = event.data as { status?: string; chars?: number; error?: string };
  const color =
    r.status === "error" ? "text-red-400" : r.status === "truncated" ? "text-amber-400" : "text-zinc-600";
  return (
    <li className="flex gap-2 pl-4 font-mono text-[11px]">
      <span className={color}>
        ↳ {r.status ?? "ok"}
        {typeof r.chars === "number" ? ` · ${r.chars} chars` : ""}
      </span>
      {r.error && <span className="min-w-0 truncate text-red-400/70" title={r.error}>{r.error}</span>}
    </li>
  );
}

export function ActivityFeed({ events }: { events: StoryActivityEvent[] }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    ref.current?.scrollTo({ top: ref.current.scrollHeight });
  }, [events.length]);
  return (
    <div ref={ref} className="max-h-56 overflow-y-auto rounded-lg border border-zinc-800 bg-zinc-900/80 p-3">
      {events.length === 0 ? (
        <p className="font-mono text-[11px] text-zinc-600">waiting for agent activity…</p>
      ) : (
        <ul className="space-y-1">
          {events.map((e, i) => (
            <ActivityRow key={i} event={e} />
          ))}
        </ul>
      )}
    </div>
  );
}
