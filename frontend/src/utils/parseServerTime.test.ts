import { describe, expect, it } from "vitest";

import { parseServerTime } from "./parseServerTime";

describe("parseServerTime", () => {
  it("treats offset-less timestamps as UTC", () => {
    // Legacy naive strings must not be read as local time.
    expect(parseServerTime("2026-10-16T14:29:43").getTime()).toBe(
      Date.UTC(2026, 9, 16, 14, 29, 43),
    );
  });

  it("parses Z-suffixed timestamps to the same instant", () => {
    expect(parseServerTime("2026-10-16T14:29:43Z").getTime()).toBe(
      parseServerTime("2026-10-16T14:29:43").getTime(),
    );
  });

  it("parses +00:00-suffixed timestamps to the same instant", () => {
    expect(parseServerTime("2026-10-16T14:29:43.388192+00:00").getTime()).toBe(
      Date.UTC(2026, 9, 16, 14, 29, 43, 388),
    );
  });

  it("passes explicit non-zero offsets through unchanged", () => {
    // +02:00 must be honoured, not re-anchored to UTC.
    expect(parseServerTime("2026-10-16T14:29:43+02:00").getTime()).toBe(
      Date.UTC(2026, 9, 16, 12, 29, 43),
    );
  });

  it("passes date-only strings through untouched", () => {
    // Per the ES date-time spec these are already UTC — no `Z` appended.
    expect(parseServerTime("2026-10-16").getTime()).toBe(
      Date.UTC(2026, 9, 16),
    );
  });
});
