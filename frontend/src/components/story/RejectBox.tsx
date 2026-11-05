import { useState } from "react";

import type { StoryActionName } from "@/components/story/StoryActions";
import { PRIMARY_BUTTON_DISABLED, SECONDARY_BUTTON } from "@/components/ui";

/**
 * Reject-reason composer shown under the action row while a story sits in
 * human_review and the reviewer pressed "Reject & requeue". Owns the draft
 * text so the parent only tracks whether the box is open at all; the reason
 * is fed into the next spec run.
 */
export function RejectBox({
  run,
  onClose,
}: {
  run: (action: StoryActionName, body?: Record<string, string>) => Promise<void>;
  onClose: () => void;
}) {
  const [reason, setReason] = useState("");
  return (
    <div className="shrink-0 space-y-2 border-b border-zinc-800/70 bg-zinc-900/40 px-5 py-3">
      <textarea
        autoFocus
        rows={2}
        value={reason}
        onChange={(e) => setReason(e.target.value)}
        placeholder="What must change? (fed into the next spec run)"
        className="w-full resize-none rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2 text-xs text-zinc-200 placeholder-zinc-600 outline-none focus:border-zinc-500"
      />
      <div className="flex gap-2">
        <button
          disabled={!reason.trim()}
          onClick={async () => {
            await run("reject", { reason: reason.trim() });
            onClose();
          }}
          className={PRIMARY_BUTTON_DISABLED}
        >
          Send back to queue
        </button>
        <button onClick={onClose} className={SECONDARY_BUTTON}>
          Cancel
        </button>
      </div>
    </div>
  );
}
