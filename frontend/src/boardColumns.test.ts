/**
 * Column-assignment contract: a story renders in exactly one column — the one
 * implied by its `status` — and by nothing else.
 *
 * The board defect report (2026-09-27) suspected cards landing in the wrong
 * column. The hypothesized-but-wrong cause was assignment derived from
 * `claimed_by` / `execution` / `ai_verdict`; these tests pin the correct
 * behavior so nobody "fixes" the board that way.
 */
import { describe, expect, it } from "vitest";

import { COLUMNS, belongsToColumn, columnForStatus } from "@/components/boardColumns";
import type { Story, StoryStatus } from "@/types";

const EVERY_STATUS: StoryStatus[] = [
  "backlog",
  "queued",
  "in_progress",
  "ai_review",
  "human_review",
  "executing",
  "landing",
  "done",
  "failed",
  "superseded",
];

function makeStory(overrides: Partial<Story> = {}): Story {
  return {
    id: "story-1",
    title: "Column contract",
    description: "",
    acceptance: "",
    status: "queued",
    project_id: null,
    epic_id: null,
    depends_on: [],
    model: null,
    skill: null,
    created_by: "tester",
    execution: "internal",
    execution_override: null,
    // gates: human by default, exactly what the API stamps at create
    gate_spec: "human",
    gate_code: "human",
    priority: "normal",
    human_rejected: false,
    kind: "code",
    claimed_by: null,
    claimed_at: null,
    spec: null,
    spec_version: 1,
    base_ref: null,
    verify_result: null,
    ai_review: null,
    ai_verdict: null,
    approved_by: null,
    error: null,
    // push+PR delivery fields: null until approval opens a pull request
    pr_url: null,
    pr_index: null,
    pushed_sha: null,
    created_at: "2026-09-27T00:00:00Z",
    updated_at: "2026-09-27T00:00:00Z",
    ...overrides,
  };
}

describe("columnForStatus", () => {
  it("maps every status to its own column", () => {
    for (const status of EVERY_STATUS) {
      expect(columnForStatus(status).status).toBe(status);
    }
  });

  it("has a column for every status (and no duplicates)", () => {
    const columnStatuses = COLUMNS.map((c) => c.status);
    expect(columnStatuses).toEqual(expect.arrayContaining(EVERY_STATUS));
    expect(new Set(columnStatuses).size).toBe(columnStatuses.length);
  });

  it("is status-only: external + claimed + AI-rejected stories stay in their own column", () => {
    // The exact combination a claimed external story carries mid-cycle —
    // none of these fields may move the card to a different column.
    for (const status of EVERY_STATUS) {
      const story = makeStory({
        status,
        execution: "external",
        claimed_by: "outside-agent",
        ai_verdict: "rejected",
      });
      expect(columnForStatus(story.status).status).toBe(status);
      // and it belongs to that column alone
      for (const column of COLUMNS) {
        expect(belongsToColumn(story, column.status)).toBe(column.status === status);
      }
    }
  });

  it("belongsToColumn decides membership on status alone", () => {
    const claimed = makeStory({ status: "human_review", execution: "external", claimed_by: "x" });
    expect(belongsToColumn(claimed, "human_review")).toBe(true);
    expect(belongsToColumn(claimed, "in_progress")).toBe(false);
    expect(belongsToColumn(claimed, "done")).toBe(false);
  });
});
