/**
 * Shared Tailwind class shells for controls and surfaces that repeat across
 * views. Plain string constants only — no component wrapper, no abstraction
 * layer. Each constant is the *exact* concatenation of the strings it replaces
 * (same tokens, same order: layout → typography → color), so rendering is
 * byte-identical to the inline literals they stand in for.
 *
 * Variants that differ only by a trailing state class (e.g. `disabled:…`) are
 * derived by concatenation rather than re-typed, so they can never drift.
 */

/** Primary call-to-action button (light chip on dark UI). */
const PRIMARY_BUTTON =
  "rounded-lg bg-zinc-100 px-3 py-1.5 text-xs font-medium text-zinc-900 transition hover:bg-white";

/** Primary button in its disabled state (e.g. while a save is in flight). */
export const PRIMARY_BUTTON_DISABLED = `${PRIMARY_BUTTON} disabled:opacity-30`;

/** Secondary bordered button (Unqueue, Reject & requeue, …). */
export const SECONDARY_BUTTON =
  "rounded-lg border border-zinc-700 px-3 py-1.5 text-xs text-zinc-300 transition hover:bg-zinc-800";

/** Uppercase field/section label used above inputs and lists. */
export const FIELD_LABEL = "text-[11px] font-semibold tracking-wide text-zinc-500 uppercase";

/** Neutral card surface (settings cards, spec/review blocks). */
export const PANEL = "rounded-xl border border-zinc-800/80 bg-zinc-900/40 p-4";

/** Pill chip shell shared by verdict/verify badges; append a tone class. */
export const BADGE = "rounded-full px-2 py-0.5 text-[10px] font-medium";

/** Green/red tone pair for the {@link BADGE} shell. */
export const badgeTone = (ok: boolean): string =>
  ok ? "bg-emerald-950/80 text-emerald-300" : "bg-red-950/80 text-red-300";
