import { useEffect, useState } from "react";

import { LiveStrip } from "@/components/story/LiveStrip";
import { RejectBox } from "@/components/story/RejectBox";
import { StoryActions, type StoryActionBody, type StoryActionName } from "@/components/story/StoryActions";
import { StoryBody } from "@/components/story/StoryBody";
import { StoryHeader } from "@/components/story/StoryHeader";
import { useStore } from "@/store";
import { APP_TITLE, storyTabTitle } from "@/utils/tabTitle";

export function StoryDetail() {
  const openStoryId = useStore((s) => s.openStoryId);
  const storyDetail = useStore((s) => s.storyDetail);
  const stories = useStore((s) => s.stories);
  const storyLogs = useStore((s) => s.storyLogs);
  const storyProgress = useStore((s) => s.storyProgress);
  const storyActivity = useStore((s) => s.storyActivity);
  const closeStory = useStore((s) => s.closeStory);
  const storyAction = useStore((s) => s.storyAction);
  const supersedeAction = useStore((s) => s.supersedeStory);
  const retryLandingAction = useStore((s) => s.retryLanding);
  const deleteStory = useStore((s) => s.deleteStory);
  const epics = useStore((s) => s.epics);
  const projects = useStore((s) => s.projects);
  const patchStory = useStore((s) => s.patchStoryAction);
  const [rejecting, setRejecting] = useState(false);

  // SSE keeps `stories` fresher than the fetched detail; prefer it for status
  const live = openStoryId
    ? (stories.find((s) => s.id === openStoryId) ?? storyDetail)
    : null;

  // Glanceable tab title while the panel is open; restored on close.
  // Reads `live` (SSE-fresh status) so the title never lags the badge.
  // Must run before the early returns below so hook order stays stable.
  useEffect(() => {
    if (!live) return;
    const previous = document.title;
    document.title = storyTabTitle(live.status, live.title);
    return () => {
      document.title = previous || APP_TITLE;
    };
  }, [live]);

  if (!openStoryId) return null;
  if (!live) return null;

  const log = storyLogs[openStoryId] ?? "";
  const progress = storyProgress[openStoryId];
  const activity = storyActivity[openStoryId] ?? [];

  const close = () => {
    setRejecting(false);
    closeStory();
  };

  const run = (
    action: StoryActionName,
    body?: StoryActionBody,
  ) => storyAction(openStoryId, action, body).catch((e: Error) => alert(e.message));

  // Supersede is its own endpoint (not a /story_action), so it gets its own
  // handler: prompt for what made this story irrelevant, then close it.
  const supersede = () => {
    const answer = window.prompt(
      "What supersedes this story? A story id, or a short note (e.g. 'landed by hand in #123').",
      "",
    );
    if (answer === null) return; // cancelled
    supersedeAction(openStoryId, "human", answer.trim()).catch((e: Error) =>
      alert(e.message),
    );
  };

  // `live` comes from SSE and has no `evidence`/`code_diff` — only the fetched
  // detail does, so it is only passed down when it refers to this very story.
  const detailForLive = storyDetail && storyDetail.id === live.id ? storyDetail : null;

  // Re-arm the landing poller (failed push+PR story with a PR): dedicated
  // retry stage, same alert-on-failure handling as the other actions.
  const retryLanding = () =>
    retryLandingAction(openStoryId, "human").catch((e: Error) => alert(e.message));

  return (
    <>
      <div className="fixed inset-0 z-30 bg-black/50" onClick={close} />
      <aside className="fixed inset-y-0 right-0 z-40 flex w-[520px] max-w-full flex-col border-l border-zinc-800 bg-zinc-950 shadow-2xl">
        <StoryHeader live={live} projects={projects} onClose={close} />

        {/* actions */}
        <StoryActions
          live={live}
          rejecting={rejecting}
          onReject={() => setRejecting(true)}
          run={run}
          onDelete={deleteStory}
          onSupersede={supersede}
          onRetryLanding={retryLanding}
        />

        {rejecting && live.status === "human_review" && (
        <RejectBox run={run} onClose={() => setRejecting(false)} />
      )}

        {/* live strip — fixed above the scrollable body, no scroll-away */}
        <LiveStrip
          status={live.status}
          execution={live.execution}
          log={log}
          slotState={live.slot_state}
        />

        {/* body */}
        <StoryBody
          live={live}
          detail={detailForLive}
          epics={epics}
          stories={stories}
          projects={projects}
          patchStory={patchStory}
          log={log}
          activity={activity}
          progress={progress}
        />
      </aside>
    </>
  );
}
