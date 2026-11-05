import { useStore } from "@/store";

export function ModelPicker() {
  const models = useStore((s) => s.models);
  const selectedModel = useStore((s) => s.selectedModel);
  const setSelectedModel = useStore((s) => s.setSelectedModel);

  if (!models) return null;

  return (
    <label className="flex items-center gap-2 text-xs text-zinc-500">
      <span className="hidden sm:inline">model</span>
      <select
        value={selectedModel ?? models.default_model}
        onChange={(e) => setSelectedModel(e.target.value)}
        className="max-w-72 truncate rounded-lg border border-zinc-700 bg-zinc-900 px-2.5 py-1.5 font-mono text-xs text-zinc-200 outline-none focus:border-zinc-500"
      >
        {models.providers.map((p) => (
          <optgroup
            key={p.id}
            label={`${p.display_name}${p.configured ? "" : " — key missing"}`}
          >
            {p.models.map((m) => (
              <option key={m.id} value={m.id} disabled={!p.configured}>
                {m.id} · {m.skill} · {"$".repeat({ low: 1, medium: 2, high: 3 }[m.cost] ?? 2)}
              </option>
            ))}
          </optgroup>
        ))}
      </select>
    </label>
  );
}
