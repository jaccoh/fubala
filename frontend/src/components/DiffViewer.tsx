import { useEffect, useMemo, useState } from "react";

import { api, diffDownloadUrl } from "@/api";

/** Parse `diff --git a/… b/…` headers into per-file foldable sections. */
type DiffSection = { header: string; body: string };

function splitDiff(diff: string): DiffSection[] {
  const lines = diff.split("\n");
  const sections: DiffSection[] = [];
  let current: DiffSection | null = null;
  for (const line of lines) {
    if (line.startsWith("diff --git ")) {
      if (current) sections.push(current);
      current = { header: line, body: "" };
    } else if (current) {
      current.body += line + "\n";
    } else {
      // preamble before the first file header
      current = { header: "", body: line + "\n" };
    }
  }
  if (current) sections.push(current);
  return sections;
}

function DiffFileSection({
  section,
  defaultOpen,
}: {
  section: DiffSection;
  defaultOpen: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const [hunkOpen, setHunkOpen] = useState<Record<number, boolean>>({});
  const hunks = useMemo(() => {
    const lines = section.body.split("\n");
    const out: { header: string; body: string }[] = [];
    let cur: { header: string; body: string } | null = null;
    for (const line of lines) {
      if (line.startsWith("@@")) {
        if (cur) out.push(cur);
        cur = { header: line, body: "" };
      } else if (cur) {
        cur.body += line + "\n";
      }
    }
    if (cur) out.push(cur);
    return out;
  }, [section.body]);
  const added = (section.body.match(/^\+[^+]/gm) || []).length;
  const removed = (section.body.match(/^\-[^-]/gm) || []).length;
  const path = section.header.replace(/^diff --git a\//, "").replace(/ b\/.*$/, "");
  return (
    <div className="overflow-hidden rounded-lg border border-zinc-800/80 bg-zinc-950">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left transition hover:bg-zinc-900/60"
      >
        <span className="font-mono text-[10px] text-zinc-500">{open ? "▾" : "▸"}</span>
        <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-zinc-300">
          {path || section.header || "(preamble)"}
        </span>
        <span className="shrink-0 font-mono text-[10px] text-emerald-400">+{added}</span>
        <span className="shrink-0 font-mono text-[10px] text-red-400">−{removed}</span>
      </button>
      {open && (
        <div className="border-t border-zinc-800/60">
          {hunks.length === 0 && (
            <pre className="p-3 font-mono text-[11px] leading-relaxed whitespace-pre text-zinc-400">
              {section.body}
            </pre>
          )}
          {hunks.map((hunk, i) => {
            const isOpen = hunkOpen[i] ?? true;
            return (
              <div key={i}>
                <button
                  onClick={() => setHunkOpen((m) => ({ ...m, [i]: !isOpen }))}
                  className="flex w-full items-center gap-2 bg-zinc-900/40 px-3 py-1 text-left transition hover:bg-zinc-900/70"
                >
                  <span className="font-mono text-[9px] text-zinc-600">{isOpen ? "▾" : "▸"}</span>
                  <span className="truncate font-mono text-[10px] text-sky-400/80">{hunk.header}</span>
                </button>
                {isOpen && (
                  <div className="overflow-x-auto py-2 font-mono text-[11px] leading-relaxed">
                    {hunk.body.split("\n").map((line, j) => {
                      if (!line) return null; // trailing newline artifact
                      const kind = line.startsWith("+")
                        ? "add"
                        : line.startsWith("-")
                          ? "del"
                          : "ctx";
                      return (
                        <div
                          key={j}
                          className={`whitespace-pre px-3 ${
                            kind === "add"
                              ? "bg-emerald-950/40 text-emerald-300"
                              : kind === "del"
                                ? "bg-red-950/40 text-red-300"
                                : "text-zinc-400"
                          }`}
                        >
                          {line}
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

/** Uncapped worktree diff with per-file folding + raw download. Polls while
 * the story is executing (read-only endpoint). `fallbackDiff` is used when
 * the endpoint yields nothing (e.g. 404 for external-execution stories, where
 * the worktree never existed): the evidence diff the executor submitted. */
export function DiffViewer({
  storyId,
  executing,
  hasBaseRef,
  fallbackDiff = null,
}: {
  storyId: string;
  executing: boolean;
  hasBaseRef: boolean;
  fallbackDiff?: string | null;
}) {
  const [diff, setDiff] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const load = async () => {
      try {
        const text = await api.getStoryDiff(storyId);
        if (cancelled) return;
        setDiff(text);
        setError(null);
      } catch (e) {
        if (cancelled) return;
        setDiff(null);
        setError((e as Error).message);
      } finally {
        if (!cancelled) setLoaded(true);
      }
      if (!cancelled && executing) timer = setTimeout(load, 5_000);
    };
    load();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [storyId, executing]);

  /** True when we are showing evidence instead of the endpoint's worktree
   * diff — no endpoint serves those, so the raw download link must go. */
  const usingFallback = !diff && Boolean(fallbackDiff);
  const effectiveDiff = diff || fallbackDiff;

  if (!loaded) {
    return (
      <p className="font-mono text-[11px] text-zinc-600">loading diff…</p>
    );
  }
  if (error && !effectiveDiff) {
    return (
      <div className="rounded-lg border border-zinc-800/80 bg-zinc-900/40 p-3 font-mono text-[11px] text-zinc-500">
        {error}
      </div>
    );
  }
  if (!effectiveDiff) {
    return (
      <p className="font-mono text-[11px] text-zinc-600">
        {hasBaseRef ? "no changes yet" : "diff appears once execution starts"}
      </p>
    );
  }
  const sections = splitDiff(effectiveDiff);
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <span className="font-mono text-[10px] text-zinc-600">
          {sections.length} file(s) · {effectiveDiff.length.toLocaleString()} chars ·{" "}
          {usingFallback ? "submitted evidence" : "uncapped"}
        </span>
        {!usingFallback && (
          <a
            href={diffDownloadUrl(storyId)}
            className="rounded border border-zinc-700 px-2 py-0.5 font-mono text-[10px] text-zinc-400 transition hover:border-zinc-500 hover:text-zinc-200"
          >
            ⤓ raw .diff
          </a>
        )}
      </div>
      <div className="max-h-96 space-y-2 overflow-y-auto">
        {sections.map((s, i) => (
          <DiffFileSection key={i} section={s} defaultOpen={i === 0} />
        ))}
      </div>
    </div>
  );
}
