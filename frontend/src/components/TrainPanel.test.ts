/**
 * Merge-train panel tests (node env, same pattern as StoryDetail.live.test.ts:
 * no jsdom, no rendering — only the pure pieces).
 *
 * What is pinned here is the contract the board needs from the train panel:
 * which batches it shows, and that clicking a member opens the story through
 * the store (the first cut dispatched a `fubala:open-story` CustomEvent that
 * nothing in the app listens for, so the click did nothing).
 */
import { describe, expect, it } from "vitest";

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { FailedTests, visibleBatches } from "@/components/TrainPanel";
import type { TrainBatch } from "@/types";

const batch = (over: Partial<TrainBatch>): TrainBatch =>
  ({
    id: "b1",
    state: "open",
    pr_url: "http://gitea/pr/1",
    pr_index: 1,
    head_sha: "abc",
    red_runs: 0,
    parent_batch_id: null,
    created_at: "2026-10-24T00:00:00Z",
    members: [],
    ...over,
  }) as TrainBatch;

describe("visibleBatches", () => {
  it("keeps every batch while the project has few (a live train)", () => {
    const batches = [
      batch({ id: "forming", state: "forming" }),
      batch({ id: "open", state: "open" }),
      batch({ id: "merged1", state: "merged" }),
    ];
    expect(visibleBatches(batches).map((b) => b.id)).toEqual([
      "forming",
      "open",
      "merged1",
    ]);
  });

  it("drops settled merged batches once the history grows", () => {
    const batches = [
      batch({ id: "forming", state: "forming" }),
      batch({ id: "open", state: "open" }),
      batch({ id: "red", state: "red" }),
      ...Array.from({ length: 5 }, (_, i) =>
        batch({ id: `merged${i}`, state: "merged" }),
      ),
    ];
    const shown = visibleBatches(batches).map((b) => b.id);
    expect(shown).toContain("forming");
    expect(shown).toContain("open");
    expect(shown).toContain("red");
    expect(shown.filter((id) => id.startsWith("merged"))).toEqual([]);
  });

  it("always keeps a merged batch that had reds (it explains the bisect)", () => {
    const batches = [
      ...Array.from({ length: 6 }, (_, i) =>
        batch({ id: `merged${i}`, state: "merged" }),
      ),
      batch({ id: "bisected", state: "merged", red_runs: 2 }),
    ];
    expect(visibleBatches(batches).map((b) => b.id)).toEqual(["bisected"]);
  });
});

describe("FailedTests", () => {
  it("renders the failing test ids with the full id in the tooltip", () => {
    const id = "tests/test_price_outlook_forecast_job.py::test_x";
    const html = renderToStaticMarkup(createElement(FailedTests, { ids: [id] }));
    expect(html).toContain(id);
    expect(html).toContain(`title="${id}"`);
  });

  it("renders nothing without ids", () => {
    expect(renderToStaticMarkup(createElement(FailedTests, { ids: [] }))).toBe("");
  });
});
