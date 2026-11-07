import { ActivityFeed } from "@/components/ActivityFeed";
import { BudgetMeters } from "@/components/BudgetMeters";
import { RunningToolBadge } from "@/components/RunningTool";
import { DiffViewer } from "@/components/DiffViewer";
import { Markdown } from "@/components/MessageItem";
import { verdictBadge } from "@/components/boardColumns";
import { BADGE, FIELD_LABEL, PANEL } from "@/components/ui";
import { OUTCOME_STATUSES, OutcomeSection } from "@/components/story/OutcomeSection";
import { StoryForm } from "@/components/story/StoryForm";
import { StoryHistory, StoryNotes } from "@/components/story/StoryTimeline";
import type { Epic, Project, Story, StoryActivityEvent, StoryDetail, StoryProgress } from "@/types";

/** Statuses where the story form (epic / dependencies) is editable. */
const EDITABLE_STATUSES = ["backlog", "queued", "failed"];

/** Statuses that show the file-diff section. */
const DIFF_STATUSES = ["executing", "ai_review", "human_review", "done", "failed"];

/** Statuses that show the live log / streaming sections. */
const LIVE_LOG_STATUSES = ["in_progress", "ai_review", "executing"];

/**
 * The scrollable body of the story panel: outcome, diff, description, live
 * log, run budget, AI review, spec, notes, history and activity. Sits below
 * the fixed header / actions / live strip.
 */
export function StoryBody({
  live,
  detail,
  epics,
  stories,
  projects,
  patchStory,
  log,
  activity,
  progress,
}: {
  /** SSE-fresh story; its status drives which sections render. */
  live: Story;
  /** Fetched detail, only when it refers to this very story. */
  detail: StoryDetail | null;
  epics: Epic[];
  stories: Story[];
  projects: Project[];
  patchStory: (
    id: string,
    patch: Omit<Partial<Story>, "execution"> & {
      execution?: Story["execution"] | null;
    },
  ) => void;
  log: string;
  activity: StoryActivityEvent[];
  progress: StoryProgress | undefined;
}) {
  // Outcome-first layout for finished stories. `live` comes from SSE and has
  // no `evidence`/`code_diff` — only the fetched detail does.
  const isOutcome = OUTCOME_STATUSES.includes(live.status);
  const outcomeVerify = live.verify_result ?? detail?.evidence?.verify ?? null;
  const fallbackDiff = detail?.code_diff || detail?.evidence?.code_diff || null;
  /** Done with nothing to show for it: no endpoint diff, no code_diff and no
   * evidence at all — explicit empty state instead of a bare "no changes". */
  const noEvidence =
    live.status === "done" &&
    detail !== null &&
    !detail.code_diff &&
    !(detail.evidence?.code_diff || detail.evidence?.summary || detail.evidence?.verify);
  const aiBadge = verdictBadge(live.ai_verdict);
  const showActivity = LIVE_LOG_STATUSES.includes(live.status);
  const running = live.status === "in_progress" || live.status === "executing";

  return (
    <div className="min-h-0 flex-1 space-y-5 overflow-y-auto px-5 py-4">
      {live.status === "failed" && live.error && (
        <div className="rounded-lg border border-red-900/60 bg-red-950/40 p-3 text-xs text-red-300">
          {live.error}
        </div>
      )}

      {EDITABLE_STATUSES.includes(live.status) && (
        <StoryForm
          live={live}
          epics={epics}
          stories={stories}
          projects={projects}
          patchStory={patchStory}
        />
      )}

      {isOutcome && <OutcomeSection live={live} detail={detail} verify={outcomeVerify} />}

      {DIFF_STATUSES.includes(live.status) && (
        <section>
          <h3 className={`mb-2 ${FIELD_LABEL}`}>
            Changes <span className="normal-case">(file diff)</span>
          </h3>
          {noEvidence ? (
            <div className="rounded-lg border border-zinc-800/80 bg-zinc-900/40 p-3 font-mono text-[11px] text-zinc-500">
              {live.kind && live.kind !== "code"
                ? `no code deliverable (${live.kind} story)`
                : "no evidence submitted"}
            </div>
          ) : (
            <DiffViewer
              storyId={live.id}
              executing={live.status === "executing"}
              hasBaseRef={Boolean(live.base_ref)}
              fallbackDiff={fallbackDiff}
            />
          )}
        </section>
      )}

      {(live.description || live.acceptance) && (
        <section className="space-y-3">
          {live.description && (
            <div>
              <h3 className={`mb-1 ${FIELD_LABEL}`}>Description</h3>
              <p className="text-sm whitespace-pre-wrap text-zinc-300">{live.description}</p>
            </div>
          )}
          {live.acceptance && (
            <div>
              <h3 className={`mb-1 ${FIELD_LABEL}`}>Acceptance criteria</h3>
              <p className="text-sm whitespace-pre-wrap text-zinc-300">{live.acceptance}</p>
            </div>
          )}
        </section>
      )}

      {LIVE_LOG_STATUSES.includes(live.status) && (
        <section>
          <h3 className="mb-1 text-[11px] font-semibold tracking-wide text-sky-400 uppercase">
            Live log
          </h3>
          <pre className="max-h-64 overflow-y-auto rounded-lg border border-zinc-800 bg-zinc-900/80 p-3 font-mono text-[11px] leading-relaxed whitespace-pre-wrap text-zinc-400">
            {log || "…"}
          </pre>
        </section>
      )}

      {running && (
        <section>
          <h3 className="mb-2 text-[11px] font-semibold tracking-wide text-sky-400 uppercase">
            Run budget
          </h3>
          {progress ? (
            <BudgetMeters progress={progress} />
          ) : (
            <p className="font-mono text-[11px] text-zinc-600">waiting for the first round…</p>
          )}
          <div className="mt-2">
            <RunningToolBadge storyId={live.id} />
          </div>
        </section>
      )}

      {live.ai_review && live.status !== "ai_review" && (
        <section>
          <div className="mb-2 flex items-center justify-between">
            <h3 className={FIELD_LABEL}>AI review</h3>
            {aiBadge && (
              <span className={`${BADGE} ${aiBadge.tone}`}>{aiBadge.label}</span>
            )}
          </div>
          <div className={PANEL}>
            <Markdown content={live.ai_review} />
          </div>
        </section>
      )}

      {live.spec && (
        <section>
          <h3 className={`mb-2 ${FIELD_LABEL}`}>
            Spec (v{live.spec_version})
          </h3>
          <div className={PANEL}>
            <Markdown content={live.spec} />
          </div>
        </section>
      )}

      {detail && <StoryNotes detail={detail} />}

      {detail && detail.events.length > 0 && <StoryHistory detail={detail} />}

      {showActivity && (
        <section>
          <h3 className="mb-1 text-[11px] font-semibold tracking-wide text-sky-400 uppercase">
            Activity
          </h3>
          <ActivityFeed events={activity} />
        </section>
      )}
    </div>
  );
}
