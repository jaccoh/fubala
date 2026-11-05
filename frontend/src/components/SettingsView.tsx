import { useEffect, useState } from "react";

import { api } from "@/api";
import { AppSettingsPanel } from "@/components/AppSettingsPanel";
import { ModelCombobox } from "@/components/ModelCombobox";
import { providerLimitLabel } from "@/components/BoardView";
import { useStore } from "@/store";
import type { PresetInfo, ProviderInfo, Tier } from "@/types";

const TIERS: Tier[] = ["low", "medium", "high"];
const COST_LABEL: Record<Tier, string> = { low: "$", medium: "$$", high: "$$$" };
const KIND_LABELS: Record<string, string> = {
  litellm: "LiteLLM",
  openai_compatible: "OpenAI-compatible",
  claude_code: "Claude Code (OAuth)",
  echo: "built-in test",
};

/** Input that commits on blur (or Enter) via patch(). */
function EditInput({
  value,
  onCommit,
  placeholder,
  mono = false,
  password = false,
  className = "",
}: {
  value: string;
  onCommit: (next: string) => void;
  placeholder?: string;
  mono?: boolean;
  password?: boolean;
  className?: string;
}) {
  const [text, setText] = useState(value);
  useEffect(() => setText(value), [value]);
  const commit = () => {
    if (text !== value) onCommit(text.trim());
  };
  return (
    <input
      type={password ? "password" : "text"}
      value={text}
      placeholder={placeholder}
      onChange={(e) => setText(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
      className={`w-full rounded-lg border border-zinc-700/70 bg-zinc-900 px-2.5 py-1.5 text-xs text-zinc-200 placeholder-zinc-600 outline-none focus:border-zinc-500 ${
        mono ? "font-mono" : ""
      } ${className}`}
    />
  );
}

function TierSelect({
  value,
  onChange,
  cost = false,
}: {
  value: Tier;
  onChange: (t: Tier) => void;
  cost?: boolean;
}) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value as Tier)}
      className={`rounded-md border border-zinc-700/70 bg-zinc-900 px-1.5 py-1 text-[11px] outline-none focus:border-zinc-500 ${
        cost ? "text-amber-300/90" : "text-sky-300/90"
      }`}
      title={cost ? "cost tier" : "skill tier"}
    >
      {TIERS.map((t) => (
        <option key={t} value={t}>
          {cost ? `${COST_LABEL[t]} ${t}` : t}
        </option>
      ))}
    </select>
  );
}

function AddProviderModal({ onClose }: { onClose: () => void }) {
  const createProvider = useStore((s) => s.createProvider);
  const [presets, setPresets] = useState<PresetInfo[] | null>(null);
  const [presetId, setPresetId] = useState("openai");
  const [id, setId] = useState("openai");
  const [displayName, setDisplayName] = useState("OpenAI");
  const [baseUrl, setBaseUrl] = useState("");
  const [envKey, setEnvKey] = useState("OPENAI_API_KEY");
  const [apiKey, setApiKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.listPresets().then((list) => {
      setPresets(list);
      apply(list.find((p) => p.id === "openai") ?? list[0]);
    });
  }, []);

  const apply = (preset: PresetInfo | undefined) => {
    if (!preset) return;
    setPresetId(preset.id);
    setId(preset.id);
    setDisplayName(preset.display_name);
    setBaseUrl(preset.base_url ?? "");
    setEnvKey(preset.env_key ?? "");
  };

  const submit = async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await createProvider({
        preset: presetId,
        id: id.trim() || undefined,
        display_name: displayName.trim() || undefined,
        base_url: baseUrl.trim() || undefined,
        env_key: envKey.trim() || undefined,
        api_key: apiKey.trim() || undefined,
      });
      onClose();
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={onClose}>
      <div
        className="w-full max-w-lg space-y-4 rounded-2xl border border-zinc-800 bg-zinc-950 p-5 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="text-base font-semibold text-zinc-100">Add provider</h2>

        <label className="block space-y-1">
          <span className="text-[11px] font-semibold tracking-wide text-zinc-500 uppercase">
            Type
          </span>
          <select
            value={presetId}
            onChange={(e) => apply((presets ?? []).find((p) => p.id === e.target.value))}
            className="w-full rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm text-zinc-100 outline-none focus:border-zinc-500"
          >
            {(presets ?? []).map((p) => (
              <option key={p.id} value={p.id}>
                {p.display_name}
              </option>
            ))}
            {presets === null && <option>loading…</option>}
          </select>
        </label>

        <div className="grid grid-cols-2 gap-3">
          <label className="block space-y-1">
            <span className="text-[11px] font-semibold tracking-wide text-zinc-500 uppercase">
              Name
            </span>
            <input
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              className="w-full rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm text-zinc-100 outline-none focus:border-zinc-500"
            />
          </label>
          <label className="block space-y-1">
            <span className="text-[11px] font-semibold tracking-wide text-zinc-500 uppercase">
              Slug (model prefix)
            </span>
            <input
              value={id}
              onChange={(e) => setId(e.target.value)}
              placeholder="my-openai"
              className="w-full rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2 font-mono text-sm text-zinc-100 outline-none focus:border-zinc-500"
            />
          </label>
        </div>

        <label className="block space-y-1">
          <span className="text-[11px] font-semibold tracking-wide text-zinc-500 uppercase">
            Base URL {presetId === "custom" && <span className="text-red-400">(required)</span>}
          </span>
          <input
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
            placeholder="https://api.example.com/v1"
            className="w-full rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2 font-mono text-xs text-zinc-100 placeholder-zinc-600 outline-none focus:border-zinc-500"
          />
        </label>

        <div className="grid grid-cols-2 gap-3">
          <label className="block space-y-1">
            <span className="text-[11px] font-semibold tracking-wide text-zinc-500 uppercase">
              Env var (fallback key)
            </span>
            <input
              value={envKey}
              onChange={(e) => setEnvKey(e.target.value)}
              placeholder="MY_PROVIDER_API_KEY"
              className="w-full rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2 font-mono text-xs text-zinc-100 placeholder-zinc-600 outline-none focus:border-zinc-500"
            />
          </label>
          <label className="block space-y-1">
            <span className="text-[11px] font-semibold tracking-wide text-zinc-500 uppercase">
              API key (stored locally)
            </span>
            <input
              type="password"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder="optional now, set later"
              className="w-full rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2 font-mono text-xs text-zinc-100 placeholder-zinc-600 outline-none focus:border-zinc-500"
            />
          </label>
        </div>

        {error && <p className="text-xs text-red-400">{error}</p>}

        <div className="flex justify-end gap-2 pt-1">
          <button onClick={onClose} className="rounded-lg px-3 py-1.5 text-sm text-zinc-400 hover:text-zinc-200">
            Cancel
          </button>
          <button
            onClick={submit}
            disabled={busy || !id.trim()}
            className="rounded-lg bg-zinc-100 px-4 py-1.5 text-sm font-medium text-zinc-900 transition hover:bg-white disabled:opacity-30"
          >
            {busy ? "Adding…" : "Add provider"}
          </button>
        </div>
      </div>
    </div>
  );
}

function ProviderCard({ provider }: { provider: ProviderInfo }) {
  const patchProvider = useStore((s) => s.patchProvider);
  const deleteProvider = useStore((s) => s.deleteProvider);
  const setProviderKey = useStore((s) => s.setProviderKey);
  const addProviderModel = useStore((s) => s.addProviderModel);
  const updateProviderModel = useStore((s) => s.updateProviderModel);
  const deleteProviderModel = useStore((s) => s.deleteProviderModel);
  const providerLimits = useStore((s) => s.providerLimits);
  const clearProviderLimit = useStore((s) => s.clearProviderLimit);
  const hold = providerLimits.find((l) => l.provider_id === provider.id);
  const [keyInput, setKeyInput] = useState("");
  const [newModel, setNewModel] = useState("");
  const [newSkill, setNewSkill] = useState<Tier>("medium");
  const [newCost, setNewCost] = useState<Tier>("medium");
  const [syncing, setSyncing] = useState(false);
  const [syncNote, setSyncNote] = useState<string | null>(null);

  const sync = async () => {
    if (syncing || provider.kind === "echo") return;
    setSyncing(true);
    setSyncNote(null);
    try {
      const result = await api.refreshProviderModels(provider.id, true);
      setSyncNote(
        `+${result.sync?.added ?? 0} new · −${result.sync?.removed ?? 0} removed · ${result.sync?.seen ?? 0} on provider`,
      );
    } catch (e) {
      setSyncNote((e as Error).message);
    } finally {
      setSyncing(false);
    }
  };

  return (
    <div className="rounded-xl border border-zinc-800/80 bg-zinc-900/40 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <div className="min-w-40 flex-1">
          <EditInput
            value={provider.display_name}
            onCommit={(v) => patchProvider(provider.id, { display_name: v })}
            className="!text-sm !font-medium"
          />
        </div>
        <span className="font-mono text-[11px] text-zinc-500">{provider.id}</span>
        <span className="text-[11px] text-zinc-500">{KIND_LABELS[provider.kind] ?? provider.kind}</span>
        {hold && (
          <span
            className="flex items-center gap-1.5 rounded-full border border-amber-800/70 bg-amber-950/60 px-2 py-0.5 text-[11px] text-amber-300"
            title={`${hold.reason} — headless pickup on this provider waits until the stated reset`}
          >
            {providerLimitLabel(hold)}
            <button
              onClick={() => clearProviderLimit(provider.id).catch(() => {})}
              className="rounded px-1 text-amber-400 transition hover:text-amber-200"
              title="Clear the hold; the scheduler retries this provider at its next tick"
            >
              Clear hold
            </button>
          </span>
        )}
        <span
          className={`rounded-full px-2 py-0.5 text-[11px] font-medium ${
            provider.configured ? "bg-emerald-950/80 text-emerald-300" : "bg-zinc-800 text-zinc-400"
          }`}
        >
          {provider.configured ? "ready" : provider.has_key || provider.env_key ? "key missing" : "no auth"}
        </span>
        {provider.protected && <span title="protected">🔒</span>}
        <button
          onClick={sync}
          disabled={syncing || provider.kind === "echo"}
          title="Fetch the live model list from the provider"
          className="rounded-lg border border-zinc-700 px-2 py-1 text-[11px] text-zinc-300 transition hover:bg-zinc-800 disabled:opacity-30"
        >
          {syncing ? "…" : "↻ Sync models"}
        </button>
        <label className="ml-auto flex items-center gap-1.5 text-[11px] text-zinc-400">
          <input
            type="checkbox"
            checked={provider.enabled}
            onChange={(e) => patchProvider(provider.id, { enabled: e.target.checked })}
            className="accent-zinc-400"
          />
          enabled
        </label>
      </div>

      {syncNote && <p className="mt-1.5 text-[11px] text-zinc-500">{syncNote}</p>}

      <div className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-2">
        <label className="flex items-center gap-2">
          <span className="w-14 shrink-0 text-[10px] font-semibold tracking-wide text-zinc-500 uppercase">
            base url
          </span>
          <EditInput
            value={provider.base_url ?? ""}
            onCommit={(v) => patchProvider(provider.id, { base_url: v })}
            placeholder="provider default"
            mono
          />
        </label>
        <label className="flex items-center gap-2">
          <span className="w-14 shrink-0 text-[10px] font-semibold tracking-wide text-zinc-500 uppercase">
            env var
          </span>
          <EditInput
            value={provider.env_key ?? ""}
            onCommit={(v) => patchProvider(provider.id, { env_key: v })}
            placeholder="none"
            mono
          />
        </label>
      </div>

      <div className="mt-2 flex items-center gap-2">
        <span className="w-14 shrink-0 text-[10px] font-semibold tracking-wide text-zinc-500 uppercase">
          api key
        </span>
        <input
          type="password"
          value={keyInput}
          onChange={(e) => setKeyInput(e.target.value)}
          placeholder={
            provider.has_key ? `stored (…${provider.key_hint})` : "paste key to store locally"
          }
          className="flex-1 rounded-lg border border-zinc-700/70 bg-zinc-900 px-2.5 py-1.5 font-mono text-xs text-zinc-200 placeholder-zinc-600 outline-none focus:border-zinc-500"
        />
        <button
          onClick={() => {
            if (keyInput.trim()) {
              setProviderKey(provider.id, keyInput.trim());
              setKeyInput("");
            }
          }}
          disabled={!keyInput.trim()}
          className="rounded-lg bg-zinc-800 px-2.5 py-1.5 text-[11px] text-zinc-200 transition hover:bg-zinc-700 disabled:opacity-30"
        >
          Save
        </button>
        {provider.has_key && (
          <button
            onClick={() => setProviderKey(provider.id, "")}
            className="rounded-lg px-2 py-1.5 text-[11px] text-zinc-500 transition hover:text-red-400"
          >
            Clear
          </button>
        )}
      </div>

      <div className="mt-4">
        <div className="mb-1.5 flex items-center justify-between">
          <h3 className="text-[10px] font-semibold tracking-wide text-zinc-500 uppercase">
            Models — skill tier & cost tier
          </h3>
          <span className="text-[10px] text-zinc-600">skill gates & cost weighs story routing</span>
        </div>
        <div className="space-y-1">
          {provider.models.map((m) => (
            <div key={m.row_id} className="flex items-center gap-2">
              <span className="min-w-0 flex-1 truncate font-mono text-xs text-zinc-300">
                {m.model_id}
              </span>
              <TierSelect value={m.skill} onChange={(t) => updateProviderModel(provider.id, m.row_id, { skill: t })} />
              <TierSelect value={m.cost} cost onChange={(t) => updateProviderModel(provider.id, m.row_id, { cost: t })} />
              <button
                onClick={() => deleteProviderModel(provider.id, m.row_id)}
                className="rounded p-1 text-zinc-600 transition hover:text-red-400"
                title="Remove model"
              >
                ✕
              </button>
            </div>
          ))}
          {provider.models.length === 0 && (
            <p className="py-1 text-[11px] text-zinc-600">no models yet — add one below</p>
          )}
        </div>
        <div className="mt-2 flex items-center gap-2 border-t border-zinc-800/60 pt-2">
          <ModelCombobox
            providerId={provider.id}
            value={newModel}
            onQueryChange={setNewModel}
            placeholder="search live models… or type a custom id"
            onPick={(modelId, skill, cost) =>
              addProviderModel(provider.id, modelId, skill, cost)
            }
          />
          <TierSelect value={newSkill} onChange={setNewSkill} />
          <TierSelect value={newCost} cost onChange={setNewCost} />
          <button
            onClick={() => {
              if (newModel.trim()) {
                addProviderModel(provider.id, newModel.trim(), newSkill, newCost);
                setNewModel("");
              }
            }}
            disabled={!newModel.trim()}
            title="Add as a custom model with the tiers selected on the left"
            className="rounded-lg bg-zinc-800 px-2.5 py-1.5 text-[11px] text-zinc-200 transition hover:bg-zinc-700 disabled:opacity-30"
          >
            Add
          </button>
        </div>
      </div>

      {!provider.protected && (
        <div className="mt-3 border-t border-zinc-800/60 pt-2 text-right">
          <button
            onClick={() => confirm(`Delete provider "${provider.display_name}"?`) && deleteProvider(provider.id)}
            className="text-[11px] text-zinc-600 transition hover:text-red-400"
          >
            delete provider
          </button>
        </div>
      )}
    </div>
  );
}

export function SettingsView() {
  const providers = useStore((s) => s.providers);
  const setView = useStore((s) => s.setView);
  const [modalOpen, setModalOpen] = useState(false);
  const [tab, setTab] = useState<"providers" | "app">("providers");

  return (
    <div className="flex h-full min-h-0 flex-col">
      <header className="flex h-12 shrink-0 items-center justify-between gap-3 border-b border-zinc-800/70 px-4">
        <div className="flex rounded-lg bg-zinc-800/40 p-0.5">
          {(["providers", "app"] as const).map((t) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={`rounded-md px-3 py-1.5 text-xs font-medium transition ${
                tab === t
                  ? "bg-zinc-700/80 text-zinc-100"
                  : "text-zinc-400 hover:bg-zinc-800/50 hover:text-zinc-200"
              }`}
            >
              {t === "providers" ? "Providers" : "Application"}
            </button>
          ))}
        </div>
        <div className="flex items-center gap-2">
          {tab === "providers" && (
            <button
              onClick={() => setModalOpen(true)}
              className="rounded-lg bg-zinc-100 px-3 py-1.5 text-xs font-medium text-zinc-900 transition hover:bg-white"
            >
              + Add provider
            </button>
          )}
          <button
            onClick={() => setView("projects")}
            className="rounded-lg border border-zinc-700 px-3 py-1.5 text-xs text-zinc-300 transition hover:bg-zinc-800"
          >
            Projects →
          </button>
          <button
            onClick={() => setView("chat")}
            className="rounded-lg px-3 py-1.5 text-xs text-zinc-400 transition hover:bg-zinc-800/60 hover:text-zinc-200"
          >
            ← Back to chat
          </button>
        </div>
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-6">
        {tab === "app" ? (
          <AppSettingsPanel />
        ) : (
          <div className="mx-auto w-full max-w-3xl space-y-3">
            <p className="text-sm leading-relaxed text-zinc-400">
              Everything here lives in the database — add providers from a type preset and edit
              anything: URLs, keys, models. Keys are stored locally (SQLite); env vars still
              work as a fallback. Skill and cost tiers drive cost-aware story routing when a
              story asks for a skill instead of a model.
            </p>
            {providers.map((p) => (
              <ProviderCard key={p.id} provider={p} />
            ))}
          </div>
        )}
      </div>

      {modalOpen && <AddProviderModal onClose={() => setModalOpen(false)} />}
    </div>
  );
}
