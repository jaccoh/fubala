/**
 * Provider usage-limit badge tests (node env, same pattern as
 * TrainPanel.test.ts: no jsdom, no rendering — only the pure label the
 * board header and the settings ProviderCard share).
 *
 * What is pinned here is the operator-facing contract of a headless 429
 * hold: the badge names the provider, states the local time the hold ends,
 * and adds the date once the reset is no longer today.
 */
import { describe, expect, it } from "vitest";

import { providerLimitLabel } from "@/components/BoardView";
import { cardVerdictChip, verdictBadge } from "@/components/boardColumns";
import type { ProviderLimit } from "@/types";

const limit = (over: Partial<ProviderLimit> = {}): ProviderLimit =>
  ({
    provider_id: "zai",
    until: "2026-10-26T11:29:59Z",
    reason: "rate limit (headless): Usage limit reached for 5 hour",
    ...over,
  }) as ProviderLimit;

/** local HH:MM of an instant, zero-padded — the same formatting the label
 * uses, derived from the Date so the assertion holds in any test TZ. */
const localHm = (d: Date): string =>
  `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;

describe("providerLimitLabel", () => {
  it("names the provider and the local end time while the hold is today", () => {
    const until = new Date("2026-10-26T11:29:59Z");
    const now = new Date(until.getTime() - 60_000); // one minute earlier:
    // the same local day in every timezone
    expect(providerLimitLabel(limit(), now)).toBe(
      `zai limited until ${localHm(until)}`,
    );
  });

  it("pads the minutes and keeps the provider id", () => {
    const until = new Date("2026-10-26T11:05:09Z");
    const now = until; // same instant: always the same local day
    expect(providerLimitLabel(limit({ until: "2026-10-26T11:05:09Z" }), now)).toBe(
      `zai limited until ${localHm(until)}`,
    );
  });

  it("adds the date once the reset is not today", () => {
    const until = new Date("2026-10-26T11:29:59Z");
    const now = new Date("2026-10-24T09:00:00Z"); // two local days earlier
    // (any TZ offset keeps 26 Oct after 24 Oct)
    expect(providerLimitLabel(limit(), now)).toBe(
      `zai limited until ${until.getDate()} Oct ${localHm(until)}`,
    );
  });
});

describe("AI verdict badges", () => {
  it("shows an amber badge for an incomplete code review", () => {
    expect(verdictBadge("incomplete")).toEqual({
      label: "⚠ incomplete",
      tone: "bg-amber-950/80 text-amber-300",
    });
    expect(cardVerdictChip("incomplete")).toEqual({
      label: "AI incomplete",
      tone: "bg-amber-950/80 text-amber-300",
    });
  });

  it("keeps approved and rejected as they were", () => {
    expect(verdictBadge("approved")?.label).toBe("✓ approved");
    expect(verdictBadge("rejected")?.label).toBe("✗ rejected");
    expect(cardVerdictChip("rejected")?.label).toBe("AI rejected");
    expect(cardVerdictChip("approved")).toBeNull();
    expect(verdictBadge(null)).toBeNull();
  });
});
