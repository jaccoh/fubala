/**
 * Slot-state labels (node env, no rendering): the live strip and the board
 * card show the same three words for the server's in-memory slot_state.
 */
import { describe, expect, it } from "vitest";

import { SLOT_STATES } from "@/components/story/LiveStrip";

describe("SLOT_STATES", () => {
  it("labels every slot state the API can send", () => {
    expect(SLOT_STATES.working.label).toBe("working");
    expect(SLOT_STATES.waiting_tests.label).toBe("waiting on tests");
    expect(SLOT_STATES.waiting_slot.label).toBe("waiting on slot");
  });
});
