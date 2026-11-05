import { useState } from "react";

import type {
  EvidenceVerify,
  Story,
  StoryDetail,
  StoryStatus,
  VerifyLaneCounts,
} from "@/types";
import { BADGE, badgeTone, PANEL } from "@/components/ui";

/** Statuses where the story is finished enough to lead with the outcome. */
export const OUTCOME_STATUSES: StoryStatus[] = ["ai_review", "human_review", "done"];

/**
 * First ~2 sentences of the AI review, used as the OUTCOME teaser when the
 * executor submitted no summary. Sentence-splitting on markdown is
 * approximate (code blocks, "e.g.") — acceptable for a teaser; the full
 * review renders below. Without a terminator the teaser is hard-capped at
 * ~240 chars so it can never blow up the panel.
 * Pure so it is unit-testable without rendering.
 */
export function reviewTeaser(review: string | null | undefined, maxChars = 240): string {
  if (!review) return "";
  const stripped = review.trim();
  if (!stripped) return "";
  const sentences = stripped.split(/(?<=[.!?])\s+/);
  let out = "";
  for (let i = 0; i < 2 && i < sentences.length; i++) {
    const candidate = out ? `${out} ${sentences[i]}` : sentences[i];
    if (candidate.length > maxChars) break;
    out = candidate;
  }
  if (!out) {
    // no sentence terminator inside the budget — hard cap
    out = stripped.length > maxChars ? `${stripped.slice(0, maxChars - 1).trimEnd()}…` : stripped;
  }
  return out.trim();
}

/** `N passed · M failed · K skipped · E errors` for one lane or the totals. */
export function countsLine(c: VerifyLaneCounts): string {
  return `${c.passed ?? 0} passed · ${c.failed ?? 0} failed · ${c.skipped ?? 0} skipped · ${c.errors ?? 0} errors`;
}

/**
 * Outcome-first block: summary, verdict chip and the verify run. Rendered as
 * the FIRST body section for ai_review/human_review/done so a human reviewer
 * sees "what happened" before scrolling into diffs and spec. Verify output is
 * collapsible — open by default when it failed (that is what needs reading),
 * closed when it passed.
 */
export function OutcomeSection({
  live,
  detail,
  verify,
}: {
  live: Story;
  detail: StoryDetail | null;
  verify: EvidenceVerify | null;
}) {
  const summary = detail?.evidence?.summary?.trim() || reviewTeaser(live.ai_review);
  const verdict = live.ai_verdict;
  // info/prep stories carry {"applicable": false} — no verify stage ever ran,
  // so there is no `passed` to read. Checked BEFORE any !passed logic.
  const notApplicable = verify?.applicable === false;
  const [outputOpen, setOutputOpen] = useState(verify ? !verify.passed && !notApplicable : false);
  const passed = verify?.passed;
  return (
    <section className={PANEL}>
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <h3 className="text-[11px] font-semibold tracking-wide text-zinc-200 uppercase">
          Outcome
        </h3>
        {verdict && (
          <span className={`${BADGE} ${badgeTone(verdict === "approved")}`}>
            {verdict === "approved" ? "✓ approved" : "✗ rejected"}
          </span>
        )}
        {verify && (notApplicable ? (
          <span className={`${BADGE} border-zinc-700/70 bg-zinc-800/40 text-zinc-400`}>
            verify n/a
          </span>
        ) : (
          <span className={`${BADGE} ${badgeTone(Boolean(passed))}`}>
            {passed ? "✓ verify passed" : "✗ verify failed"}
          </span>
        ))}
        {verify?.counts && !notApplicable && (
          <span className="font-mono text-[11px] text-zinc-400">
            {countsLine(verify.counts.totals)}
          </span>
        )}
      </div>
      {verify && !notApplicable && (verify.mismatch || verify.baseline?.flag) && (
        <div className="mb-2 space-y-0.5 font-mono text-[11px] text-amber-400">
          {verify.mismatch && <p>{verify.mismatch}</p>}
          {verify.baseline?.flag && <p>{verify.baseline.flag}</p>}
        </div>
      )}
      {verify?.counts && !notApplicable && verify.counts.lanes.length > 1 && (
        <ul className="mb-2 font-mono text-[10px] text-zinc-500">
          {verify.counts.lanes.map((lane, i) => (
            <li key={i}>
              lane {i + 1}: {countsLine(lane)}
            </li>
          ))}
        </ul>
      )}
      {summary ? (
        <p className="text-sm leading-relaxed text-zinc-200">{summary}</p>
      ) : (
        <p className="font-mono text-[11px] text-zinc-600">no summary yet</p>
      )}
      {verify && !notApplicable && (
        <div className="mt-3">
          <div className="mb-1.5 flex flex-wrap items-center gap-2">
            <span className="text-[10px] font-semibold tracking-wide text-zinc-500 uppercase">
              verify
            </span>
            <code className="rounded border border-zinc-800 bg-zinc-950 px-1.5 py-0.5 font-mono text-[10px] text-zinc-400">
              {verify.command || "(no command)"}
            </code>
            <button
              onClick={() => setOutputOpen((v) => !v)}
              className="rounded border border-zinc-700 px-2 py-0.5 font-mono text-[10px] text-zinc-400 transition hover:border-zinc-500 hover:text-zinc-200"
            >
              {outputOpen ? "▾ hide output" : "▸ show output"}
            </button>
          </div>
          {outputOpen && (
            <pre
              className={`max-h-48 overflow-auto rounded-xl border p-3 font-mono text-[11px] leading-relaxed whitespace-pre ${
                passed
                  ? "border-zinc-800/80 bg-zinc-950 text-zinc-400"
                  : "border-red-900/40 bg-red-950/10 text-red-200/80"
              }`}
            >
              {verify.output || "(no output)"}
            </pre>
          )}
        </div>
      )}
    </section>
  );
}
