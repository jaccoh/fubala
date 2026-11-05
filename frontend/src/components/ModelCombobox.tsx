import { useEffect, useRef, useState } from "react";

import { api } from "@/api";
import type { Tier } from "@/types";

interface RemoteModel {
  model_id: string;
  skill: Tier;
  cost: Tier;
}

/**
 * Searchable model picker for a provider's live catalog: type to filter the
 * provider's real model list, click to add with inferred tiers — or type any
 * custom id and use Add. Falls back to free-text when the list can't load.
 */
export function ModelCombobox({
  providerId,
  onPick,
  onQueryChange,
  value,
  placeholder,
}: {
  providerId: string;
  onPick: (modelId: string, skill: Tier, cost: Tier) => void;
  onQueryChange: (query: string) => void;
  value: string;
  placeholder: string;
}) {
  const [options, setOptions] = useState<RemoteModel[] | null>(null);
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [highlight, setHighlight] = useState(0);
  const boxRef = useRef<HTMLDivElement>(null);

  const load = async () => {
    if (loading || options) return;
    setLoading(true);
    setNote(null);
    try {
      const list = await api.listRemoteModels(providerId);
      setOptions(list);
    } catch (e) {
      setOptions([]);
      setNote(`live list unavailable — free entry (${(e as Error).message})`);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    const close = (e: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, []);

  const query = value.trim().toLowerCase();
  const matches = (options ?? [])
    .filter((m) => !query || m.model_id.toLowerCase().includes(query))
    .slice(0, 40);
  const exactMatch = (options ?? []).some(
    (m) => m.model_id.toLowerCase() === query && query !== "",
  );

  const pick = (m: RemoteModel) => {
    onPick(m.model_id, m.skill, m.cost);
    onQueryChange("");
    setOpen(false);
  };

  return (
    <div ref={boxRef} className="relative min-w-0 flex-1">
      <input
        value={value}
        placeholder={placeholder}
        onFocus={() => {
          setOpen(true);
          load();
        }}
        onChange={(e) => {
          onQueryChange(e.target.value);
          setOpen(true);
          setHighlight(0);
        }}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown") {
            e.preventDefault();
            setOpen(true);
            setHighlight((h) => Math.min(h + 1, matches.length - 1));
          } else if (e.key === "ArrowUp") {
            e.preventDefault();
            setHighlight((h) => Math.max(h - 1, 0));
          } else if (e.key === "Enter" && open && matches[highlight]) {
            e.preventDefault();
            pick(matches[highlight]);
          } else if (e.key === "Escape") {
            setOpen(false);
          }
        }}
        className="w-full rounded-lg border border-zinc-700/70 bg-zinc-900 px-2.5 py-1.5 font-mono text-xs text-zinc-200 placeholder-zinc-600 outline-none focus:border-zinc-500"
      />
      {open && (
        <div className="absolute bottom-full z-20 mb-1 max-h-64 w-full overflow-y-auto rounded-lg border border-zinc-700 bg-zinc-900 shadow-xl">
          {loading && <p className="px-3 py-2 text-[11px] text-zinc-500">loading live models…</p>}
          {!loading && note && <p className="px-3 py-2 text-[11px] text-amber-400/80">{note}</p>}
          {!loading &&
            matches.map((m, i) => (
              <button
                key={m.model_id}
                onMouseEnter={() => setHighlight(i)}
                onClick={() => pick(m)}
                className={`flex w-full items-center justify-between gap-2 px-3 py-1.5 text-left font-mono text-xs transition ${
                  i === highlight ? "bg-zinc-800 text-zinc-100" : "text-zinc-300"
                }`}
              >
                <span className="truncate">{m.model_id}</span>
                <span className="shrink-0 text-[10px] text-zinc-500">
                  {m.skill} · {"$".repeat({ low: 1, medium: 2, high: 3 }[m.cost])}
                </span>
              </button>
            ))}
          {!loading && matches.length === 0 && query && !exactMatch && (
            <p className="px-3 py-2 text-[11px] text-zinc-500">
              no match — <span className="text-zinc-300">Add "{value.trim()}"</span> as custom model
            </p>
          )}
          {!loading && !query && matches.length === 0 && !note && (
            <p className="px-3 py-2 text-[11px] text-zinc-500">no models listed</p>
          )}
        </div>
      )}
    </div>
  );
}
