import { useState } from "react";

import { FolderPicker } from "@/components/FolderPicker";
import { ProjectBrief } from "@/components/ProjectBrief";
import { useStore } from "@/store";
import type { Project, ProjectDelivery, StoryExecution, StoryGate } from "@/types";

function ProjectCard({ project }: { project: Project }) {
  const patchProject = useStore((s) => s.patchProject);
  const deleteProject = useStore((s) => s.deleteProject);
  const [extras, setExtras] = useState(project.extra_paths.join("\n"));
  const [name, setName] = useState(project.name);
  const [root, setRoot] = useState(project.root);
  const [verify, setVerify] = useState(project.verify_command ?? "");
  const [executor, setExecutor] = useState<StoryExecution>(project.executor ?? "internal");
  const [delivery, setDelivery] = useState<ProjectDelivery>(project.delivery ?? "merge");
  const [protectedGlobs, setProtectedGlobs] = useState(project.protected_globs ?? "");
  const [frozenGlobs, setFrozenGlobs] = useState(project.frozen_globs ?? "");
  const [maxDiff, setMaxDiff] = useState(String(project.auto_max_diff_lines ?? 400));
  const [prApiBase, setPrApiBase] = useState(project.pr_api_base ?? "");
  const [prWebBase, setPrWebBase] = useState(project.pr_web_base ?? "");
  const [prRepo, setPrRepo] = useState(project.pr_repo ?? "");
  const [prBaseBranch, setPrBaseBranch] = useState(project.pr_base_branch ?? "");
  const [prTokenFile, setPrTokenFile] = useState(project.pr_token_file ?? "");
  const [abShare, setAbShare] = useState(String(project.ab_headless_share ?? 0));
  const [browsing, setBrowsing] = useState(false);
  const dirty = name !== project.name || root !== project.root;

  const commit = () => {
    const extra_paths = extras
      .split("\n")
      .map((p) => p.trim())
      .filter(Boolean);
    const body: Parameters<typeof patchProject>[1] = { extra_paths };
    if (name !== project.name) body.name = name;
    if (root !== project.root) body.root = root;
    if ((verify || "") !== (project.verify_command ?? "")) body.verify_command = verify;
    if ((prApiBase || "") !== (project.pr_api_base ?? "")) body.pr_api_base = prApiBase || undefined;
    if ((prWebBase || "") !== (project.pr_web_base ?? "")) body.pr_web_base = prWebBase || undefined;
    if ((prRepo || "") !== (project.pr_repo ?? "")) body.pr_repo = prRepo || undefined;
    if ((prBaseBranch || "") !== (project.pr_base_branch ?? "")) body.pr_base_branch = prBaseBranch || undefined;
    if ((prTokenFile || "") !== (project.pr_token_file ?? "")) body.pr_token_file = prTokenFile || undefined;
    patchProject(project.id, body);
  };

  /** Executor is a toggle, not a blur-commit field: it flips the default for
   *  every inherit-story in the project (read-time resolution on the server),
   *  so a misclick should not silently rewrite the board's routing. */
  const setProjectExecutor = (next: StoryExecution) => {
    if (next === (project.executor ?? "internal")) return;
    setExecutor(next);
    patchProject(project.id, { executor: next });
  };

  /** Same immediate-commit semantics as executor: delivery changes where
   *  approved code lands, and a stray blur should not flip it. */
  const setProjectDelivery = (next: ProjectDelivery) => {
    if (next === (project.delivery ?? "merge")) return;
    setDelivery(next);
    patchProject(project.id, { delivery: next });
  };

  /** Gate defaults: immediate-commit like executor/delivery — flipping the
   *  default re-routes future stories only, never existing ones. */
  const setProjectGate = (field: "default_gate_spec" | "default_gate_code", next: StoryGate) => {
    if (next === (project[field] ?? "human")) return;
    patchProject(project.id, { [field]: next });
  };

  /** Blur-committed glob lists + diff cap for the CODE auto gate. */
  const commitGates = () => {
    const body: Parameters<typeof patchProject>[1] = {};
    if ((protectedGlobs || "") !== (project.protected_globs ?? ""))
      body.protected_globs = protectedGlobs;
    if ((frozenGlobs || "") !== (project.frozen_globs ?? ""))
      body.frozen_globs = frozenGlobs;
    const parsed = Number.parseInt(maxDiff, 10);
    const nextMax = Number.isFinite(parsed) && parsed >= 0 ? parsed : 400;
    if (String(nextMax) !== String(project.auto_max_diff_lines ?? 400)) {
      setMaxDiff(String(nextMax));
      body.auto_max_diff_lines = nextMax;
    }
    if (Object.keys(body).length) patchProject(project.id, body);
  };

  /** A/B share: committed on blur/Enter, clamped to 0..1. 0 disables the
    *  split (the default) — every new story then stays on the pump. */
  const commitAbShare = () => {
    const parsed = Number.parseFloat(abShare);
    const next = Number.isFinite(parsed) ? Math.min(1, Math.max(0, parsed)) : 0;
    const value = Number(next.toFixed(4));
    setAbShare(String(value));
    if (value !== (project.ab_headless_share ?? 0)) {
      patchProject(project.id, { ab_headless_share: value });
    }
  };

  return (
    <div className="rounded-xl border border-zinc-800/80 bg-zinc-900/40 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          onBlur={commit}
          className="min-w-40 flex-1 rounded-lg border border-zinc-700/70 bg-zinc-900 px-2.5 py-1.5 text-sm font-medium text-zinc-200 outline-none focus:border-zinc-500"
        />
        <span
          className={`rounded-full px-2 py-0.5 text-[10px] font-medium ${
            project.root_is_repo
              ? "bg-emerald-950/80 text-emerald-300"
              : "bg-zinc-800 text-zinc-400"
          }`}
          title={project.root_is_repo ? "git repo — worktree isolation available" : "not a git repo"}
        >
          {project.root_is_repo ? "git ✓" : "no git"}
        </span>
        <span className="text-[11px] text-zinc-500">{project.story_count} stories</span>
        <button
          onClick={() => confirm(`Delete project "${project.name}"? Stories are kept, unassigned.`) && deleteProject(project.id)}
          className="ml-auto text-[11px] text-zinc-600 transition hover:text-red-400"
        >
          delete
        </button>
      </div>

      <label className="mt-3 flex items-center gap-2">
        <span className="w-14 shrink-0 text-[10px] font-semibold tracking-wide text-zinc-500 uppercase">
          root
        </span>
        <input
          value={root}
          onChange={(e) => setRoot(e.target.value)}
          onBlur={commit}
          className="flex-1 rounded-lg border border-zinc-700/70 bg-zinc-900 px-2.5 py-1.5 font-mono text-xs text-zinc-200 outline-none focus:border-zinc-500"
        />
        <button
          onClick={() => setBrowsing(true)}
          className="shrink-0 rounded-lg border border-zinc-700 px-2 py-1.5 text-[11px] text-zinc-300 transition hover:bg-zinc-800"
          title="Browse the filesystem to set this project's folder"
        >
          Browse…
        </button>
        {!project.root_exists && (
          <span className="text-[10px] text-amber-400">path missing</span>
        )}
      </label>

      <label className="mt-2 block">
        <span className="text-[10px] font-semibold tracking-wide text-zinc-500 uppercase">
          extra allowed paths (one per line — the soft guardrail's approved exits)
        </span>
        <textarea
          rows={Math.max(2, extras.split("\n").length)}
          value={extras}
          onChange={(e) => setExtras(e.target.value)}
          onBlur={commit}
          spellCheck={false}
          placeholder={"/etc/ssl/certs\n../shared-libs"}
          className="mt-1 w-full resize-y rounded-lg border border-zinc-700/70 bg-zinc-900 px-2.5 py-1.5 font-mono text-xs text-zinc-200 placeholder-zinc-600 outline-none focus:border-zinc-500"
        />
      </label>

      <label className="mt-2 block">
        <span className="text-[10px] font-semibold tracking-wide text-zinc-500 uppercase">
          verify command override (empty = app default)
        </span>
        <input
          value={verify ?? ""}
          onChange={(e) => setVerify(e.target.value)}
          onBlur={commit}
          spellCheck={false}
          placeholder="e.g. cd backend_root && uv run pytest -q"
          className="mt-1 w-full rounded-lg border border-zinc-700/70 bg-zinc-900 px-2.5 py-1.5 font-mono text-xs text-zinc-200 placeholder-zinc-600 outline-none focus:border-zinc-500"
        />
      </label>

      <label className="mt-2 flex items-center gap-2">
        <span className="w-14 shrink-0 text-[10px] font-semibold tracking-wide text-zinc-500 uppercase">
          executor
        </span>
        <select
          value={executor}
          onChange={(e) => setProjectExecutor(e.target.value as StoryExecution)}
          className="flex-1 rounded-lg border border-zinc-700/70 bg-zinc-900 px-2.5 py-1.5 text-xs text-zinc-200 outline-none focus:border-zinc-500"
          title="internal: Fubala's own pipeline runs this project's stories. external: they wait for an outside agent to claim them (claim/complete API or MCP). headless: the CODE stage runs via the Claude Code CLI in the story worktree; spec, verify, review and gates are unchanged. Stories with their own explicit setting override this default; existing stories are never rewritten — inheritance is resolved at read time."
        >
          <option value="internal">internal — Fubala runner (spec → review → coder)</option>
          <option value="external">external — outside agents claim stories (PR + CI)</option>
          <option value="headless">headless — Claude Code CLI runs the CODE stage</option>
        </select>
        {executor === "external" && (
          <span className="shrink-0 text-[10px] text-amber-400/80">inherit-stories wait for claim</span>
        )}
      </label>

      <label className="mt-2 flex items-center gap-2">
        <span className="w-14 shrink-0 text-[10px] font-semibold tracking-wide text-zinc-500 uppercase">
          A/B headless
        </span>
        <input
          type="number"
          min={0}
          max={1}
          step={0.05}
          value={abShare}
          onChange={(e) => setAbShare(e.target.value)}
          onBlur={commitAbShare}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              commitAbShare();
            }
          }}
          className="w-20 rounded-lg border border-zinc-700/70 bg-zinc-900 px-2.5 py-1.5 text-xs text-zinc-200 outline-none focus:border-zinc-500"
          title="Share of this project's NEW stories routed to the headless CODE executor when they enter queued with no explicit override (0..1). Deterministic per story id, so restarts never flip a story. 0 = never (default)."
        />
        <span className="shrink-0 text-[10px] text-zinc-500">
          share of new stories on the headless CODE executor (0..1, 0 = off)
        </span>
      </label>

      <label className="mt-2 flex items-center gap-2">
        <span className="w-14 shrink-0 text-[10px] font-semibold tracking-wide text-zinc-500 uppercase">
          code graph
        </span>
        <input
          type="checkbox"
          checked={!!project.code_graph_enabled}
          onChange={(e) => patchProject(project.id, { code_graph_enabled: e.target.checked })}
          title="code-review-graph: an impact report at every code review, and a code graph MCP server for CLI sessions. Graph data lives outside the repo."
        />
        <span className="shrink-0 text-[10px] text-zinc-500">
          impact report at every code review; the MCP server applies to CLI sessions only
        </span>
      </label>

      <label className="mt-2 flex items-center gap-2">
        <span className="w-14 shrink-0 text-[10px] font-semibold tracking-wide text-zinc-500 uppercase">
          delivery
        </span>
        <select
          value={delivery}
          onChange={(e) => setProjectDelivery(e.target.value as ProjectDelivery)}
          className="flex-1 rounded-lg border border-zinc-700/70 bg-zinc-900 px-2.5 py-1.5 text-xs text-zinc-200 outline-none focus:border-zinc-500"
          title="merge: approving merges the story branch into this project's root checkout (the original behavior). push: approving pushes the branch fubala/<story> to the remote and — when all four PR fields are set — opens a Gitea pull request. The root checkout is never touched."
        >
          <option value="merge">merge — into the project root checkout</option>
          <option value="push">push — branch to remote (+ optional Gitea PR)</option>
        </select>
        {delivery === "push" && (
          <span className="shrink-0 text-[10px] text-amber-400/80">no self-update</span>
        )}
      </label>

      <div className="mt-2 space-y-2 rounded-lg border border-zinc-800/60 bg-zinc-950/40 p-2.5">
        <p className="text-[10px] leading-relaxed text-zinc-500">
          Gates decide who approves this project&apos;s new stories: a human, or
          machine checks. Changing a default never re-routes stories that
          already exist — the gate is stamped at create.
        </p>
        <div className="flex flex-wrap items-center gap-2">
          <label className="flex flex-1 items-center gap-2">
            <span className="w-14 shrink-0 text-[10px] font-semibold tracking-wide text-zinc-500 uppercase">
              spec gate
            </span>
            <select
              value={project.default_gate_spec ?? "human"}
              onChange={(e) =>
                setProjectGate("default_gate_spec", e.target.value as StoryGate)
              }
              className="flex-1 rounded-lg border border-zinc-700/70 bg-zinc-900 px-2 py-1.5 text-xs text-zinc-200 outline-none focus:border-zinc-500"
              title="Default SPEC gate for new stories in this project: human = the operator approves after the AI review; auto = machine checks only (AI review approved and no human rejection ever recorded on the story), any failed check parks the story at the human gate."
            >
              <option value="human">human (default)</option>
              <option value="auto">auto — machine checks</option>
            </select>
          </label>
          <label className="flex flex-1 items-center gap-2">
            <span className="w-14 shrink-0 text-[10px] font-semibold tracking-wide text-zinc-500 uppercase">
              code gate
            </span>
            <select
              value={project.default_gate_code ?? "human"}
              onChange={(e) =>
                setProjectGate("default_gate_code", e.target.value as StoryGate)
              }
              className="flex-1 rounded-lg border border-zinc-700/70 bg-zinc-900 px-2 py-1.5 text-xs text-zinc-200 outline-none focus:border-zinc-500"
              title="Default CODE gate for new stories in this project: human = the operator approves after verify + AI code review; auto = machine checks only (verify green, AI review approved, no protected/frozen paths, diff within the cap, and no human rejection ever recorded), any failed check parks the story at the human gate."
            >
              <option value="human">human (default)</option>
              <option value="auto">auto — machine checks</option>
            </select>
          </label>
        </div>
        <label className="flex items-center gap-2">
          <span className="w-24 shrink-0 text-[10px] font-semibold tracking-wide text-zinc-500 uppercase">
            protected
          </span>
          <input
            value={protectedGlobs}
            onChange={(e) => setProtectedGlobs(e.target.value)}
            onBlur={commitGates}
            spellCheck={false}
            placeholder="backend/app/auth.py, .gitea/"
            className="flex-1 rounded-lg border border-zinc-700/70 bg-zinc-900 px-2.5 py-1.5 font-mono text-xs text-zinc-200 placeholder-zinc-600 outline-none focus:border-zinc-500"
            title="Comma-separated globs. A diff touching any of these paths fails the CODE auto gate's no_protected_paths check and parks the story at the human gate. A directory name also matches everything below it."
          />
        </label>
        <label className="flex items-center gap-2">
          <span className="w-24 shrink-0 text-[10px] font-semibold tracking-wide text-zinc-500 uppercase">
            frozen
          </span>
          <input
            value={frozenGlobs}
            onChange={(e) => setFrozenGlobs(e.target.value)}
            onBlur={commitGates}
            spellCheck={false}
            placeholder="docs/, docker/"
            className="flex-1 rounded-lg border border-zinc-700/70 bg-zinc-900 px-2.5 py-1.5 font-mono text-xs text-zinc-200 placeholder-zinc-600 outline-none focus:border-zinc-500"
            title="Comma-separated globs. A diff touching any of these paths fails the CODE auto gate's no_frozen_paths check and parks the story at the human gate. Use this for paths an auto story must never touch at all."
          />
        </label>
        <label className="flex items-center gap-2">
          <span className="w-24 shrink-0 text-[10px] font-semibold tracking-wide text-zinc-500 uppercase">
            max diff
          </span>
          <input
            type="number"
            min={0}
            value={maxDiff}
            onChange={(e) => setMaxDiff(e.target.value)}
            onBlur={commitGates}
            className="w-24 rounded-lg border border-zinc-700/70 bg-zinc-900 px-2.5 py-1.5 text-xs text-zinc-200 outline-none focus:border-zinc-500"
            title="Cap on added+removed diff lines for the CODE auto gate (diff_within_max). A bigger diff parks the story at the human gate. 0 = no cap."
          />
          <span className="text-[10px] text-zinc-500">
            lines (added+removed; 0 = no cap)
          </span>
        </label>
      </div>

      {delivery === "push" && (
        <div className="mt-2 space-y-2 rounded-lg border border-zinc-800/60 bg-zinc-950/40 p-2.5">
          <p className="text-[10px] leading-relaxed text-zinc-500">
            All four fields filled → approving also opens a pull request on
            Gitea. Leave any blank for push-only (no PR). The token file is a
            path on this machine; its contents are read at approval time and
            never stored or logged.
          </p>
          <label className="flex items-center gap-2">
            <span className="w-20 shrink-0 text-[10px] font-semibold tracking-wide text-zinc-500 uppercase">
              api base
            </span>
            <input
              value={prApiBase}
              onChange={(e) => setPrApiBase(e.target.value)}
              onBlur={commit}
              spellCheck={false}
              placeholder="https://git.example.com"
              className="flex-1 rounded-lg border border-zinc-700/70 bg-zinc-900 px-2.5 py-1.5 font-mono text-xs text-zinc-200 placeholder-zinc-600 outline-none focus:border-zinc-500"
            />
          </label>
          <label className="flex items-center gap-2">
            <span className="w-20 shrink-0 text-[10px] font-semibold tracking-wide text-zinc-500 uppercase">
              web base
            </span>
            <input
              value={prWebBase}
              onChange={(e) => setPrWebBase(e.target.value)}
              onBlur={commit}
              spellCheck={false}
              placeholder="optional public URL for PR links"
              className="flex-1 rounded-lg border border-zinc-700/70 bg-zinc-900 px-2.5 py-1.5 font-mono text-xs text-zinc-200 placeholder-zinc-600 outline-none focus:border-zinc-500"
            />
          </label>
          <label className="flex items-center gap-2">
            <span className="w-20 shrink-0 text-[10px] font-semibold tracking-wide text-zinc-500 uppercase">
              repo
            </span>
            <input
              value={prRepo}
              onChange={(e) => setPrRepo(e.target.value)}
              onBlur={commit}
              spellCheck={false}
              placeholder="owner/name"
              className="flex-1 rounded-lg border border-zinc-700/70 bg-zinc-900 px-2.5 py-1.5 font-mono text-xs text-zinc-200 placeholder-zinc-600 outline-none focus:border-zinc-500"
            />
          </label>
          <label className="flex items-center gap-2">
            <span className="w-20 shrink-0 text-[10px] font-semibold tracking-wide text-zinc-500 uppercase">
              base branch
            </span>
            <input
              value={prBaseBranch}
              onChange={(e) => setPrBaseBranch(e.target.value)}
              onBlur={commit}
              spellCheck={false}
              placeholder="main"
              className="flex-1 rounded-lg border border-zinc-700/70 bg-zinc-900 px-2.5 py-1.5 font-mono text-xs text-zinc-200 placeholder-zinc-600 outline-none focus:border-zinc-500"
            />
          </label>
          <label className="flex items-center gap-2">
            <span className="w-20 shrink-0 text-[10px] font-semibold tracking-wide text-zinc-500 uppercase">
              token file
            </span>
            <input
              value={prTokenFile}
              onChange={(e) => setPrTokenFile(e.target.value)}
              onBlur={commit}
              spellCheck={false}
              placeholder="/etc/fubala/gitea-token"
              className="flex-1 rounded-lg border border-zinc-700/70 bg-zinc-900 px-2.5 py-1.5 font-mono text-xs text-zinc-200 placeholder-zinc-600 outline-none focus:border-zinc-500"
            />
          </label>
          <div className="flex flex-wrap items-center gap-2 pt-1">
            <button
              onClick={() =>
                patchProject(project.id, { merge_train: !project.merge_train })
              }
              className={`rounded-lg px-2.5 py-1 text-[11px] font-medium transition ${
                project.merge_train
                  ? "bg-emerald-950/80 text-emerald-300 hover:bg-emerald-900/70"
                  : "bg-zinc-800 text-zinc-400 hover:bg-zinc-700"
              }`}
              title={
                "Merge train: gate-approved stories are batched into ONE pull " +
                "request per window; CI runs once per batch, a red batch gets " +
                "one flake re-run and is then bisected. Needs push delivery " +
                "and all four pr_* fields."
              }
            >
              {project.merge_train ? "train on" : "train off"}
            </button>
            <label className="flex items-center gap-1.5">
              <span className="text-[10px] text-zinc-500">window</span>
              <input
                type="number"
                min={1}
                defaultValue={String(project.train_window_s ?? 300)}
                onBlur={(e) => {
                  const parsed = Number.parseInt(e.target.value, 10);
                  const next = Number.isFinite(parsed) && parsed >= 1 ? parsed : 300;
                  if (next !== (project.train_window_s ?? 300)) {
                    patchProject(project.id, { train_window_s: next });
                  }
                }}
                className="w-16 rounded-lg border border-zinc-700/70 bg-zinc-900 px-2 py-1 text-xs text-zinc-200 outline-none focus:border-zinc-500"
                title="Seconds a forming batch waits for more members before it flushes"
              />
              <span className="text-[10px] text-zinc-500">s</span>
            </label>
            <label className="flex items-center gap-1.5">
              <span className="text-[10px] text-zinc-500">max</span>
              <input
                type="number"
                min={1}
                defaultValue={String(project.train_max ?? 6)}
                onBlur={(e) => {
                  const parsed = Number.parseInt(e.target.value, 10);
                  const next = Number.isFinite(parsed) && parsed >= 1 ? parsed : 6;
                  if (next !== (project.train_max ?? 6)) {
                    patchProject(project.id, { train_max: next });
                  }
                }}
                className="w-16 rounded-lg border border-zinc-700/70 bg-zinc-900 px-2 py-1 text-xs text-zinc-200 outline-none focus:border-zinc-500"
                title="Members at which a forming batch flushes immediately"
              />
            </label>
          </div>
        </div>
      )}

      <ProjectBrief project={project} />

      {dirty && <p className="mt-1 text-[11px] text-amber-400/80">changes apply on blur</p>}

      {browsing && (
        <FolderPicker
          startPath={root}
          onCancel={() => setBrowsing(false)}
          onPick={(path) => {
            setBrowsing(false);
            setRoot(path);
            if (name === project.name) {
              setName(path.split("/").filter(Boolean).pop() ?? name);
            }
            patchProject(project.id, { root: path });
          }}
        />
      )}
    </div>
  );
}

export function ProjectsView() {
  const projects = useStore((s) => s.projects);
  const createProject = useStore((s) => s.createProject);
  const setView = useStore((s) => s.setView);
  const [name, setName] = useState("");
  const [root, setRoot] = useState("");
  const [busy, setBusy] = useState(false);
  const [browsing, setBrowsing] = useState(false);

  const submit = async () => {
    if (!name.trim() || !root.trim() || busy) return;
    setBusy(true);
    try {
      await createProject({ name: name.trim(), root: root.trim() });
      setName("");
      setRoot("");
    } catch (e) {
      alert((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      <header className="flex h-12 shrink-0 items-center justify-between border-b border-zinc-800/70 px-4">
        <h1 className="text-sm font-medium text-zinc-300">
          Projects <span className="ml-2 text-xs text-zinc-500">{projects.length}</span>
        </h1>
        <button
          onClick={() => setView("chat")}
          className="rounded-lg px-3 py-1.5 text-xs text-zinc-400 transition hover:bg-zinc-800/60 hover:text-zinc-200"
        >
          ← Back to chat
        </button>
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-6">
        <div className="mx-auto w-full max-w-3xl space-y-3">
          <p className="text-sm leading-relaxed text-zinc-400">
            A project is a workspace (zcode-style): its root directory is the
            agent's guardrail — file tools, the coder's worktree, and merges all
            live inside it. Escaping to other paths requires a live permission
            you grant (per mode in Application settings). Approved exits are
            remembered as extra allowed paths.
          </p>

          {projects.map((p) => (
            <ProjectCard key={p.id} project={p} />
          ))}

          <div className="rounded-xl border border-dashed border-zinc-700 p-4">
            <h3 className="mb-2 text-xs font-semibold tracking-wide text-zinc-500 uppercase">
              Add project
            </h3>
            <div className="flex flex-wrap gap-2">
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="Name"
                className="w-40 rounded-lg border border-zinc-700/70 bg-zinc-900 px-2.5 py-1.5 text-xs text-zinc-200 placeholder-zinc-600 outline-none focus:border-zinc-500"
              />
              <input
                value={root}
                onChange={(e) => setRoot(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && submit()}
                placeholder="/absolute/path/to/repo"
                className="min-w-64 flex-1 rounded-lg border border-zinc-700/70 bg-zinc-900 px-2.5 py-1.5 font-mono text-xs text-zinc-200 placeholder-zinc-600 outline-none focus:border-zinc-500"
              />
              <button
                onClick={() => setBrowsing(true)}
                className="rounded-lg border border-zinc-700 px-2.5 py-1.5 text-xs text-zinc-300 transition hover:bg-zinc-800"
              >
                Browse…
              </button>
              <button
                onClick={submit}
                disabled={!name.trim() || !root.trim() || busy}
                className="rounded-lg bg-zinc-100 px-3 py-1.5 text-xs font-medium text-zinc-900 transition hover:bg-white disabled:opacity-30"
              >
                Add
              </button>
            </div>
          {browsing && (
            <FolderPicker
              onCancel={() => setBrowsing(false)}
              onPick={(path) => {
                setBrowsing(false);
                setRoot(path);
                if (!name.trim()) setName(path.split("/").filter(Boolean).pop() ?? "");
              }}
            />
          )}
          </div>
        </div>
      </div>
    </div>
  );
}
