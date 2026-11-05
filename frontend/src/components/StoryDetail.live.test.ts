import { describe, expect, it } from "vitest";

import { extractLastTool } from "@/components/story/LiveStrip";
import { reviewTeaser } from "@/components/story/OutcomeSection";

describe("extractLastTool", () => {
  it("returns null for an empty log (status transition wipes storyLogs)", () => {
    expect(extractLastTool("")).toBeNull();
  });

  it("returns null when the log has no tool lines", () => {
    expect(extractLastTool("[spec] using model zai/glm-5.3\nwriting story\n")).toBeNull();
  });

  it("parses name and args from a tool line", () => {
    const log = "[spec] using model zai/glm-5.3\n🔧 write_file(path='a.ts') → 120 chars\n";
    expect(extractLastTool(log)).toEqual({ name: "write_file", args: "path='a.ts'" });
  });

  it("scans bottom-up: returns the LAST tool call, not the first", () => {
    const log = [
      "🔧 read_file(path='old.py') → 10 chars",
      "[executing] editing",
      "🔧 edit_file(path='new.py') → 20 chars",
    ].join("\n");
    expect(extractLastTool(log)).toEqual({ name: "edit_file", args: "path='new.py'" });
  });

  it("truncates long args to ~48 chars with an ellipsis", () => {
    const longArgs = "x".repeat(80);
    const tool = extractLastTool(`🔧 run_command(${longArgs}) → 5 chars`);
    expect(tool).not.toBeNull();
    expect(tool!.args.length).toBe(49); // 48 chars + …
    expect(tool!.args.endsWith("…")).toBe(true);
  });

  it("ignores non-tool lines that merely mention the wrench emoji", () => {
    expect(extractLastTool("🔧 nope no parens here")).toBeNull();
  });
});

describe("reviewTeaser", () => {
  it("returns '' for null/empty/whitespace reviews", () => {
    expect(reviewTeaser(null)).toBe("");
    expect(reviewTeaser(undefined)).toBe("");
    expect(reviewTeaser("   ")).toBe("");
  });

  it("returns the first two sentences when they fit the budget", () => {
    const review = "Added the diff explorer. Tests stay green. Third sentence is dropped.";
    expect(reviewTeaser(review)).toBe("Added the diff explorer. Tests stay green.");
  });

  it("returns one sentence when the second would overflow the budget", () => {
    const long = "First sentence. " + "x".repeat(300);
    expect(reviewTeaser(long)).toBe("First sentence.");
  });

  it("hard-caps at ~240 chars with an ellipsis when there is no terminator", () => {
    const blob = "y".repeat(400);
    const out = reviewTeaser(blob);
    expect(out.length).toBe(240); // 239 chars + …
    expect(out.endsWith("…")).toBe(true);
  });

  it("keeps a short terminator-free review whole", () => {
    expect(reviewTeaser("one long line without punctuation")).toBe(
      "one long line without punctuation",
    );
  });

  it("never exceeds the budget", () => {
    const mixed = `${"a. ".repeat(200)}final`;
    expect(reviewTeaser(mixed).length).toBeLessThanOrEqual(240);
  });
});
