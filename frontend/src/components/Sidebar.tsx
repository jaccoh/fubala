import { useState } from "react";

import { NewProjectModal } from "@/components/ProjectPicker";
import { useStore } from "@/store";
import type { SessionInfo } from "@/types";

function sessionTitle(s: { title: string }): string {
  return s.title || "Untitled";
}

function ViewSwitcher() {
  const view = useStore((s) => s.view);
  const setView = useStore((s) => s.setView);

  const tab = (target: "chat" | "board", label: string) => (
    <button
      onClick={() => setView(target)}
      className={`flex-1 rounded-md px-3 py-1.5 text-xs font-medium transition ${
        view === target
          ? "bg-zinc-700/80 text-zinc-100"
          : "text-zinc-400 hover:bg-zinc-800/50 hover:text-zinc-200"
      }`}
    >
      {label}
    </button>
  );

  return (
    <div className="flex rounded-lg bg-zinc-800/40 p-0.5">
      {tab("chat", "Chat")}
      {tab("board", "Board")}
    </div>
  );
}

function ChatRow({ session }: { session: SessionInfo }) {
  const currentId = useStore((s) => s.currentId);
  const view = useStore((s) => s.view);
  const openSession = useStore((s) => s.openSession);
  const deleteSession = useStore((s) => s.deleteSession);
  return (
    <div
      className={`group flex items-center rounded-lg pr-1 transition ${
        session.id === currentId && view === "chat"
          ? "bg-zinc-800/80"
          : "hover:bg-zinc-800/40"
      }`}
    >
      <button
        onClick={() => openSession(session.id)}
        className="min-w-0 flex-1 truncate py-1.5 pr-1 pl-7 text-left text-[13px] text-zinc-300"
        title={sessionTitle(session)}
      >
        {sessionTitle(session)}
      </button>
      <button
        onClick={() => deleteSession(session.id)}
        className="hidden shrink-0 rounded p-1 text-zinc-500 hover:text-red-400 group-hover:block"
        title="Delete session"
      >
        ✕
      </button>
    </div>
  );
}

/** zcode-style chat tree: projects as collapsible nodes, chats as leaves. */
function ChatTree() {
  const sessions = useStore((s) => s.sessions);
  const projects = useStore((s) => s.projects);
  const currentId = useStore((s) => s.currentId);
  const newSession = useStore((s) => s.newSession);
  const setView = useStore((s) => s.setView);
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  const [creatingProject, setCreatingProject] = useState(false);

  const unassigned = sessions.filter((s) => !s.project_id);
  const toggle = (key: string) => setCollapsed((c) => ({ ...c, [key]: !c[key] }));
  // a project with the open chat auto-expands
  const isOpen = (key: string, containsCurrent: boolean) =>
    !collapsed[key] || (containsCurrent && false);

  return (
    <>
      <div className="px-3">
        <button
          onClick={() => newSession()}
          className="w-full rounded-lg bg-zinc-800 px-3 py-2 text-sm font-medium text-zinc-100 transition hover:bg-zinc-700"
        >
          + New chat
        </button>
      </div>

      <nav className="mt-3 flex-1 space-y-2 overflow-y-auto px-3 pb-3">
        {projects.map((p) => {
          const chats = sessions.filter((s) => s.project_id === p.id);
          const containsCurrent = chats.some((s) => s.id === currentId);
          const open = isOpen(p.id, containsCurrent);
          return (
            <div key={p.id}>
              <div className="group flex items-center gap-1 rounded-lg pr-1 transition hover:bg-zinc-800/40">
                <button
                  onClick={() => toggle(p.id)}
                  className="flex min-w-0 flex-1 items-center gap-1.5 px-2 py-1.5 text-left"
                  title={`${p.name} — ${p.root}`}
                >
                  <span className={`text-[10px] text-zinc-500 transition ${open ? "rotate-90" : ""}`}>▶</span>
                  <span className="truncate text-xs font-semibold text-zinc-300">▣ {p.name}</span>
                  <span className="shrink-0 text-[10px] text-zinc-600">{chats.length}</span>
                </button>
                <button
                  onClick={() => newSession(p.id)}
                  className="shrink-0 rounded p-1 text-zinc-600 transition hover:text-emerald-300 group-hover:text-zinc-300"
                  title={`New chat in ${p.name}`}
                >
                  ＋
                </button>
              </div>
              {open && (
                <div className="space-y-0.5">
                  {chats.map((s) => (
                    <ChatRow key={s.id} session={s} />
                  ))}
                  {chats.length === 0 && (
                    <p className="py-1 pr-2 pl-7 text-[11px] text-zinc-700">
                      no chats — ＋ to start one in {p.name}
                    </p>
                  )}
                </div>
              )}
            </div>
          );
        })}

        <div>
          <div className="group flex items-center rounded-lg pr-1 transition hover:bg-zinc-800/40">
            <button
              onClick={() => toggle("__none__")}
              className="flex min-w-0 flex-1 items-center gap-1.5 px-2 py-1.5 text-left"
              title="Chats not tied to a project"
            >
              <span className={`text-[10px] text-zinc-500 transition ${!collapsed.__none__ ? "rotate-90" : ""}`}>▶</span>
              <span className="truncate text-xs font-semibold text-zinc-400">◇ no project</span>
              <span className="shrink-0 text-[10px] text-zinc-600">{unassigned.length}</span>
            </button>
          </div>
          {!collapsed.__none__ && (
            <div className="space-y-0.5">
              {unassigned.map((s) => (
                <ChatRow key={s.id} session={s} />
              ))}
              {unassigned.length === 0 && (
                <p className="py-1 pr-2 pl-7 text-[11px] text-zinc-700">no chats</p>
              )}
            </div>
          )}
        </div>

        <button
          onClick={() => setCreatingProject(true)}
          className="w-full rounded-lg border border-dashed border-zinc-700 px-2 py-1.5 text-left text-xs text-zinc-400 transition hover:border-zinc-500 hover:text-zinc-200"
        >
          ＋ New project…
        </button>
        <button
          onClick={() => setView("projects")}
          className="w-full rounded-lg px-2 py-1 text-left text-[11px] text-zinc-600 transition hover:text-zinc-300"
        >
          manage projects →
        </button>
      </nav>

      {creatingProject && (
        <NewProjectModal
          onCancel={() => setCreatingProject(false)}
          onCreated={() => setCreatingProject(false)}
        />
      )}
    </>
  );
}

export function Sidebar() {
  const view = useStore((s) => s.view);
  const models = useStore((s) => s.models);
  const stories = useStore((s) => s.stories);
  const setView = useStore((s) => s.setView);

  const keyed = models?.providers.filter((p) => p.configured).length ?? 0;
  const total = models?.providers.length ?? 0;
  const active = stories.filter((s) => s.status === "in_progress").length;

  return (
    <aside className="flex w-64 shrink-0 flex-col border-r border-zinc-800/70 bg-zinc-900/40">
      <div className="flex items-center gap-2 px-4 pt-4 pb-3">
        <span className="text-lg">fubala</span>
        <span className="rounded bg-zinc-800 px-1.5 py-0.5 text-[10px] font-medium tracking-wide text-zinc-400 uppercase">
          phase 1
        </span>
      </div>

      <div className="px-3 pb-3">
        <ViewSwitcher />
      </div>

      {view === "chat" ? (
        <ChatTree />
      ) : (
        <div className="flex-1 overflow-y-auto px-3 pb-3">
          <div className="space-y-1.5 rounded-lg bg-zinc-800/30 p-3 text-xs text-zinc-400">
            <p className="text-[11px] font-semibold tracking-wide text-zinc-500 uppercase">
              Stories
            </p>
            <p>{stories.length} total</p>
            <p className="flex items-center gap-1.5">
              {active > 0 && (
                <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-sky-400" />
              )}
              {active} running
            </p>
            <p>
              {stories.filter((s) => s.status === "ai_review" || s.status === "human_review").length}{" "}
              awaiting review
            </p>
          </div>
        </div>
      )}

      <div className="border-t border-zinc-800/70 px-3 py-3">
        <button
          onClick={() => setView(view === "settings" ? "chat" : "settings")}
          className="flex w-full items-center justify-between rounded-lg px-3 py-2 text-sm text-zinc-400 transition hover:bg-zinc-800/40 hover:text-zinc-200"
        >
          <span>⚙ Providers</span>
          <span className="text-xs text-zinc-500">
            {keyed}/{total} keyed
          </span>
        </button>
      </div>
    </aside>
  );
}
