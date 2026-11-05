import { useState } from "react";

import { signInWithToken, UnauthorizedError } from "@/api";
import { PRIMARY_BUTTON_DISABLED, PANEL, FIELD_LABEL } from "@/components/ui";

/** Error text shown for a wrong/missing token. Exported for the node-env
 * tests (no DOM in this repo's vitest setup), which assert the mapping. */
export const INVALID_TOKEN_MESSAGE = "Token is not valid";

export type SignInOutcome = "ok" | "invalid" | "error";

/**
 * The sign-in flow, kept out of the component so vitest can drive it without
 * a DOM: POST the token, reload on success, classify the failure.
 *
 * On 200 the backend has set the HttpOnly `fubala_session` cookie, so the
 * reload boots the full shell with its SSE streams. The token itself is never
 * written anywhere — not web storage, not the URL — it exists only as the
 * Bearer header of this one request.
 */
export async function performSignIn(token: string): Promise<SignInOutcome> {
  try {
    await signInWithToken(token);
  } catch (err) {
    return err instanceof UnauthorizedError ? "invalid" : "error";
  }
  window.location.reload();
  return "ok";
}

/**
 * Sign-in gate shown when the API says 401: the browser has no session
 * cookie and is not on loopback. The operator pastes the API token once and
 * the backend mints the cookie; the page then reloads into the normal shell.
 */
export function SignInView() {
  const [token, setToken] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    const value = token.trim();
    if (!value || busy) return;
    setBusy(true);
    setError(null);
    const outcome = await performSignIn(value);
    if (outcome === "ok") return; // reload is coming; leave the form alone
    setBusy(false);
    setError(outcome === "invalid" ? INVALID_TOKEN_MESSAGE : "Sign-in failed");
  };

  return (
    <div className="flex h-full items-center justify-center bg-zinc-950 p-6">
      <form onSubmit={submit} className={`${PANEL} w-full max-w-sm space-y-4`}>
        <div className="space-y-1">
          <h1 className="text-lg font-semibold text-zinc-100">Fubala</h1>
          <p className="text-xs text-zinc-500">
            This board is protected by an API token.
          </p>
        </div>

        <label className="block space-y-1.5">
          <span className={FIELD_LABEL}>API token</span>
          <input
            type="password"
            name="fubala-token"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            autoComplete="off"
            autoFocus
            spellCheck={false}
            className="w-full rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm text-zinc-100 outline-none focus:border-zinc-500"
          />
        </label>

        {error !== null && (
          <p className="text-xs text-red-400" role="alert">
            {error}
          </p>
        )}

        <button
          type="submit"
          disabled={busy || token.trim() === ""}
          className={PRIMARY_BUTTON_DISABLED}
        >
          {busy ? "Signing in…" : "Sign in"}
        </button>

        <p className="text-[11px] text-zinc-500">
          Get it from Settings → API token, or ask the coordinator
        </p>
      </form>
    </div>
  );
}
