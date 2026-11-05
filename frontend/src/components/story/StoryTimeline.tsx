import { useState } from "react";

import { useStore } from "@/store";
import type { StoryDetail, StoryEvent } from "@/types";
import { parseServerTime } from "@/utils/parseServerTime";

/**
 * One history row: timestamp + kind + a best-effort detail pulled out of the
 * event's JSON payload (reason / error / status transition).
 */
function EventRow({ event }: { event: StoryEvent }) {
  let detail = "";
  try {
    const parsed = JSON.parse(event.data) as Record<string, unknown>;
    if (typeof parsed.reason === "string") detail = parsed.reason;
    else if (typeof parsed.error === "string") detail = parsed.error;
    else if (parsed.from && parsed.to)
      detail = `${parsed.from} → ${parsed.to}${
        typeof parsed.approved_by === "string" ? ` (by ${parsed.approved_by})` : ""
      }`;
    else if (event.kind === "gate_auto" && Array.isArray(parsed.checks)) {
      // auto-gate verdict: "spec/code gate auto: PASSED" plus the failed
      // checks named (the parking note carries the details).
      const failed = (parsed.checks as { name: string; passed: boolean }[]).filter(
        (c) => !c.passed,
      );
      const gate = typeof parsed.gate === "string" ? parsed.gate : "?";
      detail = failed.length
        ? `${gate} gate auto: held — ${failed.map((c) => c.name).join(", ")}`
        : `${gate} gate auto: passed`;
    }
  } catch {
    /* non-json data */
  }
  return (
    <li className="flex gap-2 text-[11px]">
      <span className="w-16 shrink-0 text-right font-mono text-zinc-600">
        {parseServerTime(event.created_at).toLocaleTimeString([], {
          hour: "2-digit",
          minute: "2-digit",
        })}
      </span>
      <span className="font-mono text-zinc-400">{event.kind}</span>
      {detail && <span className="min-w-0 truncate text-zinc-500">{detail}</span>}
    </li>
  );
}

/** Append-only work log plus the raw event history, from the fetched detail. */
export function StoryNotes({ detail }: { detail: StoryDetail }) {
  const [note, setNote] = useState("");
  return (
    <section>
      <h3 className="mb-2 text-[11px] font-semibold tracking-wide text-zinc-500 uppercase">
        Notes <span className="normal-case">(append-only work log)</span>
      </h3>
      {detail.notes.length > 0 && (
        <div className="mb-2 max-h-56 space-y-1.5 overflow-y-auto">
          {detail.notes.map((n) => (
            <div
              key={n.id}
              className="rounded-lg border border-zinc-800/80 bg-zinc-900/40 px-3 py-2"
            >
              <div className="mb-1 flex items-center gap-2 text-[10px] text-zinc-500">
                <span className="font-medium text-zinc-400">{n.author}</span>
                <span>{parseServerTime(n.created_at).toLocaleString()}</span>
                {n.as_feedback && (
                  <>
                    <span className="rounded bg-amber-500/15 px-1.5 text-amber-400">
                      feedback
                    </span>
                    <span>{n.seen_by_coder ? "seen by coder" : "not yet seen by coder"}</span>
                  </>
                )}
              </div>
              <p className="whitespace-pre-wrap font-mono text-xs text-zinc-300">{n.body}</p>
            </div>
          ))}
        </div>
      )}
      <div className="flex gap-2">
        <input
          value={note}
          onChange={(e) => setNote(e.target.value)}
          onKeyDown={async (e) => {
            if (e.key !== "Enter" || !note.trim()) return;
            const { api: liveApi } = await import("@/api");
            await liveApi.addStoryNote(detail.id, note.trim());
            setNote("");
            const fresh = await liveApi.getStory(detail.id);
            useStore.setState((st) => ({
              stories: st.stories.map((s) =>
                s.id === fresh.id ? { ...s, ...fresh } : s,
              ),
            }));
          }}
          placeholder="Add a note — STATE / VERIFIED / NEXT / BLOCKERS…"
          className="flex-1 rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-1.5 text-xs text-zinc-200 placeholder-zinc-600 outline-none focus:border-zinc-500"
        />
      </div>
    </section>
  );
}

/** Chronological event list for the story's whole life on the board. */
export function StoryHistory({ detail }: { detail: StoryDetail }) {
  return (
    <section>
      <h3 className="mb-2 text-[11px] font-semibold tracking-wide text-zinc-500 uppercase">
        History
      </h3>
      <ul className="space-y-1">
        {detail.events.map((e) => (
          <EventRow key={e.id} event={e} />
        ))}
      </ul>
    </section>
  );
}
