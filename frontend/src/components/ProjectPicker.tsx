import { useState } from "react";

import { api } from "@/api";
import { FolderPicker } from "@/components/FolderPicker";
import { useStore } from "@/store";

/** Shared project selector with zcode-style folder setting: pick an existing
 * project or create one on the spot by browsing to a folder. */
export function ProjectPicker({
  value,
  onChange,
  noneLabel = "no project",
  title = "Workspace (file tools + permissions scope)",
}: {
  value: string;
  onChange: (projectId: string) => void;
  noneLabel?: string;
  title?: string;
}) {
  const projects = useStore((s) => s.projects);
  const [creating, setCreating] = useState(false);

  return (
    <>
      <select
        value={value}
        title={title}
        onChange={(e) => {
          if (e.target.value === "__new__") {
            setCreating(true);
            return;
          }
          onChange(e.target.value);
        }}
        className="max-w-44 truncate rounded-lg border border-zinc-700 bg-zinc-900 px-2 py-1.5 text-xs text-zinc-300 outline-none focus:border-zinc-500"
      >
        <option value="">{noneLabel}</option>
        {projects.map((p) => (
          <option key={p.id} value={p.id}>
            ▣ {p.name}
          </option>
        ))}
        <option value="__new__">＋ New project…</option>
      </select>
      {creating && (
        <NewProjectModal
          onCancel={() => setCreating(false)}
          onCreated={(id) => {
            setCreating(false);
            onChange(id);
          }}
        />
      )}
    </>
  );
}

export function NewProjectModal({
  onCreated,
  onCancel,
}: {
  onCreated: (projectId: string) => void;
  onCancel: () => void;
}) {
  const refreshProjects = useStore((s) => s.refreshProjects);
  const [name, setName] = useState("");
  const [root, setRoot] = useState("");
  const [browsing, setBrowsing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const create = async () => {
    if (!root.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      const project = await api.createProject({
        name: name.trim() || root.trim().split("/").filter(Boolean).pop() || "project",
        root: root.trim(),
      });
      await refreshProjects();
      onCreated(project.id);
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={onCancel}>
      <div
        className="w-full max-w-md space-y-4 rounded-2xl border border-zinc-800 bg-zinc-950 p-5 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="text-base font-semibold text-zinc-100">New project</h2>
        <p className="-mt-2 text-xs leading-relaxed text-zinc-500">
          A project is a workspace folder: it scopes file tools and permissions
          for chats and stories working in it — zcode-style.
        </p>

        <label className="block space-y-1">
          <span className="text-[11px] font-semibold tracking-wide text-zinc-500 uppercase">
            Folder
          </span>
          <div className="flex gap-2">
            <input
              value={root}
              onChange={(e) => setRoot(e.target.value)}
              placeholder="/path/to/project"
              className="flex-1 rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2 font-mono text-xs text-zinc-200 placeholder-zinc-600 outline-none focus:border-zinc-500"
            />
            <button
              onClick={() => setBrowsing(true)}
              className="shrink-0 rounded-lg border border-zinc-700 px-3 py-2 text-xs text-zinc-300 transition hover:bg-zinc-800"
            >
              Browse…
            </button>
          </div>
        </label>

        {browsing && (
          <FolderPicker
            onCancel={() => setBrowsing(false)}
            onPick={(path) => {
              setBrowsing(false);
              setRoot(path);
            }}
          />
        )}

        <label className="block space-y-1">
          <span className="text-[11px] font-semibold tracking-wide text-zinc-500 uppercase">
            Name <span className="font-normal normal-case">(defaults to the folder name)</span>
          </span>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder={root.trim().split("/").filter(Boolean).pop() ?? "my project"}
            className="w-full rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm text-zinc-200 placeholder-zinc-600 outline-none focus:border-zinc-500"
          />
        </label>

        {error && <p className="text-xs text-red-400">{error}</p>}

        <div className="flex justify-end gap-2 pt-1">
          <button
            onClick={onCancel}
            className="rounded-lg px-3 py-1.5 text-sm text-zinc-400 transition hover:text-zinc-200"
          >
            Cancel
          </button>
          <button
            onClick={create}
            disabled={!root.trim() || busy}
            className="rounded-lg bg-zinc-100 px-4 py-1.5 text-sm font-medium text-zinc-900 transition hover:bg-white disabled:cursor-not-allowed disabled:opacity-30"
          >
            {busy ? "Creating…" : "Create project"}
          </button>
        </div>
      </div>
    </div>
  );
}
