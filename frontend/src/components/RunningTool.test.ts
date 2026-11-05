import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { isToolSlow, RunningToolList, runningToolLabel } from "@/components/RunningTool";
import { parseActivityEvent, type RunningTool } from "@/types";

const T0 = Date.parse("2025-01-01T00:00:00.000Z");
const mk = (over: Partial<RunningTool> = {}): RunningTool => ({
  call_id: "a",
  name: "run_verify",
  args: "{}",
  started_at: "2025-01-01T00:00:00.000Z",
  timeout_s: 600,
  ...over,
});

describe("runningToolLabel", () => {
  it("formats minutes and seconds", () => {
    expect(runningToolLabel(mk(), T0 + 252_000)).toBe("running: run_verify, 4 min 12 s");
    expect(runningToolLabel(mk({ name: "x" }), T0 + 9_000)).toBe("running: x, 9 s");
  });
  it("clamps negative elapsed time", () => {
    expect(runningToolLabel(mk({ name: "x" }), T0 - 5_000)).toBe("running: x, 0 s");
  });
});

describe("isToolSlow", () => {
  it("is true only past half the timeout", () => {
    expect(isToolSlow(mk(), T0 + 300_000)).toBe(false);
    expect(isToolSlow(mk(), T0 + 301_000)).toBe(true);
  });
});

describe("RunningToolList (compact badge body)", () => {
  const render = (tools: RunningTool[]) =>
    renderToStaticMarkup(createElement(RunningToolList, { tools, compact: true, now: T0 + 252_000 }));
  it("shows the oldest tool plus +N, and nothing when empty", () => {
    const html = render([mk(), mk({ call_id: "b", name: "other" })]);
    expect(html).toContain("running: run_verify");
    expect(html).toContain("+1");
    expect(render([])).toBe("");
  });
});

describe("parseActivityEvent span events", () => {
  it("ignores tool_started and tool_finished", () => {
    expect(parseActivityEvent("tool_started", "{}")).toBeNull();
    expect(parseActivityEvent("tool_finished", "{}")).toBeNull();
  });
});
