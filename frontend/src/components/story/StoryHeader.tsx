import type { Project, Story } from "@/types";

/** Panel header: title plus the mono metadata line and the ✕ close button. */
export function StoryHeader({
  live,
  projects,
  onClose,
}: {
  live: Story;
  projects: Project[];
  onClose: () => void;
}) {
  const project = projects.find((p) => p.id === live.project_id);
  return (
    <header className="flex shrink-0 items-start justify-between gap-3 border-b border-zinc-800/70 px-5 py-4">
      <div className="min-w-0">
        <h2 className="truncate text-base font-semibold text-zinc-100">{live.title}</h2>
        <p className="mt-1 font-mono text-[11px] text-zinc-500">
          {live.model ?? `auto:${live.skill ?? "medium"}`} · {live.status}
          {live.execution === "external" &&
            ` · external${live.claimed_by ? ` · claimed by ${live.claimed_by}` : ""}`}
          {live.execution === "headless" && " · headless (Claude Code CLI)"}
          {project && ` · ▣ ${project.name}`}
          {live.spec_version > 0 && ` · spec v${live.spec_version}`}
          {live.approved_by && ` · approved by ${live.approved_by}`}
        </p>
      </div>
      <button
        onClick={onClose}
        className="rounded-lg p-1.5 text-zinc-500 transition hover:bg-zinc-800 hover:text-zinc-200"
      >
        ✕
      </button>
    </header>
  );
}
