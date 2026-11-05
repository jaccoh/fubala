import { useCallback, useEffect, useState } from "react";

import { api } from "@/api";

interface BrowseState {
  path: string;
  parent: string | null;
  dirs: { name: string; path: string }[];
}

/** zcode-style folder chooser: breadcrumb path, clickable subdirectories,
 * manual path entry. Calls onPick with an absolute directory path. */
export function FolderPicker({
  onPick,
  onCancel,
  startPath,
}: {
  onPick: (path: string) => void;
  onCancel: () => void;
  startPath?: string;
}) {
  const [state, setState] = useState<BrowseState | null>(null);
  const [manual, setManual] = useState("");
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (path?: string) => {
    setError(null);
    try {
      const res = await api.browseFolders(path);
      setState({ path: res.path, parent: res.parent, dirs: res.dirs });
      setManual(res.path);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    load(startPath);
  }, [load, startPath]);

  const crumbs = (): { label: string; path: string }[] => {
    if (!state) return [];
    const parts = state.path.split("/").filter(Boolean);
    const out = [{ label: "/", path: "/" }];
    let acc = "";
    for (const part of parts) {
      acc += `/${part}`;
      out.push({ label: part, path: acc });
    }
    return out;
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={onCancel}>
      <div
        className="flex max-h-[80vh] w-full max-w-xl flex-col gap-3 rounded-2xl border border-zinc-800 bg-zinc-950 p-5 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between">
          <h2 className="text-base font-semibold text-zinc-100">Choose a folder</h2>
          {state?.parent && (
            <button
              onClick={() => load(state.parent!)}
              className="rounded-lg border border-zinc-700 px-2.5 py-1 text-xs text-zinc-300 transition hover:bg-zinc-800"
            >
              ↑ up
            </button>
          )}
        </div>

        <div className="flex flex-wrap items-center gap-0.5 rounded-lg border border-zinc-800 bg-zinc-900/60 px-2 py-1.5 font-mono text-xs">
          {crumbs().map((c, i) => (
            <button
              key={c.path}
              onClick={() => load(c.path)}
              className={`rounded px-1 py-0.5 transition hover:bg-zinc-800 hover:text-zinc-100 ${
                i === crumbs().length - 1 ? "text-zinc-100" : "text-zinc-500"
              }`}
            >
              {c.label}
              {i > 0 && i < crumbs().length - 1 && <span className="ml-0.5 text-zinc-700">/</span>}
            </button>
          ))}
        </div>

        <div className="min-h-40 flex-1 overflow-y-auto rounded-lg border border-zinc-800 bg-zinc-900/40">
          {!state && !error && <p className="p-4 text-xs text-zinc-600">loading…</p>}
          {error && <p className="p-4 text-xs text-red-400">{error}</p>}
          {state?.dirs.length === 0 && (
            <p className="p-4 text-xs text-zinc-600">no subdirectories</p>
          )}
          {state?.dirs.map((d) => (
            <button
              key={d.path}
              onDoubleClick={() => load(d.path)}
              onClick={() => setManual(d.path)}
              className="flex w-full items-center gap-2 px-3 py-1.5 text-left font-mono text-xs text-zinc-300 transition hover:bg-zinc-800/70"
              title={d.path}
            >
              <span className="text-sky-400">▸</span>
              <span className="truncate">{d.name}</span>
              <span
                className="ml-auto shrink-0 rounded px-1 text-[10px] text-zinc-600 opacity-0 transition group-hover:opacity-100"
                onClick={(e) => {
                  e.stopPropagation();
                  load(d.path);
                }}
              >
                open
              </span>
            </button>
          ))}
        </div>

        <div className="flex gap-2">
          <input
            value={manual}
            onChange={(e) => setManual(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && load(manual)}
            placeholder="/absolute/path"
            className="flex-1 rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2 font-mono text-xs text-zinc-200 placeholder-zinc-600 outline-none focus:border-zinc-500"
          />
          <button
            onClick={() => load(manual)}
            className="rounded-lg border border-zinc-700 px-3 py-2 text-xs text-zinc-300 transition hover:bg-zinc-800"
          >
            Go
          </button>
        </div>
        <p className="text-[10px] text-zinc-600">
          click to select · double-click to open · or type a path and hit Go
        </p>

        <div className="flex justify-end gap-2">
          <button
            onClick={onCancel}
            className="rounded-lg px-3 py-1.5 text-sm text-zinc-400 transition hover:text-zinc-200"
          >
            Cancel
          </button>
          <button
            onClick={() => state && onPick(manual || state.path)}
            disabled={!state}
            className="rounded-lg bg-emerald-600 px-4 py-1.5 text-sm font-medium text-white transition hover:bg-emerald-500 disabled:cursor-not-allowed disabled:opacity-40"
          >
            Use this folder
          </button>
        </div>
      </div>
    </div>
  );
}
