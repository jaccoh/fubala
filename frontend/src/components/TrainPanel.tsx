import { useEffect, useState } from "react";

import { api } from "@/api";
import { useStore } from "@/store";
import type { Project, TrainBatch } from "@/types";

/** The state badge colors for a train batch. */
const STATE_STYLES: Record<string, string> = {
  forming: "bg-amber-950/70 text-amber-300",
  open: "bg-sky-950/70 text-sky-300",
  merged: "bg-emerald-950/80 text-emerald-300",
  red: "bg-red-950/70 text-red-300",
};

const STATE_HINTS: Record<string, string> = {
  forming: "members are joining; the batch flushes on its window or size cap",
  open: "the batch branch is pushed and its pull request is running CI",
  merged: "the batch PR merged; members were judged against main",
  red: "red (tests failed, or failed twice): the members were bisected into child batches (or bounced)",
};

/**
 * Which batches the panel renders. A long-lived train accumulates merged
 * batches fast; the board only needs the live ones (plus anything that
 * explains a bisect), so settled merged batches fall away once the history
 * grows past a handful.
 */
export function visibleBatches(batches: TrainBatch[]): TrainBatch[] {
  return batches.filter(
    (b) => b.state !== "merged" || b.red_runs > 0 || batches.length <= 3,
  );
}

/** The failing pytest ids of a red batch: monospace, truncated, full id in the tooltip. */
export function FailedTests({ ids }: { ids: string[] }) {
  if (ids.length === 0) return null;
  return (
    <ul className="basis-full space-y-0.5">
      {ids.map((id) => (
        <li
          key={id}
          title={id}
          className="truncate font-mono text-[10px] text-rose-300"
        >
          {id}
        </li>
      ))}
    </ul>
  );
}

/**
 * The merge-train panel: this project's batches — member stories, the batch
 * PR link, and the batch state. Rendered on the board when a project filter
 * is active and that project runs a train; no separate page.
 */
export function TrainPanel({ project }: { project: Project }) {
  const [batches, setBatches] = useState<TrainBatch[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!project.merge_train) return;
    let alive = true;
    const load = async () => {
      try {
        const next = await api.projectTrain(project.id);
        if (alive) {
          setBatches(next);
          setError(null);
        }
      } catch (e) {
        if (alive) setError(String(e));
      }
    };
    load();
    // the train advances on every scheduler tick; a slow poll keeps the
    // panel honest without adding an SSE channel for it
    const timer = window.setInterval(load, 5000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [project.id, project.merge_train]);

  if (!project.merge_train) return null;

  const interesting = visibleBatches(batches ?? []);

  return (
    <div className="mx-4 mb-3 rounded-xl border border-zinc-800/80 bg-zinc-900/40 p-3">
      <div className="flex items-center gap-2">
        <h2 className="text-xs font-semibold tracking-wide text-zinc-400 uppercase">
          Merge train
        </h2>
        <span className="text-[10px] text-zinc-600">
          {project.train_window_s}s window · max {project.train_max}
        </span>
      </div>
      {project.train_paused && (
        <p className="mt-2 text-[11px] text-amber-400">
          Train paused by {project.train_paused_by}: {project.train_pause_reason}
        </p>
      )}
      {error && <p className="mt-2 text-[11px] text-red-400">{error}</p>}
      {!error && batches === null && (
        <p className="mt-2 text-[11px] text-zinc-600">loading…</p>
      )}
      {batches !== null && batches.length === 0 && (
        <p className="mt-2 text-[11px] text-zinc-600">
          no batches yet — approved stories join the next one
        </p>
      )}
      <div className="mt-2 space-y-1.5">
        {interesting.map((batch) => (
          <div
            key={batch.id}
            className="flex flex-wrap items-center gap-2 rounded-lg border border-zinc-800/60 bg-zinc-950/40 px-2.5 py-1.5"
          >
            <span
              className={`rounded-full px-2 py-0.5 text-[10px] font-medium ${
                STATE_STYLES[batch.state] ?? "bg-zinc-800 text-zinc-400"
              }`}
              title={STATE_HINTS[batch.state] ?? ""}
            >
              {batch.state}
            </span>
            {batch.pr_url ? (
              <a
                href={batch.pr_url}
                target="_blank"
                rel="noreferrer"
                className="text-[11px] text-sky-400 underline decoration-dotted hover:text-sky-300"
              >
                batch PR #{batch.pr_index ?? "?"}
              </a>
            ) : (
              <span className="text-[11px] text-zinc-600">no PR yet</span>
            )}
            {batch.red_runs > 0 && (
              <span
                className="text-[10px] text-amber-400"
                title="confirmed reds on the judged head (one re-run is allowed per level)"
              >
                {batch.red_runs} red
              </span>
            )}
            {batch.parent_batch_id && (
              <span
                className="text-[10px] text-zinc-600"
                title={`bisected from batch ${batch.parent_batch_id}`}
              >
                ↳ bisect
              </span>
            )}
            <span className="ml-auto flex flex-wrap items-center gap-1">
              {batch.members.map((m) => (
                <button
                  key={m.id}
                  onClick={() => useStore.getState().openStory(m.id)}
                  title={`${m.title ?? m.id} — ${m.status ?? "?"}`}
                  className="max-w-40 truncate rounded bg-zinc-800/80 px-1.5 py-0.5 text-[10px] text-zinc-300 transition hover:bg-zinc-700"
                >
                  {m.title ?? m.id}
                </button>
              ))}
            </span>
            <FailedTests ids={batch.failed_tests ?? []} />
          </div>
        ))}
      </div>
    </div>
  );
}
