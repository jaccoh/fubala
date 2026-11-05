import { useState } from "react";

import { useStore } from "@/store";
import type { StoryGate, StoryKind } from "@/types";

export function NewStoryModal({
  onClose,
  initial,
  defaultProjectId = "",
}: {
  onClose: () => void;
  initial?: { title?: string; description?: string; acceptance?: string };
  defaultProjectId?: string;
}) {
  const models = useStore((s) => s.models);
  const projects = useStore((s) => s.projects);
  const epics = useStore((s) => s.epics);
  const createEpic = useStore((s) => s.createEpic);
  const createStory = useStore((s) => s.createStory);
  const [title, setTitle] = useState(initial?.title ?? "");
  const [description, setDescription] = useState(initial?.description ?? "");
  const [acceptance, setAcceptance] = useState(initial?.acceptance ?? "");
  const [model, setModel] = useState<string>("");
  const [projectId, setProjectId] = useState<string>(defaultProjectId);
  const [epicId, setEpicId] = useState<string>("");
  const [newEpic, setNewEpic] = useState("");
  const [execution, setExecution] = useState<"inherit" | "internal" | "external">("inherit");
  const [kind, setKind] = useState<StoryKind>("code");
  const [gateSpec, setGateSpec] = useState<"inherit" | StoryGate>("inherit");
  const [gateCode, setGateCode] = useState<"inherit" | StoryGate>("inherit");
  const [busy, setBusy] = useState(false);

  const configured = models?.providers.filter((p) => p.configured) ?? [];
  const selected = model || models?.default_model || "";
  const isAuto = selected.startsWith("auto:");

  const submit = async () => {
    if (!title.trim() || busy) return;
    setBusy(true);
    try {
      await createStory({
        title: title.trim(),
        description: description.trim() || undefined,
        acceptance: acceptance.trim() || undefined,
        ...(isAuto ? { skill: selected.slice(5) } : { model: selected || undefined }),
        ...(projectId ? { project_id: projectId } : {}),
        ...(epicId ? { epic_id: epicId } : {}),
        // inherit = omit the key entirely so the story follows
        // Project.executor; internal/external are explicit overrides
        ...(execution !== "inherit" ? { execution } : {}),
        ...(kind !== "code" ? { kind } : {}),
        // gates resolve payload -> project default -> human on the server
        ...(gateSpec !== "inherit" ? { gate_spec: gateSpec } : {}),
        ...(gateCode !== "inherit" ? { gate_code: gateCode } : {}),
      });
      onClose();
    } catch (e) {
      alert((e as Error).message);
      setBusy(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onClick={onClose}
    >
      <div
        className="w-full max-w-lg space-y-4 rounded-2xl border border-zinc-800 bg-zinc-950 p-5 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="text-base font-semibold text-zinc-100">New story</h2>

        <input
          autoFocus
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && submit()}
          placeholder="Title — what should be true when this is done?"
          className="w-full rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm text-zinc-100 placeholder-zinc-600 outline-none focus:border-zinc-500"
        />
        <textarea
          rows={3}
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder="Description / context for the spec writer…"
          className="w-full resize-none rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm text-zinc-100 placeholder-zinc-600 outline-none focus:border-zinc-500"
        />
        <textarea
          rows={2}
          value={acceptance}
          onChange={(e) => setAcceptance(e.target.value)}
          placeholder="Acceptance criteria (optional, one per line)"
          className="w-full resize-none rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm text-zinc-100 placeholder-zinc-600 outline-none focus:border-zinc-500"
        />

        <label className="flex items-center gap-2 text-xs text-zinc-500">
          <span>project</span>
          <select
            value={projectId}
            onChange={(e) => setProjectId(e.target.value)}
            className="max-w-72 flex-1 truncate rounded-lg border border-zinc-700 bg-zinc-900 px-2.5 py-1.5 text-xs text-zinc-200 outline-none focus:border-zinc-500"
          >
            <option value="">none — global project root</option>
            {projects.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name} · {p.root}{p.root_is_repo ? "" : " (no git)"}
              </option>
            ))}
          </select>
        </label>

        <div className="flex items-end gap-2">
          <label className="flex flex-1 items-center gap-2 text-xs text-zinc-500">
            <span>epic</span>
            <select
              value={epicId}
              onChange={(e) => setEpicId(e.target.value)}
              className="max-w-60 flex-1 truncate rounded-lg border border-zinc-700 bg-zinc-900 px-2.5 py-1.5 text-xs text-zinc-200 outline-none focus:border-zinc-500"
            >
              <option value="">none</option>
              {epics.map((e) => (
                <option key={e.id} value={e.id}>
                  ◆ {e.title}
                </option>
              ))}
            </select>
          </label>
          <input
            value={newEpic}
            onChange={(e) => setNewEpic(e.target.value)}
            placeholder="or new epic…"
            className="w-36 rounded-lg border border-zinc-700 bg-zinc-900 px-2.5 py-1.5 text-xs text-zinc-200 placeholder-zinc-600 outline-none focus:border-zinc-500"
          />
          <button
            onClick={async () => {
              if (!newEpic.trim()) return;
              const epic = await createEpic(newEpic.trim());
              if (epic) {
                setEpicId(epic.id);
                setNewEpic("");
              }
            }}
            disabled={!newEpic.trim()}
            className="rounded-lg bg-zinc-800 px-2.5 py-1.5 text-[11px] text-zinc-200 transition hover:bg-zinc-700 disabled:opacity-30"
          >
            +
          </button>
        </div>

        <label className="flex items-center gap-2 text-xs text-zinc-500">
          <span>spec model</span>
          <select
            value={selected}
            onChange={(e) => setModel(e.target.value)}
            className="max-w-72 flex-1 truncate rounded-lg border border-zinc-700 bg-zinc-900 px-2.5 py-1.5 font-mono text-xs text-zinc-200 outline-none focus:border-zinc-500"
          >
            <optgroup label="Auto — cost-aware by skill">
              <option value="auto:low">auto · skill low</option>
              <option value="auto:medium">auto · skill medium</option>
              <option value="auto:high">auto · skill high</option>
            </optgroup>
            {configured.map((p) => (
              <optgroup key={p.id} label={p.display_name}>
                {p.models.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.id} · {m.skill} · {"$".repeat({ low: 1, medium: 2, high: 3 }[m.cost] ?? 2)}
                  </option>
                ))}
              </optgroup>
            ))}
          </select>
        </label>

        <label className="flex items-center gap-2 text-xs text-zinc-500">
          <span>executed by</span>
          <select
            value={execution}
            onChange={(e) =>
              setExecution(e.target.value as "inherit" | "internal" | "external")
            }
            className="max-w-72 flex-1 truncate rounded-lg border border-zinc-700 bg-zinc-900 px-2.5 py-1.5 text-xs text-zinc-200 outline-none focus:border-zinc-500"
            title="inherit: follow this project's executor (Projects page) — the default. internal: Fubala's own spec → AI review → coder pipeline. external: an outside agent claims the story (claim/complete API or MCP) and lands the code itself, e.g. via a Gitea PR + CI. Choosing internal/external here is an explicit per-story override."
          >
            <option value="inherit">
              inherit — project default (Projects page)
            </option>
            <option value="internal">internal — Fubala runner (spec → review → coder)</option>
            <option value="external">external — outside agent claims it (PR + CI)</option>
          </select>
        </label>

        <label className="flex items-center gap-2 text-xs text-zinc-500">
          <span>kind</span>
          <select
            value={kind}
            onChange={(e) => setKind(e.target.value as StoryKind)}
            className="max-w-72 flex-1 truncate rounded-lg border border-zinc-700 bg-zinc-900 px-2.5 py-1.5 text-xs text-zinc-200 outline-none focus:border-zinc-500"
            title="code: the full pipeline — spec, AI review, then a coder agent in a worktree whose branch is merged on approval. info: the spec/document is the deliverable; approval finishes the story, no code runs. prep: the deliverable is child stories; approval finishes once at least one child links back via depends_on."
          >
            <option value="code">code — coder in a worktree, merge on approve</option>
            <option value="info">info — the document is the deliverable</option>
            <option value="prep">prep — child stories are the deliverable</option>
          </select>
        </label>

        <div className="flex flex-wrap items-end gap-2">
          <label className="flex flex-1 items-center gap-2 text-xs text-zinc-500">
            <span className="shrink-0">spec gate</span>
            <select
              value={gateSpec}
              onChange={(e) => setGateSpec(e.target.value as "inherit" | StoryGate)}
              className="min-w-0 flex-1 truncate rounded-lg border border-zinc-700 bg-zinc-900 px-2.5 py-1.5 text-xs text-zinc-200 outline-none focus:border-zinc-500"
              title="Who approves the spec after the AI review: inherit = this project's default gate (Projects page), human = you, auto = machine checks only (AI review approved and no human rejection ever recorded)."
            >
              <option value="inherit">inherit — project default</option>
              <option value="human">human — you approve</option>
              <option value="auto">auto — machine checks</option>
            </select>
          </label>
          <label className="flex flex-1 items-center gap-2 text-xs text-zinc-500">
            <span className="shrink-0">code gate</span>
            <select
              value={gateCode}
              onChange={(e) => setGateCode(e.target.value as "inherit" | StoryGate)}
              className="min-w-0 flex-1 truncate rounded-lg border border-zinc-700 bg-zinc-900 px-2.5 py-1.5 text-xs text-zinc-200 outline-none focus:border-zinc-500"
              title="Who approves the coded work after verify + AI code review: inherit = this project's default gate, human = you, auto = machine checks only (verify green, AI review approved, no protected/frozen paths, diff within the project cap, and no human rejection ever recorded)."
            >
              <option value="inherit">inherit — project default</option>
              <option value="human">human — you approve</option>
              <option value="auto">auto — machine checks</option>
            </select>
          </label>
        </div>

        <div className="flex justify-end gap-2 pt-1">
          <button
            onClick={onClose}
            className="rounded-lg px-3 py-1.5 text-sm text-zinc-400 transition hover:text-zinc-200"
          >
            Cancel
          </button>
          <button
            onClick={submit}
            disabled={!title.trim() || busy}
            className="rounded-lg bg-zinc-100 px-4 py-1.5 text-sm font-medium text-zinc-900 transition hover:bg-white disabled:cursor-not-allowed disabled:opacity-30"
          >
            {busy ? "Creating…" : "Create story"}
          </button>
        </div>
      </div>
    </div>
  );
}
