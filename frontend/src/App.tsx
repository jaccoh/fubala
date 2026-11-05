import { useEffect } from "react";

import { Sidebar } from "@/components/Sidebar";
import { BoardView } from "@/components/BoardView";
import { PermissionPrompts } from "@/components/PermissionPrompts";
import { ProjectsView } from "@/components/ProjectsView";
import { ChatView } from "@/components/ChatView";
import { SettingsView } from "@/components/SettingsView";
import { SignInView } from "@/components/SignInView";
import { useStore } from "@/store";
import { buildUrl } from "@/url";
import { UnauthorizedError } from "@/api";

export default function App() {
  const init = useStore((s) => s.init);
  const view = useStore((s) => s.view);
  const urlRestored = useStore((s) => s.urlRestored);
  const authRequired = useStore((s) => s.authRequired);

  useEffect(() => {
    init().catch((e) => {
      // expected while signed out: the gate is already on screen
      if (e instanceof UnauthorizedError) return;
      console.error("init failed:", e);
    });
  }, [init]);

  // ---- URL writer -------------------------------------------------------
  //
  // Keeps the address bar live as the user navigates. It is installed only
  // once `urlRestored` is true (set by `applyLocation`, init's last step), so
  // init defaults can never clobber deep-link params before the restore has
  // happened.
  //
  //   view change                  → pushState    (back button steps views)
  //   filter / openStoryId change  → replaceState (no history spam)
  //
  // popstate re-reads the location into the store; while that settle the
  // writer only tracks the URL (the browser already moved history).
  useEffect(() => {
    if (!urlRestored) return;

    let lastView: string | null = null;
    let lastUrl: string | null = null;
    let settling = false; // suppress writes while a popstate is being applied

    const write = (url: string, push: boolean) => {
      if (push) history.pushState(null, "", url);
      else history.replaceState(null, "", url);
    };

    const urlFor = () => {
      const { view: v, openStoryId, projectFilter, epicFilter } = useStore.getState();
      return { v, url: buildUrl(v, openStoryId, projectFilter, epicFilter) };
    };

    // Install-time baseline: state was just restored from the URL, so these
    // normally agree. They differ only when a param was ignored (stale
    // `?story=`) — normalise that away without adding a history entry.
    {
      const { v, url } = urlFor();
      lastView = v;
      lastUrl = document.location.pathname + document.location.search;
      if (lastUrl !== url) {
        history.replaceState(null, "", url);
        lastUrl = url;
      }
    }

    const sync = () => {
      const { v, url } = urlFor();
      const viewChanged = lastView !== null && lastView !== v;
      if (url !== lastUrl || viewChanged) {
        if (!settling) write(url, viewChanged);
        lastUrl = url;
      }
      lastView = v;
    };

    const unsub = useStore.subscribe(sync);

    const onPopState = () => {
      settling = true;
      useStore
        .getState()
        .applyLocation(document.location)
        .catch(() => {})
        .finally(() => {
          settling = false;
          // normalise: the browser already moved, but state may have ignored a
          // param (e.g. `?story=ghost`) — rewrite in place, no new entry
          const { v, url } = urlFor();
          lastUrl = url;
          lastView = v;
          history.replaceState(null, "", url);
        });
    };
    window.addEventListener("popstate", onPopState);

    return () => {
      unsub();
      window.removeEventListener("popstate", onPopState);
    };
  }, [urlRestored]);

  // Signed-out remote browser: no shell, no SSE streams — just the gate.
  if (authRequired) return <SignInView />;

  return (
    <div className="flex h-full">
      <Sidebar />
      <main className="flex min-w-0 flex-1 flex-col">
        {view === "board" ? (
          <BoardView />
        ) : view === "projects" ? (
          <ProjectsView />
        ) : view === "settings" ? (
          <SettingsView />
        ) : (
          <ChatView />
        )}
      </main>
      <PermissionPrompts />
    </div>
  );
}
