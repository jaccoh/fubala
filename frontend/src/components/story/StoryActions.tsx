import { useState } from "react";

import type { Story } from "@/types";

/** Story action endpoint names the action row / reject box can invoke. */
export type StoryActionName =
  | "queue"
  | "unqueue"
  | "approve"
  | "reject"
  | "discard"
  | "claim"
  | "complete"
  | "release";

/** JSON body for a story action; `max_tool_calls` is spec-gate (approve) only. */
export type StoryActionBody = Record<string, string | number>;

/** Per-story tool budget UI bound (mirrors the API: ge=1, le=160). */
export const BUDGET_MIN = 1;
export const BUDGET_MAX = 160;

/** Pulsing "agent at work" badge used between the action buttons. */
function PulsingStage({ dot, text, children }: { dot: string; text: string; children: string }) {
  return (
    <span className={`flex items-center gap-2 text-xs ${text}`}>
      <span className={`h-2 w-2 animate-pulse rounded-full ${dot}`} />
      {children}
    </span>
  );
}

/** Status-dependent action row: queue/approve/reject/retry/release controls. */
export function StoryActions({
  live,
  rejecting,
  onReject,
  run,
  onDelete,
  onSupersede,
  onRetryLanding,
}: {
  live: Story;
  rejecting: boolean;
  onReject: () => void;
  run: (action: StoryActionName, body?: StoryActionBody) => Promise<void>;
  onDelete: (id: string) => void;
  onSupersede: () => void;
  onRetryLanding?: () => void;
}) {
  /** Draft of the spec-gate tool-budget bump; null = input idle. */
  const [budgetDraft, setBudgetDraft] = useState<string | null>(null);
  /** True once the operator typed a budget to send with this approve. */
  const budgetActive = budgetDraft !== null && budgetDraft.trim() !== "";
  const approve = () => {
    if (budgetActive) {
      const n = Number(budgetDraft.trim());
      if (!Number.isInteger(n) || n < BUDGET_MIN || n > BUDGET_MAX) {
        alert(`Tool budget must be a whole number between ${BUDGET_MIN} and ${BUDGET_MAX}`);
        return;
      }
      void run("approve", { by: "local", max_tool_calls: n });
    } else {
      void run("approve", { by: "local" });
    }
    setBudgetDraft(null);
  };
  return (
    <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-zinc-800/70 px-5 py-3">
      {live.status === "backlog" && (
        <>
          <button
            onClick={() => run("queue")}
            className="rounded-lg bg-sky-600 px-3 py-1.5 text-xs font-medium text-white transition hover:bg-sky-500"
          >
            Queue {live.execution === "external" ? "for external claim" : "for spec"}
          </button>
          <button
            onClick={() =>
              confirm(
                `Delete "${live.title}" permanently?\n\nThis removes the story and its history — it cannot be undone. Use "Discard changes" instead if you only want to drop the code.`,
              ) && onDelete(live.id)
            }
            className="rounded-lg px-3 py-1.5 text-xs text-zinc-400 transition hover:text-red-400"
          >
            Delete
          </button>
        </>
      )}
      {live.status === "queued" && (
        <button
          onClick={() => run("unqueue")}
          className="rounded-lg border border-zinc-700 px-3 py-1.5 text-xs text-zinc-300 transition hover:bg-zinc-800"
        >
          Unqueue
        </button>
      )}
      {live.status === "in_progress" && live.execution === "external" && (
        <>
          <span className="flex items-center gap-2 text-xs text-teal-300">
            <span className="h-2 w-2 animate-pulse rounded-full bg-teal-400" />
            {live.claimed_by
              ? `${live.claimed_by} is working this story externally`
              : "claimed externally"}
          </span>
          {live.claimed_by && (
            <button
              onClick={() =>
                confirm(
                  `Release the claim held by "${live.claimed_by}"?\n\nThe story goes back to queued and can be claimed again — use this when the claiming agent is gone (stale claim).`,
                ) && run("release")
              }
              className="rounded-lg border border-zinc-700 px-3 py-1.5 text-xs text-zinc-400 transition hover:text-zinc-200"
              title="Stale-claim escape: drop the claim and requeue the story"
            >
              Release claim
            </button>
          )}
        </>
      )}
      {live.status === "in_progress" && live.execution !== "external" && (
        <PulsingStage dot="bg-sky-400" text="text-sky-300">
          generating spec…
        </PulsingStage>
      )}
      {live.status === "ai_review" && (
        <PulsingStage dot="bg-fuchsia-400" text="text-fuchsia-300">
          AI reviewer at work…
        </PulsingStage>
      )}
      {live.status === "executing" && live.execution !== "external" && (
        <PulsingStage dot="bg-orange-400" text="text-orange-300">
          coder agent executing in worktree…
        </PulsingStage>
      )}
      {live.status === "human_review" && !rejecting && (
        <>
          <input
            type="number"
            min={BUDGET_MIN}
            max={BUDGET_MAX}
            value={budgetDraft ?? ""}
            onChange={(e) => setBudgetDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                approve();
              }
            }}
            placeholder={live.max_tool_calls ? String(live.max_tool_calls) : "budget"}
            className={`w-20 rounded-lg border bg-zinc-900 px-2 py-1.5 font-mono text-xs outline-none ${
              budgetActive
                ? "border-sky-500 text-zinc-100"
                : "border-zinc-700/70 text-zinc-400"
            }`}
            title="Per-run tool-call budget for the coder phase, applied when you approve. Only at this gate (spec): the code gate has executed work, its budget is locked. Empty = keep the stored budget (app default when none)."
          />
          <button
            onClick={approve}
            disabled={
              // {"applicable": false} (info/prep) has no `passed` — checked
              // first so it never reads as a failure
              live.verify_result?.applicable !== false &&
              live.execution !== "external" &&
              !!live.verify_result &&
              !live.verify_result.passed
            }
            className="rounded-lg bg-emerald-600 px-3 py-1.5 text-xs font-medium text-white transition hover:bg-emerald-500 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {live.execution === "external"
              ? "✓ Approve (code landed externally)"
              : live.kind && live.kind !== "code"
                ? "✓ Approve & finish"
                : live.base_ref
                  ? live.verify_result?.applicable === false
                    ? "✓ Approve"
                    : live.verify_result && !live.verify_result.passed
                      ? "✗ verify failed — merge blocked"
                      : "✓ Approve & merge"
                  : "✓ Approve → execute"}
          </button>
          <button
            onClick={onReject}
            className="rounded-lg border border-zinc-700 px-3 py-1.5 text-xs text-zinc-300 transition hover:bg-zinc-800"
          >
            ⟲ Reject & requeue
          </button>
          {live.base_ref && live.execution !== "external" && (
            <button
              onClick={() => confirm("Discard the executed changes (worktree deleted)?") && run("discard")}
              className="rounded-lg px-3 py-1.5 text-xs text-zinc-500 transition hover:text-red-400"
            >
              Discard changes
            </button>
          )}
        </>
      )}
      {live.status === "failed" && (
        <>
          <button
            onClick={() => run("queue")}
            className="rounded-lg bg-amber-600 px-3 py-1.5 text-xs font-medium text-white transition hover:bg-amber-500"
          >
            Retry
          </button>
          {live.pr_url && onRetryLanding && (
            <button
              onClick={onRetryLanding}
              className="rounded-lg border border-sky-700 px-3 py-1.5 text-xs text-sky-300 transition hover:bg-sky-950"
              title="The pull request is still open: push the branch and watch it again instead of re-running the story. Backend 409s when the story has no pull request."
            >
              Re-arm landing
            </button>
          )}
          <button
            onClick={() => onSupersede()}
            className="rounded-lg border border-zinc-700 px-3 py-1.5 text-xs text-zinc-300 transition hover:bg-zinc-800"
            title="Close this failed story because another story (or an outside change) made its work irrelevant. It moves to the Superseded lane, out of Failed."
          >
            Close as superseded…
          </button>
        </>
      )}
    </div>
  );
}
