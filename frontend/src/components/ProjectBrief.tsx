import { useCallback, useEffect, useState } from "react";

import { api } from "@/api";
import { Markdown } from "@/components/MessageItem";
import { useStore } from "@/store";
import type { Project, ProjectBrief as Brief, ProjectBriefVersionInfo } from "@/types";

const BUTTON =
  "rounded-lg border border-zinc-700 px-2 py-1 text-[11px] text-zinc-300 transition hover:bg-zinc-800 disabled:opacity-30";
const LABEL = "text-[10px] font-semibold tracking-wide text-zinc-500 uppercase";

/** The project's brief: a versioned Markdown description written to be read
 *  by an LLM in one go and by a human. Saves are optimistic-concurrency PUTs
 *  against the version the editor started from; a conflict keeps the user's
 *  text and offers Reload or Overwrite. History is append-only: a restore
 *  saves the old body as a new version. */
export function ProjectBrief({ project }: { project: Project }) {
  const patchProject = useStore((s) => s.patchProject);
  const [open, setOpen] = useState(false);
  const [brief, setBrief] = useState<Brief | null>(null);
  const [draft, setDraft] = useState<string | null>(null);
  const [baseVersion, setBaseVersion] = useState(0);
  const [conflict, setConflict] = useState<Brief | null>(null);
  const [versions, setVersions] = useState<ProjectBriefVersionInfo[] | null>(null);
  const [viewing, setViewing] = useState<Brief | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [budget, setBudget] = useState(String(project.brief_token_budget ?? 8000));

  const load = useCallback(async () => {
    try {
      const current = await api.getProjectBrief(project.id);
      setBrief(current);
      setError(null);
      return current;
    } catch (e) {
      setError((e as Error).message);
      return null;
    }
  }, [project.id]);

  useEffect(() => {
    void load();
  }, [load, project.brief_token_budget]);

  const loadVersions = async () => {
    try {
      setVersions(await api.listProjectBriefVersions(project.id));
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const startEdit = () => {
    if (!brief) return;
    setDraft(brief.body);
    setBaseVersion(brief.version);
    setConflict(null);
  };

  const save = async (body: string, against: number) => {
    setBusy(true);
    try {
      const result = await api.putProjectBrief(project.id, {
        body,
        base_version: against,
        author: "ui",
      });
      if (result.ok) {
        setBrief(result.brief);
        setDraft(null);
        setConflict(null);
        setViewing(null);
        if (versions) void loadVersions();
      } else {
        // keep the user's text; show what the server has now
        setConflict(result.current);
      }
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const reload = async () => {
    const current = await load();
    if (current) {
      setDraft(current.body);
      setBaseVersion(current.version);
      setConflict(null);
    }
  };

  const restore = (old: Brief) => {
    if (!brief) return;
    if (!confirm(`Restore brief v${old.version} as a new version?`)) return;
    void save(old.body, brief.version);
  };

  const commitBudget = () => {
    const parsed = Number.parseInt(budget, 10);
    const next = Number.isFinite(parsed) && parsed >= 1 ? parsed : 8000;
    setBudget(String(next));
    if (next !== (project.brief_token_budget ?? 8000)) {
      void patchProject(project.id, { brief_token_budget: next });
    }
  };

  return (
    <div className="mt-2 rounded-lg border border-zinc-800/60 bg-zinc-950/40 p-2.5">
      <div className="flex flex-wrap items-center gap-2">
        <button onClick={() => setOpen(!open)} className={LABEL}>
          {open ? "▾" : "▸"} brief
        </button>
        {brief && (
          <span className="text-[10px] text-zinc-500">
            {brief.version ? `v${brief.version} by ${brief.author || "-"}` : "no brief"}
            {" · "}
            <span className={brief.over_budget ? "text-amber-400" : undefined}>
              {brief.tokens} / {brief.budget} tokens
            </span>
          </span>
        )}
        {brief?.over_budget && (
          <span className="text-[10px] text-amber-400">
            over budget: trim it so an LLM can read it in one go
          </span>
        )}
      </div>

      {open && (
        <div className="mt-2 space-y-2">
          <div className="flex flex-wrap items-center gap-3">
            <label className="flex items-center gap-1.5">
              <input
                type="checkbox"
                checked={!!project.brief_in_spec_prompt}
                onChange={(e) =>
                  patchProject(project.id, { brief_in_spec_prompt: e.target.checked })
                }
              />
              <span className="text-[11px] text-zinc-300">inject into spec writer</span>
            </label>
            <label className="flex items-center gap-1.5">
              <span className="text-[10px] text-zinc-500">budget</span>
              <input
                type="number"
                min={1}
                value={budget}
                onChange={(e) => setBudget(e.target.value)}
                onBlur={commitBudget}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    commitBudget();
                  }
                }}
                className="w-20 rounded-lg border border-zinc-700/70 bg-zinc-900 px-2 py-1 text-xs text-zinc-200 outline-none focus:border-zinc-500"
              />
              <span className="text-[10px] text-zinc-500">tokens</span>
            </label>
          </div>
          <p className="text-[10px] leading-relaxed text-zinc-500">
            Injection sends only the &quot;## Owner rules&quot; and &quot;## Non-goals&quot;
            sections (exact headings) plus the story&apos;s epic description to the
            spec writer, never the whole brief. The coder never sees the brief.
          </p>

          {error && <p className="text-[11px] text-red-400">{error}</p>}

          {draft === null ? (
            <>
              <div className="flex flex-wrap gap-2">
                <button onClick={startEdit} disabled={!brief} className={BUTTON}>
                  Edit
                </button>
                <button
                  onClick={() => (versions ? setVersions(null) : void loadVersions())}
                  className={BUTTON}
                >
                  {versions ? "Hide history" : "History"}
                </button>
              </div>
              {brief &&
                (brief.body ? (
                  <div className="max-h-[32rem] overflow-y-auto rounded-lg border border-zinc-800 p-2 text-sm">
                    <Markdown content={brief.body} />
                  </div>
                ) : (
                  <p className="text-[11px] text-zinc-500">No brief yet.</p>
                ))}
            </>
          ) : (
            <>
              {conflict && (
                <div className="space-y-1 rounded-lg border border-amber-700/60 bg-amber-950/30 p-2">
                  <p className="text-[11px] text-amber-300">
                    Someone saved v{conflict.version} (by {conflict.author || "-"}) since you
                    started from v{baseVersion}. Your text is kept below.
                  </p>
                  <div className="flex gap-2">
                    <button onClick={() => void reload()} disabled={busy} className={BUTTON}>
                      Reload (discard my edit)
                    </button>
                    <button
                      onClick={() => void save(draft, conflict.version)}
                      disabled={busy}
                      className={BUTTON}
                    >
                      Overwrite v{conflict.version}
                    </button>
                  </div>
                  <details>
                    <summary className="cursor-pointer text-[10px] text-zinc-400">
                      server v{conflict.version}
                    </summary>
                    <pre className="mt-1 max-h-60 overflow-auto whitespace-pre-wrap font-mono text-[11px] text-zinc-400">
                      {conflict.body}
                    </pre>
                  </details>
                </div>
              )}
              <textarea
                rows={20}
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                spellCheck={false}
                className="w-full resize-y rounded-lg border border-zinc-700/70 bg-zinc-900 px-2.5 py-1.5 font-mono text-xs text-zinc-200 outline-none focus:border-zinc-500"
              />
              <div className="flex flex-wrap gap-2">
                <button
                  onClick={() => void save(draft, baseVersion)}
                  disabled={busy || !!conflict}
                  className={BUTTON}
                >
                  Save
                </button>
                <button
                  onClick={() => {
                    setDraft(null);
                    setConflict(null);
                  }}
                  className={BUTTON}
                >
                  Cancel
                </button>
                {!draft.trim() && brief?.template && (
                  <button onClick={() => setDraft(brief.template ?? "")} className={BUTTON}>
                    Insert template
                  </button>
                )}
                <span className="self-center text-[10px] text-zinc-500">
                  ~{Math.ceil(draft.length / 4)} / {brief?.budget ?? 8000} tokens
                </span>
              </div>
            </>
          )}

          {versions && (
            <div className="space-y-1">
              <span className={LABEL}>history</span>
              {versions.length === 0 && (
                <p className="text-[11px] text-zinc-500">No versions yet.</p>
              )}
              {versions.map((v) => (
                <div key={v.version} className="flex items-center gap-2 text-[11px] text-zinc-400">
                  <span className="w-10 font-mono">v{v.version}</span>
                  <span className="flex-1 truncate">
                    {v.author || "-"} · {new Date(v.created_at).toLocaleString()} · {v.tokens}{" "}
                    tokens
                  </span>
                  <button
                    onClick={async () => {
                      try {
                        setViewing(await api.getProjectBrief(project.id, v.version));
                      } catch (e) {
                        setError((e as Error).message);
                      }
                    }}
                    className={BUTTON}
                  >
                    View
                  </button>
                </div>
              ))}
              {viewing && (
                <div className="space-y-1 rounded-lg border border-zinc-800 p-2">
                  <div className="flex items-center gap-2">
                    <span className="text-[11px] text-zinc-300">
                      v{viewing.version} by {viewing.author || "-"}
                    </span>
                    <button
                      onClick={() => restore(viewing)}
                      disabled={busy || viewing.version === brief?.version}
                      className={BUTTON}
                    >
                      Restore
                    </button>
                    <button onClick={() => setViewing(null)} className={BUTTON}>
                      Close
                    </button>
                  </div>
                  <div className="max-h-80 overflow-y-auto text-sm">
                    <Markdown content={viewing.body} />
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
