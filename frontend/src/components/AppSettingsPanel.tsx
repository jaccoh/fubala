import { useEffect, useState } from "react";

import { api } from "@/api";
import { useStore } from "@/store";
import type { AppSettings, ModelsResponse, Tier } from "@/types";

function NumberField({
  label,
  value,
  onCommit,
  min,
  max,
  step = 1,
  hint,
}: {
  label: string;
  value: number;
  onCommit: (v: number) => void;
  min: number;
  max: number;
  step?: number;
  hint?: string;
}) {
  const [text, setText] = useState(String(value));
  useEffect(() => setText(String(value)), [value]);
  return (
    <label className="block space-y-1">
      <span className="text-[11px] font-semibold tracking-wide text-zinc-500 uppercase">
        {label}
      </span>
      <input
        type="number"
        value={text}
        min={min}
        max={max}
        step={step}
        onChange={(e) => setText(e.target.value)}
        onBlur={() => {
          const n = Number(text);
          if (!Number.isNaN(n) && n >= min && n <= max && n !== value) onCommit(n);
          else setText(String(value));
        }}
        className="w-28 rounded-lg border border-zinc-700/70 bg-zinc-900 px-2.5 py-1.5 font-mono text-xs text-zinc-200 outline-none focus:border-zinc-500"
      />
      {hint && <span className="block text-[10px] text-zinc-600">{hint}</span>}
    </label>
  );
}

type WorkerResourceKey =
  | "worker_cpu_request"
  | "worker_cpu_limit"
  | "worker_memory_request"
  | "worker_memory_limit";

/** A Kubernetes quantity input: commits on blur, shows the API error. */
function QuantityField({ label, field, hint }: { label: string; field: WorkerResourceKey; hint: string }) {
  const settings = useStore((s) => s.appSettings);
  const patch = useStore((s) => s.patchAppSettings);
  const value = settings?.[field] ?? "";
  const [text, setText] = useState(value);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => setText(value), [value]);
  return (
    <label className="block space-y-1">
      <span className="text-[11px] font-semibold tracking-wide text-zinc-500 uppercase">
        {label}
      </span>
      <input
        type="text"
        value={text}
        onChange={(e) => setText(e.target.value)}
        onBlur={async () => {
          const v = text.trim();
          if (v === value) {
            setText(value);
            setError(null);
            return;
          }
          try {
            await patch({ [field]: v } as Partial<AppSettings>);
            setError(null);
          } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
            setText(value);
          }
        }}
        className="w-28 rounded-lg border border-zinc-700/70 bg-zinc-900 px-2.5 py-1.5 font-mono text-xs text-zinc-200 outline-none focus:border-zinc-500"
      />
      <span className="block text-[10px] text-zinc-600">{hint}</span>
      {error && <span className="block text-[10px] text-red-400">{error}</span>}
    </label>
  );
}

function PromptEditor({
  field,
  title,
  hint,
}: {
  field: "spec_writer_prompt" | "ai_reviewer_prompt";
  title: string;
  hint: string;
}) {
  const settings = useStore((s) => s.appSettings);
  const patch = useStore((s) => s.patchAppSettings);
  const [text, setText] = useState("");
  const [defaults, setDefaults] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    import("@/api").then(({ api }) =>
      api.getPromptDefaults().then((d) => {
        setDefaults(d[field]);
        setText(settings?.[field] ?? d[field]);
      }),
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const active = settings?.[field] ?? null;
  const dirty = text !== (active ?? defaults ?? "");

  return (
    <div className="space-y-2 rounded-xl border border-zinc-800/80 bg-zinc-900/40 p-4">
      <div className="flex items-center justify-between gap-2">
        <div>
          <h3 className="text-sm font-medium text-zinc-200">{title}</h3>
          <p className="text-[11px] text-zinc-600">{hint}</p>
        </div>
        {active && (
          <span className="rounded-full bg-amber-950/70 px-2 py-0.5 text-[10px] text-amber-300">
            customized
          </span>
        )}
      </div>
      <textarea
        rows={10}
        value={text}
        onChange={(e) => setText(e.target.value)}
        spellCheck={false}
        className="w-full resize-y rounded-lg border border-zinc-700/70 bg-zinc-950 p-3 font-mono text-[11px] leading-relaxed text-zinc-300 outline-none focus:border-zinc-500"
      />
      <div className="flex items-center gap-2">
        <button
          disabled={!dirty || busy}
          onClick={async () => {
            setBusy(true);
            try {
              await patch({ [field]: text } as Partial<AppSettings>);
            } finally {
              setBusy(false);
            }
          }}
          className="rounded-lg bg-zinc-100 px-3 py-1.5 text-xs font-medium text-zinc-900 transition hover:bg-white disabled:opacity-30"
        >
          {busy ? "Saving…" : "Save prompt"}
        </button>
        <button
          disabled={!active || busy}
          onClick={async () => {
            setBusy(true);
            try {
              await patch({ [field]: "" } as Partial<AppSettings>);
              setText(defaults ?? "");
            } finally {
              setBusy(false);
            }
          }}
          className="rounded-lg px-3 py-1.5 text-xs text-zinc-400 transition hover:text-zinc-200 disabled:opacity-30"
        >
          Reset to default
        </button>
        {dirty && <span className="text-[11px] text-amber-400/80">unsaved changes</span>}
      </div>
    </div>
  );
}

/** A NumberField whose empty value means "use the default" (sent as null). */
function OptionalNumberField({
  label,
  value,
  onCommit,
  min,
  max,
  step = 1,
  hint,
  placeholder,
}: {
  label: string;
  value: number | null;
  onCommit: (v: number | null) => void;
  min: number;
  max: number;
  step?: number;
  hint?: string;
  placeholder?: string;
}) {
  const [text, setText] = useState(value == null ? "" : String(value));
  useEffect(() => setText(value == null ? "" : String(value)), [value]);
  return (
    <label className="block space-y-1">
      <span className="text-[11px] font-semibold tracking-wide text-zinc-500 uppercase">
        {label}
      </span>
      <input
        type="number"
        value={text}
        min={min}
        max={max}
        step={step}
        placeholder={placeholder}
        onChange={(e) => setText(e.target.value)}
        onBlur={() => {
          if (text.trim() === "") {
            if (value !== null) onCommit(null);
            return;
          }
          const n = Number(text);
          if (!Number.isNaN(n) && n >= min && n <= max && n !== value) onCommit(n);
          else setText(value == null ? "" : String(value));
        }}
        className="w-28 rounded-lg border border-zinc-700/70 bg-zinc-900 px-2.5 py-1.5 font-mono text-xs text-zinc-200 outline-none focus:border-zinc-500"
      />
      {hint && <span className="block text-[10px] text-zinc-600">{hint}</span>}
    </label>
  );
}

export function AppSettingsPanel() {
  const settings = useStore((s) => s.appSettings);
  const models = useStore((s) => s.models);
  const providers = useStore((s) => s.providers);
  const sessions = useStore((s) => s.sessions);
  const stories = useStore((s) => s.stories);
  const patch = useStore((s) => s.patchAppSettings);
  const refreshSettings = useStore((s) => s.refreshSettings);
  const [chatPrompt, setChatPrompt] = useState("");
  const [chatDirty, setChatDirty] = useState(false);
  const [freshToken, setFreshToken] = useState<string | null>(null);
  const [tokenBusy, setTokenBusy] = useState(false);
  const [tokenError, setTokenError] = useState<string | null>(null);

  useEffect(() => {
    setChatPrompt(settings?.chat_system_prompt ?? "");
    setChatDirty(false);
  }, [settings?.chat_system_prompt]);

  if (!settings) return null;
  const configured = (models as ModelsResponse).providers.filter((p) => p.configured);

  return (
    <div className="mx-auto w-full max-w-3xl space-y-3">
      {/* general */}
      <div className="space-y-4 rounded-xl border border-zinc-800/80 bg-zinc-900/40 p-4">
        <h3 className="text-sm font-medium text-zinc-200">General</h3>

        <div className="flex flex-wrap items-end gap-6">
          <label className="block space-y-1">
            <span className="text-[11px] font-semibold tracking-wide text-zinc-500 uppercase">
              Default model
            </span>
            <select
              value={settings.default_model ?? ""}
              onChange={(e) => patch({ default_model: e.target.value || null })}
              className="max-w-72 rounded-lg border border-zinc-700/70 bg-zinc-900 px-2.5 py-1.5 font-mono text-xs text-zinc-200 outline-none focus:border-zinc-500"
            >
              <option value="">Auto — cheapest keyed medium-skill</option>
              {configured.map((p) => (
                <optgroup key={p.id} label={p.display_name}>
                  {p.models.map((m) => (
                    <option key={m.id} value={m.id}>
                      {m.id} · {m.skill} · {"$".repeat({ low: 1, medium: 2, high: 3 }[m.cost as Tier] ?? 2)}
                    </option>
                  ))}
                </optgroup>
              ))}
            </select>
          </label>

          <NumberField
            label="Parallel stories"
            value={settings.max_parallel_stories}
            min={1}
            max={16}
            hint="stories spec-generating at once"
            onCommit={(v) => patch({ max_parallel_stories: v })}
          />
          <NumberField
            label="Queue poll (s)"
            value={settings.story_poll_interval}
            min={0.05}
            max={60}
            step={0.05}
            hint="how often the queue is scanned"
            onCommit={(v) => patch({ story_poll_interval: v })}
          />
          <NumberField
            label="External WIP / project"
            value={settings.max_external_wip_per_project}
            min={1}
            max={16}
            hint="claimed external stories in progress per project"
            onCommit={(v) => patch({ max_external_wip_per_project: v })}
          />
          <NumberField
            label="Headless timeout (s)"
            value={settings.headless_timeout_seconds}
            min={30}
            max={7200}
            hint="wall-clock bound for one headless Claude Code CODE stage"
            onCommit={(v) => patch({ headless_timeout_seconds: v })}
          />
          <QuantityField
            label="Worker CPU request"
            field="worker_cpu_request"
            hint="worker pod, applies to the next run (100m-16)"
          />
          <QuantityField
            label="Worker CPU limit"
            field="worker_cpu_limit"
            hint="worker pod, applies to the next run (100m-16)"
          />
          <QuantityField
            label="Worker memory request"
            field="worker_memory_request"
            hint="worker pod, applies to the next run (256Mi-32Gi)"
          />
          <QuantityField
            label="Worker memory limit"
            field="worker_memory_limit"
            hint="worker pod, applies to the next run (256Mi-32Gi)"
          />
        </div>

        <label className="block space-y-1">
          <span className="text-[11px] font-semibold tracking-wide text-zinc-500 uppercase">
            Project root
          </span>
          <input
            defaultValue={settings.project_root ?? ""}
            key={settings.project_root ?? "unset"}
            placeholder="/path/to/your/project — enables read-only code tools"
            onBlur={(e) => {
              const v = e.target.value.trim();
              if (v !== (settings.project_root ?? "")) patch({ project_root: v });
            }}
            className="w-full max-w-xl rounded-lg border border-zinc-700/70 bg-zinc-900 px-2.5 py-1.5 font-mono text-xs text-zinc-200 placeholder-zinc-600 outline-none focus:border-zinc-500"
          />
          <span className="block text-[10px] text-zinc-600">
            When set, chat and the spec writer can list, read, and search this
            directory (read-only, sandboxed — traversal outside it is rejected).
          </span>
        </label>

        <label className="block space-y-1">
          <span className="text-[11px] font-semibold tracking-wide text-zinc-500 uppercase">
            Context files
          </span>
          <input
            key={settings.context_files}
            defaultValue={settings.context_files}
            placeholder="AGENTS.md, CLAUDE.md, HANDOVER.md — injected into story prompts"
            onBlur={(e) => {
              const v = e.target.value.trim();
              if (v !== settings.context_files) patch({ context_files: v });
            }}
            className="w-full max-w-xl rounded-lg border border-zinc-700/70 bg-zinc-900 px-2.5 py-1.5 font-mono text-xs text-zinc-200 placeholder-zinc-600 outline-none focus:border-zinc-500"
          />
          <span className="block text-[10px] text-zinc-600">
            Matching files in the project root are prepended to the spec-writer
            and coder system prompts — the model doesn&apos;t have to remember to
            read them. Root-level names/globs, first match wins.
          </span>
        </label>

        <label className="flex items-start gap-3 rounded-lg border border-amber-900/40 bg-amber-950/20 p-3">
          <input
            type="checkbox"
            checked={settings.auto_approve_after_ai_review}
            onChange={(e) => patch({ auto_approve_after_ai_review: e.target.checked })}
            className="mt-0.5 accent-amber-400"
          />
          <span className="text-xs leading-relaxed text-zinc-300">
            <span className="font-medium">Auto-approve after AI review.</span>{" "}
            <span className="text-amber-400/90">Deprecated — prefer per-story gates.</span>{" "}
            Stories with an
            explicit AI-approved verdict skip the human gate and go straight to done (stamped{" "}
            <span className="font-mono text-[11px]">auto (ai-approved)</span>). Unparsable
            verdicts still wait for you. Per-story{" "}
            <span className="font-mono text-[11px]">gate_spec</span>/
            <span className="font-mono text-[11px]">gate_code</span> (set at create, in the
            story panel while editable, or per project in Projects) machine-check the story
            instead and still park it at the human gate when a check fails.
            <span className="mt-1 block text-amber-400/80">
              ⚠ this removes your last checkpoint — only enable for throwaway work.
            </span>
          </span>
        </label>
      </div>

      {/* permissions */}
      <div className="space-y-3 rounded-xl border border-red-900/40 bg-red-950/10 p-4">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-medium text-zinc-200">Execution & permissions</h3>
          <span className="text-[10px] text-zinc-600">
            coder agents · worktree isolation · command policy
          </span>
        </div>

        <label className="flex items-start gap-3">
          <input
            type="checkbox"
            checked={settings.write_tools_enabled}
            onChange={(e) => patch({ write_tools_enabled: e.target.checked })}
            className="mt-0.5 accent-emerald-400"
          />
          <span className="text-xs leading-relaxed text-zinc-300">
            <span className="font-medium">Write tools (execution).</span> Approved stories are
            executed by a coder agent in an isolated git worktree; the diff comes
            back for AI + human review before merging.
          </span>
        </label>

        <div className="space-y-1">
          <span className="text-[11px] font-semibold tracking-wide text-zinc-500 uppercase">
            Permission mode (outside-project paths)
          </span>
          <div className="flex flex-wrap gap-2">
            {([
              ["ask", "Ask me", "out-of-project paths prompt you live (zcode-style)"],
              ["strict", "Strict", "deny outright — the model stays in its dirs"],
              ["skip", "Skip all", "auto-grant everything (yolo)"],
            ] as const).map(([mode, label, title]) => (
              <button
                key={mode}
                title={title}
                onClick={() => patch({ permission_mode: mode })}
                className={`rounded-lg px-3 py-1.5 text-xs font-medium transition ${
                  settings.permission_mode === mode
                    ? mode === "skip"
                      ? "bg-red-600 text-white"
                      : "bg-zinc-100 text-zinc-900"
                    : "border border-zinc-700 text-zinc-300 hover:bg-zinc-800"
                }`}
              >
                {label}
              </button>
            ))}
          </div>
          {settings.permission_mode === "skip" && (
            <span className="block text-[11px] text-red-400/90">
              ⚠ any command runs in the worktree without the allowlist; the worktree
              jail, deny-lists, and budgets still apply.
            </span>
          )}
        </div>

        <label className="block space-y-1">
          <span className="text-[11px] font-semibold tracking-wide text-zinc-500 uppercase">
            Allowed commands {settings.permission_mode === "skip" && "(ignored — skip is on)"}
          </span>
          <input
            key={settings.allowed_commands}
            defaultValue={settings.allowed_commands}
            placeholder="empty = safe defaults (git, ls, cat, python3, node, npm, pytest, make…)"
            onBlur={(e) => {
              const v = e.target.value.trim();
              if (v !== settings.allowed_commands) patch({ allowed_commands: v });
            }}
            className={`w-full max-w-xl rounded-lg border border-zinc-700/70 bg-zinc-900 px-2.5 py-1.5 font-mono text-xs text-zinc-200 placeholder-zinc-600 outline-none focus:border-zinc-500 ${
              settings.permission_mode === "skip" ? "opacity-40" : ""
            }`}
          />
        </label>
      </div>

      {/* self-improvement */}
      <div className="space-y-3 rounded-xl border border-emerald-900/40 bg-emerald-950/10 p-4">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-medium text-zinc-200">Self-improvement</h3>
          <span className="text-[10px] text-zinc-600">
            verify in the worktree · gate the merge · restart on new code
          </span>
        </div>

        <label className="block space-y-1">
          <span className="text-[11px] font-semibold tracking-wide text-zinc-500 uppercase">
            Verify command
          </span>
          <input
            key={settings.verify_command}
            defaultValue={settings.verify_command}
            placeholder="e.g. cd backend && uv run pytest -q  (empty = verification skipped)"
            onBlur={(e) => {
              const v = e.target.value.trim();
              if (v !== settings.verify_command) patch({ verify_command: v });
            }}
            className="w-full max-w-xl rounded-lg border border-zinc-700/70 bg-zinc-900 px-2.5 py-1.5 font-mono text-xs text-zinc-200 placeholder-zinc-600 outline-none focus:border-zinc-500"
          />
          <span className="block text-[10px] text-zinc-600">
            Run in the story worktree after the coder commits. Failures bounce
            back to the coder for fix rounds; a still-failing story can&apos;t be
            merged.
          </span>
        </label>

        <div className="flex flex-wrap gap-x-8 gap-y-3">
          <NumberField
            label="Fix rounds"
            value={settings.verify_fix_rounds}
            min={0}
            max={5}
            hint="coder retries on failing verify"
            onCommit={(v) => patch({ verify_fix_rounds: v })}
          />
          <NumberField
            label="Verify timeout (s)"
            value={settings.verify_timeout}
            min={10}
            max={3600}
            hint="per verify run"
            onCommit={(v) => patch({ verify_timeout: v })}
          />
          <NumberField
            label="Spec reject limit"
            value={settings.spec_reject_limit}
            min={0}
            max={10}
            hint="AI rejections before parking at the human gate"
            onCommit={(v) => patch({ spec_reject_limit: v })}
          />
        </div>

        <label className="flex items-start gap-3 rounded-lg border border-amber-900/40 bg-amber-950/20 p-3">
          <input
            type="checkbox"
            checked={settings.self_update_enabled}
            onChange={(e) => patch({ self_update_enabled: e.target.checked })}
            className="mt-0.5 accent-amber-400"
          />
          <span className="text-xs leading-relaxed text-zinc-300">
            <span className="font-medium">Self-update on merge.</span> After
            Approve &amp; merge, the server restarts onto the new code and
            health-checks itself. If the new version won&apos;t boot, the merge is
            reverted automatically and the previous code comes back.
            <span className="mt-1 block text-amber-400/80">
              ⚠ the restart drops in-flight SSE streams for a few seconds.
            </span>
          </span>
        </label>
      </div>

      {/* api access */}
      <div className="space-y-3 rounded-xl border border-emerald-900/40 bg-emerald-950/10 p-4">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-medium text-zinc-200">API access</h3>
          <span className="text-[10px] text-zinc-600">
            bearer token · loopback &amp; session always allowed
          </span>
        </div>

        <label className="flex items-start gap-3 rounded-lg border border-emerald-900/40 bg-emerald-950/20 p-3">
          <input
            type="checkbox"
            checked={settings.api_auth_enabled}
            onChange={(e) => patch({ api_auth_enabled: e.target.checked })}
            className="mt-0.5 accent-emerald-400"
          />
          <span className="text-xs leading-relaxed text-zinc-300">
            <span className="font-medium">Require a token for API calls.</span>{" "}
            When on, every <code className="font-mono">/api/v1</code> call from
            another machine must carry an <code className="font-mono">Authorization:
            Bearer</code> header. Requests from this machine, and browsers with
            a Fubala session cookie, keep working as before. The board stays
            reachable on <code className="font-mono">/api/v1/health</code> so
            uptime checks are unaffected.
          </span>
        </label>

        <div className="space-y-2 rounded-lg border border-zinc-800/70 bg-zinc-900/40 p-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="text-xs text-zinc-400">
              API token{" "}
              {settings.api_token_hint ? (
                <span className="font-mono text-zinc-300">
                  ······{settings.api_token_hint}
                </span>
              ) : (
                <span className="text-zinc-600">· none configured</span>
              )}
            </span>
            <div className="flex gap-2">
              <button
                type="button"
                disabled={tokenBusy}
                onClick={async () => {
                  setTokenBusy(true);
                  setTokenError(null);
                  try {
                    const res = await api.rotateApiToken();
                    setFreshToken(res.token);
                    await refreshSettings();
                  } catch (e) {
                    setTokenError(e instanceof Error ? e.message : String(e));
                  } finally {
                    setTokenBusy(false);
                  }
                }}
                className="rounded-md border border-emerald-700/60 bg-emerald-900/40 px-2.5 py-1 text-xs text-emerald-200 hover:bg-emerald-900/70 disabled:opacity-50"
              >
                {settings.api_token_hint ? "Rotate token" : "Generate token"}
              </button>
              {settings.api_token_hint && (
                <button
                  type="button"
                  disabled={tokenBusy}
                  onClick={async () => {
                    setTokenBusy(true);
                    setTokenError(null);
                    try {
                      await api.deleteApiToken();
                      setFreshToken(null);
                      await refreshSettings();
                    } catch (e) {
                      setTokenError(e instanceof Error ? e.message : String(e));
                    } finally {
                      setTokenBusy(false);
                    }
                  }}
                  className="rounded-md border border-zinc-700 bg-zinc-800/60 px-2.5 py-1 text-xs text-zinc-300 hover:bg-zinc-800 disabled:opacity-50"
                >
                  Revoke
                </button>
              )}
            </div>
          </div>

          {freshToken && (
            <div className="space-y-2 rounded-lg border border-emerald-700/50 bg-emerald-950/30 p-2.5">
              <p className="text-[11px] text-emerald-200">
                Copy it now — it is never shown again.
              </p>
              <div className="flex items-center gap-2">
                <code className="flex-1 overflow-x-auto rounded bg-zinc-950/60 px-2 py-1 font-mono text-[11px] text-emerald-300">
                  {freshToken}
                </code>
                <button
                  type="button"
                  onClick={() => void navigator.clipboard?.writeText(freshToken)}
                  className="rounded-md border border-emerald-700/60 bg-emerald-900/40 px-2 py-1 text-[11px] text-emerald-200 hover:bg-emerald-900/70"
                >
                  Copy
                </button>
              </div>
              <p className="text-[11px] text-amber-400/80">
                Rotating invalidates every client that has the old token
                configured (MCP servers included) until their env is updated.
              </p>
            </div>
          )}

          {tokenError && (
            <p className="text-[11px] text-red-400">{tokenError}</p>
          )}
        </div>
      </div>

      {/* guardrails */}
      <div className="space-y-3 rounded-xl border border-sky-900/40 bg-sky-950/10 p-4">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-medium text-zinc-200">Agent guardrails</h3>
          <span className="text-[10px] text-zinc-600">
            secrets redacted · sensitive files denied · budgets enforced
          </span>
        </div>

        <div className="flex flex-wrap gap-x-8 gap-y-3">
          <label className="flex items-center gap-2 text-xs text-zinc-300">
            <input
              type="checkbox"
              checked={settings.chat_tools_enabled}
              onChange={(e) => patch({ chat_tools_enabled: e.target.checked })}
              className="accent-sky-400"
            />
            tools in chat
          </label>
          <label className="flex items-center gap-2 text-xs text-zinc-300">
            <input
              type="checkbox"
              checked={settings.spec_tools_enabled}
              onChange={(e) => patch({ spec_tools_enabled: e.target.checked })}
              className="accent-sky-400"
            />
            tools for the spec writer
          </label>
          <NumberField
            label="Max tool calls"
            value={settings.tools_max_calls}
            min={1}
            max={160}
            hint="per agent run (story override: max 160)"
            onCommit={(v) => patch({ tools_max_calls: v })}
          />
          <NumberField
            label="Tool time budget (s)"
            value={settings.tools_timeout_seconds}
            min={5}
            max={3600}
            hint="wall clock per run"
            onCommit={(v) => patch({ tools_timeout_seconds: v })}
          />
          <OptionalNumberField
            label="Spec output tokens"
            value={settings.spec_max_output_tokens}
            min={256}
            max={131072}
            step={256}
            placeholder="default"
            hint="empty = gateway default; raise if specs fail with 'spec phase produced no spec'"
            onCommit={(v) => patch({ spec_max_output_tokens: v })}
          />
          <OptionalNumberField
            label="Coder output tokens"
            value={settings.coder_max_output_tokens}
            min={256}
            max={131072}
            step={256}
            placeholder="default"
            hint="empty = gateway default; per-round completion budget for the coder"
            onCommit={(v) => patch({ coder_max_output_tokens: v })}
          />
          <NumberField
            label="Token escalation cap"
            value={settings.agent_max_output_tokens_cap}
            min={1024}
            max={131072}
            step={1024}
            hint="ceiling for automatic cap-hit escalations"
            onCommit={(v) => patch({ agent_max_output_tokens_cap: v })}
          />
        </div>

        <label className="block space-y-1">
          <span className="text-[11px] font-semibold tracking-wide text-zinc-500 uppercase">
            Extra denied file globs
          </span>
          <input
            key={settings.tools_deny_globs}
            defaultValue={settings.tools_deny_globs}
            placeholder="e.g. *.sql, migrations/, package-lock.json (in addition to .env*, keys, credentials…)"
            onBlur={(e) => {
              const v = e.target.value.trim();
              if (v !== settings.tools_deny_globs) patch({ tools_deny_globs: v });
            }}
            className="w-full max-w-xl rounded-lg border border-zinc-700/70 bg-zinc-900 px-2.5 py-1.5 font-mono text-xs text-zinc-200 placeholder-zinc-600 outline-none focus:border-zinc-500"
          />
        </label>
      </div>

      {/* chat */}
      <div className="space-y-2 rounded-xl border border-zinc-800/80 bg-zinc-900/40 p-4">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-medium text-zinc-200">Chat system prompt</h3>
          {settings.chat_system_prompt && (
            <span className="rounded-full bg-amber-950/70 px-2 py-0.5 text-[10px] text-amber-300">
              customized
            </span>
          )}
        </div>
        <textarea
          rows={3}
          value={chatPrompt}
          placeholder="(none — models answer plainly)"
          onChange={(e) => {
            setChatPrompt(e.target.value);
            setChatDirty(true);
          }}
          className="w-full resize-y rounded-lg border border-zinc-700/70 bg-zinc-950 p-3 font-mono text-[11px] text-zinc-300 placeholder-zinc-600 outline-none focus:border-zinc-500"
        />
        <div className="flex gap-2">
          <button
            disabled={!chatDirty}
            onClick={() => patch({ chat_system_prompt: chatPrompt })}
            className="rounded-lg bg-zinc-100 px-3 py-1.5 text-xs font-medium text-zinc-900 transition hover:bg-white disabled:opacity-30"
          >
            Save
          </button>
          <button
            disabled={!settings.chat_system_prompt}
            onClick={() => patch({ chat_system_prompt: "" })}
            className="rounded-lg px-3 py-1.5 text-xs text-zinc-400 transition hover:text-zinc-200 disabled:opacity-30"
          >
            Clear
          </button>
        </div>
      </div>

      {/* prompts */}
      <PromptEditor
        field="spec_writer_prompt"
        title="Spec writer prompt"
        hint="system prompt for the story spec-generation stage"
      />
      <PromptEditor
        field="ai_reviewer_prompt"
        title="AI reviewer prompt"
        hint="system prompt for the AI review gate — keep the VERDICT line format"
      />

      {/* system info */}
      <div className="rounded-xl border border-zinc-800/80 bg-zinc-900/40 p-4">
        <h3 className="mb-2 text-sm font-medium text-zinc-200">System</h3>
        <dl className="grid grid-cols-2 gap-x-6 gap-y-1 text-[11px] text-zinc-500 sm:grid-cols-3">
          <div>
            <dt className="inline">providers: </dt>
            <dd className="inline text-zinc-300">
              {providers.filter((p) => p.configured).length}/{providers.length} ready
            </dd>
          </div>
          <div>
            <dt className="inline">stories: </dt>
            <dd className="inline text-zinc-300">{stories.length}</dd>
          </div>
          <div>
            <dt className="inline">chat sessions: </dt>
            <dd className="inline text-zinc-300">{sessions.length}</dd>
          </div>
        </dl>
        <p className="mt-2 text-[10px] text-zinc-600">
          Settings apply live — no restart. Stored in the local database.
        </p>
      </div>
    </div>
  );
}
