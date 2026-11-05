import { useState } from "react";

import type { Epic, Project, Story, StoryGate } from "@/types";

/** Per-story tool budget UI bound (mirrors the API: ge=1, le=160). */
const BUDGET_MIN = 1;
const BUDGET_MAX = 160;

/** Epic picker + dependency chips, shown for backlog/queued/failed stories. */
export function StoryForm({
  live,
  epics,
  stories,
  projects,
  patchStory,
}: {
  live: Story;
  epics: Epic[];
  stories: Story[];
  projects: Project[];
  patchStory: (
    id: string,
    patch: Omit<Partial<Story>, "execution" | "max_tool_calls"> & {
      /** null clears the override so the story inherits Project.executor. */
      execution?: Story["execution"] | null;
      /** null clears the override so the coder inherits the app default. */
      max_tool_calls?: number | null;
    },
  ) => void;
}) {
  /** Draft of the tool-budget input; null = show the stored value. */
  const [budgetDraft, setBudgetDraft] = useState<string | null>(null);
  const budgetValue = budgetDraft ?? live.max_tool_calls?.toString() ?? "";
  const commitBudget = () => {
    if (budgetDraft === null) return;
    setBudgetDraft(null);
    const trimmed = budgetDraft.trim();
    if (trimmed === "") {
      // empty = clear the override, back to the app's tools_max_calls
      patchStory(live.id, { max_tool_calls: null });
      return;
    }
    const n = Number(trimmed);
    if (!Number.isInteger(n) || n < BUDGET_MIN || n > BUDGET_MAX) {
      alert(`Tool budget must be a whole number between ${BUDGET_MIN} and ${BUDGET_MAX}`);
      return;
    }
    patchStory(live.id, { max_tool_calls: n });
  };
  /** Effective executor of this story's project, for the inherit label. */
  const projectExecutor =
    projects.find((p) => p.id === live.project_id)?.executor ?? "internal";
  /** A human rejection is permanent: the gate selectors go read-only so the
   *  UI never promises an auto gate that can no longer pass. */
  const gatesLocked = live.human_rejected === true;
  const gateTitle = (which: "spec" | "code") =>
    gatesLocked
      ? "a human rejected this story at some gate — the auto gates are permanently off for it"
      : `${which} gate: human = you approve at the gate. auto = machine checks only (verify green, AI review approved, no protected/frozen paths, diff within the project cap, and no human rejection ever recorded); any failed check parks the story at the human gate with the failed checks named in a note.`;
  const gateSelect = (
    which: "spec" | "code",
    value: StoryGate,
    onPick: (next: StoryGate) => void,
  ) => (
    <select
      value={value}
      onChange={(e) => onPick(e.target.value as StoryGate)}
      disabled={gatesLocked}
      title={gateTitle(which)}
      className="flex-1 rounded-lg border border-zinc-700/70 bg-zinc-900 px-2 py-1.5 text-xs text-zinc-200 outline-none focus:border-zinc-500 disabled:cursor-not-allowed disabled:opacity-60"
    >
      <option value="human">human (default)</option>
      <option value="auto">auto (machine checks)</option>
    </select>
  );
  return (
    <section className="space-y-2">
      <div className="flex items-center gap-2">
        <span className="w-16 shrink-0 text-[10px] font-semibold tracking-wide text-zinc-500 uppercase">
          epic
        </span>
        <select
          value={live.epic_id ?? ""}
          onChange={(e) => patchStory(live.id, { epic_id: e.target.value || null })}
          className="flex-1 rounded-lg border border-zinc-700/70 bg-zinc-900 px-2 py-1.5 text-xs text-zinc-200 outline-none focus:border-zinc-500"
        >
          <option value="">none</option>
          {epics.map((e) => (
            <option key={e.id} value={e.id}>
              ◆ {e.title}
            </option>
          ))}
        </select>
      </div>
      <div>
        <span className="mb-1 block text-[10px] font-semibold tracking-wide text-zinc-500 uppercase">
          depends on (must be done before this starts)
        </span>
        <div className="mb-1.5 flex flex-wrap gap-1.5">
          {live.depends_on.map((depId) => {
            const dep = stories.find((s) => s.id === depId);
            return (
              <span
                key={depId}
                className={`flex items-center gap-1 rounded-md border px-2 py-0.5 font-mono text-[10px] ${
                  dep?.status === "done"
                    ? "border-emerald-800/60 text-emerald-300"
                    : "border-amber-800/60 text-amber-300"
                }`}
                title={dep ? `${dep.title} — ${dep.status}` : depId}
              >
                {dep ? dep.title.slice(0, 24) : depId.slice(0, 8)}
                <button
                  onClick={() =>
                    patchStory(live.id, {
                      depends_on: live.depends_on.filter((d) => d !== depId),
                    })
                  }
                  className="text-zinc-500 hover:text-red-400"
                >
                  ✕
                </button>
              </span>
            );
          })}
        </div>
        <select
          value=""
          onChange={(e) =>
            e.target.value &&
            patchStory(live.id, {
              depends_on: [...live.depends_on, e.target.value],
            })
          }
          className="w-full rounded-lg border border-zinc-700/70 bg-zinc-900 px-2 py-1.5 text-xs text-zinc-400 outline-none focus:border-zinc-500"
        >
          <option value="">+ add dependency…</option>
          {stories
            .filter((s) => s.id !== live.id && !live.depends_on.includes(s.id))
            .map((s) => (
              <option key={s.id} value={s.id}>
                {s.title} ({s.status})
              </option>
            ))}
        </select>
      </div>
      <div className="flex items-center gap-2">
        <span className="w-16 shrink-0 text-[10px] font-semibold tracking-wide text-zinc-500 uppercase">
          run by
        </span>
        <select
          value={live.execution_override ?? "inherit"}
          onChange={(e) => {
            const next = e.target.value;
            patchStory(live.id, {
              execution: next === "inherit" ? null : (next as Story["execution"]),
            });
          }}
          className="flex-1 rounded-lg border border-zinc-700/70 bg-zinc-900 px-2 py-1.5 text-xs text-zinc-200 outline-none focus:border-zinc-500"
          title="inherit = follow this story's project executor (Projects page). internal / external / headless are explicit per-story overrides."
        >
          <option value="inherit">
            inherit{projectExecutor ? ` (project: ${projectExecutor})` : " (project default)"}
          </option>
          <option value="internal">internal (Fubala runner)</option>
          <option value="external">external (outside agent)</option>
          <option value="headless">headless (Claude Code CLI)</option>
        </select>
      </div>
      <div className="flex items-center gap-2">
        <span className="w-16 shrink-0 text-[10px] font-semibold tracking-wide text-zinc-500 uppercase">
          budget
        </span>
        <input
          type="number"
          min={BUDGET_MIN}
          max={BUDGET_MAX}
          value={budgetValue}
          onChange={(e) => setBudgetDraft(e.target.value)}
          onBlur={commitBudget}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              commitBudget();
            }
          }}
          placeholder="app default"
          className="flex-1 rounded-lg border border-zinc-700/70 bg-zinc-900 px-2 py-1.5 text-xs text-zinc-200 outline-none focus:border-zinc-500"
          title="Per-story tool-call budget for the coder run. Empty = the app's Max tool calls setting applies. Big stories (large refactors, long test cycles) may need more than the default."
        />
        <span className="text-[10px] text-zinc-500">
          tool calls ({BUDGET_MIN}–{BUDGET_MAX}; empty = app default)
        </span>
      </div>
      <div className="flex items-center gap-2">
        <span className="w-16 shrink-0 text-[10px] font-semibold tracking-wide text-zinc-500 uppercase">
          spec gate
        </span>
        {gateSelect("spec", live.gate_spec ?? "human", (next) =>
          patchStory(live.id, { gate_spec: next }),
        )}
        {gatesLocked && (
          <span className="text-[10px] text-amber-400">human-rejected</span>
        )}
      </div>
      <div className="flex items-center gap-2">
        <span className="w-16 shrink-0 text-[10px] font-semibold tracking-wide text-zinc-500 uppercase">
          code gate
        </span>
        {gateSelect("code", live.gate_code ?? "human", (next) =>
          patchStory(live.id, { gate_code: next }),
        )}
        {gatesLocked && (
          <span className="text-[10px] text-amber-400">human-rejected</span>
        )}
      </div>
    </section>
  );
}
