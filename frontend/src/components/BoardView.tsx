import { useEffect, useState } from "react";

import { ReviewToast } from "@/components/ReviewToast";
import { NewStoryModal } from "@/components/NewStoryModal";
import { ProjectPicker } from "@/components/ProjectPicker";
import { StoryDetail } from "@/components/StoryDetail";
import { TrainPanel } from "@/components/TrainPanel";
import { COLUMNS, belongsToColumn, columnForStatus, isCollapsedLane } from "@/components/boardColumns";
import { useStore } from "@/store";
import type { ProviderLimit, Story } from "@/types";

import { BudgetBadge } from "@/components/BudgetMeters";
import { RunningToolBadge } from "@/components/RunningTool";
import { timeAgo } from "@/utils/timeAgo";
import { parseServerTime } from "@/utils/parseServerTime";

const MONTHS_SHORT = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

/** "zai limited until 14:30" — the local time a provider's usage-limit hold
 * ends (headless 429s). Deterministic English formatting (no locale), and
 * the date is added when the reset is not today. Pure so the badge contract
 * is testable without jsdom. */
export function providerLimitLabel(limit: ProviderLimit, now: Date = new Date()): string {
  const until = parseServerTime(limit.until);
  const hhmm = `${String(until.getHours()).padStart(2, "0")}:${String(
    until.getMinutes(),
  ).padStart(2, "0")}`;
  const sameDay =
    until.getFullYear() === now.getFullYear() &&
    until.getMonth() === now.getMonth() &&
    until.getDate() === now.getDate();
  if (sameDay) return `${limit.provider_id} limited until ${hhmm}`;
  return `${limit.provider_id} limited until ${until.getDate()} ${
    MONTHS_SHORT[until.getMonth()]
  } ${hhmm}`;
}

function ProviderLimitBadge({ limit }: { limit: ProviderLimit }) {
  const clearProviderLimit = useStore((s) => s.clearProviderLimit);
  return (
    <span
      className="flex shrink-0 items-center gap-1.5 rounded-full border border-amber-800/70 bg-amber-950/60 px-2 py-1 text-[11px] text-amber-300"
      title={`${limit.reason} — headless pickup on this provider waits until the stated reset; internal stories keep running`}
    >
      {providerLimitLabel(limit)}
      <button
        onClick={() => clearProviderLimit(limit.provider_id).catch(() => {})}
        className="rounded px-1 text-amber-400 transition hover:text-amber-200"
        title="Clear the hold; the scheduler retries this provider at its next tick"
      >
        Clear hold
      </button>
    </span>
  );
}

function StoryCard({ story, blockedBy }: { story: Story; blockedBy: Story[] }) {
  const openStory = useStore((s) => s.openStory);
  const setEpicFilter = useStore((s) => s.setEpicFilter);
  const epics = useStore((s) => s.epics);
  const projects = useStore((s) => s.projects);
  const progress = useStore((s) => s.storyProgress[story.id]);
  const column = columnForStatus(story.status);
  const epic = epics.find((e) => e.id === story.epic_id);
  const project = projects.find((p) => p.id === story.project_id);
  const busy =
    story.status === "in_progress" ||
    story.status === "ai_review" ||
    story.status === "executing";

  return (
    <button
      onClick={() => openStory(story.id)}
      className="w-full rounded-xl border border-zinc-800/80 bg-zinc-900/60 p-3 text-left transition hover:border-zinc-600"
    >
      <div className="flex items-start justify-between gap-2">
        <p className="text-sm font-medium leading-snug text-zinc-200">{story.title}</p>
        {busy && (
          <span className="mt-0.5 h-2 w-2 shrink-0 animate-pulse rounded-full bg-sky-400" />
        )}
      </div>
      {story.depends_on.length > 0 && (
        <p
          className={`mt-1.5 text-[10px] ${blockedBy.length ? "text-amber-400" : "text-zinc-600"}`}
          title={blockedBy.map((b) => `${b.title} (${b.status})`).join("\n") || "all dependencies done"}
        >
          {blockedBy.length
            ? `⏸ blocked by ${blockedBy.length}: ${blockedBy.map((b) => b.title).join(", ").slice(0, 40)}${blockedBy.map((b) => b.title).join(", ").length > 40 ? "…" : ""}`
            : `deps ✓ ${story.depends_on.length}`}
        </p>
      )}
      <div className="mt-2 flex items-center gap-2 text-[11px] text-zinc-500">
        <span className={`h-1.5 w-1.5 rounded-full ${column?.dot ?? "bg-zinc-500"}`} />
        <span className="truncate font-mono">
          {story.model ?? `auto:${story.skill ?? "medium"}`}
        </span>
        {story.priority === "urgent" && (
          <span
            className="shrink-0 rounded bg-red-950/80 px-1 text-[10px] font-semibold text-red-300"
            title="urgent: picked up ahead of all normal stories"
          >
            urgent
          </span>
        )}
        {story.execution === "external" && (
          <span
            className="shrink-0 rounded bg-teal-950/80 px-1 text-[10px] text-teal-300"
            title="external execution — an outside agent claims this story and lands the code itself (claim/complete API or MCP); the internal runner never picks it up"
          >
            ext{story.claimed_by ? ` · ${story.claimed_by}` : ""}
          </span>
        )}
        {story.execution === "headless" && (
          <span
            className="shrink-0 rounded bg-violet-950/80 px-1 text-[10px] text-violet-300"
            title="headless execution — the CODE stage runs via the Claude Code CLI in this story's worktree; spec, verify, AI review and the gates are unchanged"
          >
            claude
          </span>
        )}
        {story.ai_verdict === "rejected" && (
          <span className="shrink-0 rounded bg-red-950/80 px-1 text-[10px] text-red-300">
            AI rejected
          </span>
        )}
        <span className="ml-auto shrink-0">{timeAgo(story.updated_at)}</span>
      </div>
      {(story.status === "in_progress" || story.status === "executing") &&
        progress && (
          <div className="mt-1.5">
            <BudgetBadge progress={progress} />
          </div>
        )}
      {story.status === "executing" && (
        <div className="mt-1.5">
          <RunningToolBadge storyId={story.id} compact />
        </div>
      )}
      {story.execution === "external" && story.claimed_by && (
        <p
          className={`mt-1.5 truncate rounded px-1.5 py-0.5 text-[10px] ${
            story.status === "in_progress"
              ? "bg-amber-950/70 text-amber-300"
              : "bg-zinc-800 text-zinc-500"
          }`}
          title={`externally claimed — use Release in the story detail if this rots`}
        >
          ⏸ claimed by {story.claimed_by}
          {story.claimed_at ? ` · ${timeAgo(story.claimed_at)}` : ""}
        </p>
      )}
      {story.status === "queued" &&
        story.queue_position != null &&
        story.waiting_behind != null && (
          <p className="mt-1.5 text-[10px] text-zinc-600">
            waiting for a slot ({story.waiting_behind} busy) · position{" "}
            {story.queue_position}
          </p>
        )}
      {story.status === "failed" && story.error && (
        <p className="mt-2 truncate text-[11px] text-red-400">{story.error}</p>
      )}
      {project && (
        <span
          className="mt-1 mr-1.5 inline-block rounded bg-sky-950/70 px-1.5 py-0.5 text-[10px] text-sky-300"
          title={`project: ${project.name} (${project.root})`}
        >
          ▣ {project.name}
        </span>
      )}
      {epic && (
        <button
          onClick={(e) => {
            e.stopPropagation();
            setEpicFilter(epicFilterMatches(epic.id) ? null : epic.id);
          }}
          className="mt-2 rounded bg-violet-950/70 px-1.5 py-0.5 text-[10px] text-violet-300 transition hover:bg-violet-900/70"
          title={`epic: ${epic.title} — click to filter`}
        >
          ◆ {epic.title}
        </button>
      )}
    </button>
  );
}

let currentEpicFilter: string | null = null;
function epicFilterMatches(id: string) {
  return currentEpicFilter === id;
}

export function BoardView() {
  const stories = useStore((s) => s.stories);
  const epics = useStore((s) => s.epics);
  const projects = useStore((s) => s.projects);
  const epicFilter = useStore((s) => s.epicFilter);
  const setEpicFilter = useStore((s) => s.setEpicFilter);
  const projectFilter = useStore((s) => s.projectFilter);
  const setProjectFilter = useStore((s) => s.setProjectFilter);
  const pendingToastId = useStore((s) => s.pendingToastId);
  const openStory = useStore((s) => s.openStory);
  const dismissToast = useStore((s) => s.dismissToast);
  const providerLimits = useStore((s) => s.providerLimits);
  const refreshProviderLimits = useStore((s) => s.refreshProviderLimits);
  const schedulerStalledSince = useStore((s) => s.schedulerStalledSince);
  const [modalOpen, setModalOpen] = useState(false);
  // lanes that render expanded despite being collapsible (superseded starts
  // collapsed: an archive, not a work queue)
  const [expandedLanes, setExpandedLanes] = useState<ReadonlySet<string>>(new Set());
  currentEpicFilter = epicFilter;

  // Holds expire server-side with no story event to say so, and the board is
  // where the operator must see both the hold and its end — a slow poll keeps
  // the badges honest (same cadence as the train panel).
  useEffect(() => {
    refreshProviderLimits().catch(() => {});
    const timer = window.setInterval(
      () => refreshProviderLimits().catch(() => {}),
      5000,
    );
    return () => window.clearInterval(timer);
  }, [refreshProviderLimits]);

  const visible = stories.filter(
    (s) =>
      (!epicFilter || s.epic_id === epicFilter) &&
      (!projectFilter || s.project_id === projectFilter),
  );
  const storyById = new Map(stories.map((s) => [s.id, s]));
  const blockedBy = (story: Story): Story[] =>
    story.depends_on
      .map((id) => storyById.get(id))
      .filter((d): d is Story => !!d && d.status !== "done");

  // Find the story for the toast
  const toastStory = pendingToastId ? storyById.get(pendingToastId) : undefined;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <header className="flex h-12 shrink-0 items-center justify-between gap-2 border-b border-zinc-800/70 px-4">
        <div className="flex min-w-0 items-center gap-2">
          <h1 className="shrink-0 text-sm font-medium text-zinc-300">Story board</h1>
          {providerLimits.map((limit) => (
            <ProviderLimitBadge key={limit.provider_id} limit={limit} />
          ))}
        </div>
        <div className="flex items-center gap-2">
          <ProjectPicker
            value={projectFilter ?? ""}
            noneLabel="all projects"
            title="Filter the board; new stories default to this project"
            onChange={(id) => setProjectFilter(id || null)}
          />
          <select
            value={epicFilter ?? ""}
            onChange={(e) => setEpicFilter(e.target.value || null)}
            className="max-w-44 truncate rounded-lg border border-zinc-700 bg-zinc-900 px-2 py-1.5 text-xs text-zinc-300 outline-none focus:border-zinc-500"
            title="Filter by epic"
          >
            <option value="">all epics</option>
            {epics.map((e) => (
              <option key={e.id} value={e.id}>
                ◆ {e.title} ({e.done_count}/{e.story_count})
              </option>
            ))}
          </select>
          <button
            onClick={() => setModalOpen(true)}
            className="rounded-lg bg-zinc-100 px-3 py-1.5 text-xs font-medium text-zinc-900 transition hover:bg-white"
          >
            + New story
          </button>
        </div>
      </header>

      {schedulerStalledSince && (
        <div
          role="alert"
          className="flex shrink-0 items-center gap-2 border-b border-red-900/60 bg-red-950/40 px-4 py-1.5 text-xs text-red-300"
          title="The story scheduler's heartbeat went stale (last completed tick below). Nothing is moving stories; the server may need a restart."
        >
          <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-red-400" />
          Scheduler stalled since{" "}
          {parseServerTime(schedulerStalledSince).toLocaleTimeString([], {
            hour: "2-digit",
            minute: "2-digit",
            hourCycle: "h23",
          })}
        </div>
      )}

      <div className="min-h-0 flex-1 overflow-x-auto overflow-y-auto p-4">
        {(() => {
          const trainProject = projectFilter
            ? projects.find((p) => p.id === projectFilter)
            : undefined;
          return trainProject && trainProject.merge_train ? (
            <TrainPanel project={trainProject} />
          ) : null;
        })()}
        <div className="flex h-full min-w-max gap-3">
          {COLUMNS.map(({ status, label, dot }) => {
            const cards = visible.filter((s) => belongsToColumn(s, status));
            const collapsed = isCollapsedLane(status) && !expandedLanes.has(status);
            return (
              <section
                key={status}
                className={`flex shrink-0 flex-col rounded-xl bg-zinc-900/30 ${
                  collapsed ? "w-full max-w-72" : "w-72"
                }`}
              >
                <div className="flex items-center gap-2 px-3 py-2.5">
                  <span className={`h-2 w-2 rounded-full ${dot}`} />
                  <h2 className="text-xs font-semibold tracking-wide text-zinc-400 uppercase">
                    {label}
                  </h2>
                  <span className="ml-auto text-xs text-zinc-500">{cards.length}</span>
                  {isCollapsedLane(status) && (
                    <button
                      onClick={() =>
                        setExpandedLanes((prev) => {
                          const next = new Set(prev);
                          if (next.has(status)) {
                            next.delete(status);
                          } else {
                            next.add(status);
                          }
                          return next;
                        })
                      }
                      className="ml-1 rounded px-1 text-xs text-zinc-500 transition hover:text-zinc-300"
                      title={
                        collapsed
                          ? "Expand this lane (superseded stories are closed, not failed)"
                          : "Collapse this lane"
                      }
                    >
                      {collapsed ? "▸" : "▾"}
                    </button>
                  )}
                </div>
                {collapsed ? (
                  <div className="px-3 pb-2.5">
                    <p className="text-[11px] text-zinc-600">
                      collapsed — {cards.length} closed as superseded
                    </p>
                    {cards.length > 0 && (
                      <p
                        className="mt-0.5 truncate text-[11px] text-zinc-700"
                        title={cards.map((s) => s.title).join(", ")}
                      >
                        {cards.map((s) => s.title).join(", ")}
                      </p>
                    )}
                  </div>
                ) : (
                  <div className="flex-1 space-y-2 overflow-y-auto px-2 pb-2">
                    {cards.map((story) => (
                      <StoryCard key={story.id} story={story} blockedBy={blockedBy(story)} />
                    ))}
                    {cards.length === 0 && (
                      <p className="px-2 py-3 text-center text-[11px] text-zinc-700">empty</p>
                    )}
                  </div>
                )}
              </section>
            );
          })}
        </div>
      </div>

      <StoryDetail />
      {modalOpen && (
        <NewStoryModal
          onClose={() => setModalOpen(false)}
          defaultProjectId={projectFilter ?? ""}
        />
      )}
      {toastStory && (
        <ReviewToast
          storyId={toastStory.id}
          title={toastStory.title}
          onDismiss={dismissToast}
          onClick={openStory}
        />
      )}
    </div>
  );
}
