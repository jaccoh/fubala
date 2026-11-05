import { useStore } from "@/store";

/** Live operator prompts (zcode-style): agents asking to leave their dirs. */
export function PermissionPrompts() {
  const pending = useStore((s) => s.pendingPermissions);
  const decide = useStore((s) => s.decidePermission);
  const stories = useStore((s) => s.stories);

  if (pending.length === 0) return null;

  return (
    <div className="fixed bottom-4 right-4 z-50 w-96 max-w-[92vw] space-y-2">
      {pending.slice(0, 4).map((p) => {
        const story = p.story_id ? stories.find((s) => s.id === p.story_id) : null;
        return (
          <div
            key={p.id}
            className="animate-pulse-once rounded-xl border border-amber-700/60 bg-zinc-950/95 p-3 shadow-2xl backdrop-blur"
            style={{ animation: "none" }}
          >
            <p className="mb-1 flex items-center gap-1.5 text-[11px] font-semibold tracking-wide text-amber-300 uppercase">
              🔐 permission request {story && <span className="text-zinc-500 normal-case">· {story.title}</span>}
            </p>
            <p className="truncate font-mono text-xs text-zinc-200" title={p.value}>
              {p.value}
            </p>
            {p.reason && <p className="mt-0.5 text-[11px] text-zinc-400">“{p.reason}”</p>}
            <div className="mt-2 flex gap-2">
              <button
                onClick={() => decide(p.id, "approve")}
                className="rounded-lg bg-emerald-600 px-3 py-1 text-[11px] font-medium text-white transition hover:bg-emerald-500"
              >
                Approve
              </button>
              <button
                onClick={() => decide(p.id, "deny")}
                className="rounded-lg border border-zinc-700 px-3 py-1 text-[11px] text-zinc-300 transition hover:bg-zinc-800"
              >
                Deny
              </button>
              <span className="ml-auto self-center text-[10px] text-zinc-600">
                approve remembers the path
              </span>
            </div>
          </div>
        );
      })}
      {pending.length > 4 && (
        <p className="text-center text-[11px] text-zinc-500">+{pending.length - 4} more…</p>
      )}
    </div>
  );
}
