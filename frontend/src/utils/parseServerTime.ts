/**
 * Parse a timestamp from the API into a Date, treating offset-less strings
 * as UTC. The backend stores UTC, but legacy rows and hand-built payloads
 * may arrive without an explicit offset; `new Date(iso)` would read those
 * as *local* time and skew every relative clock ("2h ago", timeline times)
 * outside of UTC. Appending `Z` before parsing makes the intent explicit.
 *
 * - Already-aware strings (`…Z`, `…+02:00`, `…+02`) pass through untouched —
 *   never a double `Z`.
 * - Date-only values (`2026-10-16`, already UTC per the ES date-time string
 *   spec) pass through untouched too.
 */
export function parseServerTime(iso: string): Date {
  const hasTime = /T/i.test(iso);
  const endsWithOffset = /(?:Z|[+-]\d{2}(?::?\d{2})?)$/i.test(iso);
  if (!hasTime || endsWithOffset) {
    return new Date(iso);
  }
  return new Date(`${iso}Z`);
}
