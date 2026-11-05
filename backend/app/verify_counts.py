"""Pytest counts parsed from a verify output, so the coordinator can trust a
verify result as numbers instead of an exit code alone."""

from __future__ import annotations

import re

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_WORDS = (
    "passed|failed|skipped|errors?|xfailed|xpassed|deselected|warnings?|reruns?"
)
_ITEM = rf"\d+ (?:{_WORDS})"
# a real pytest final line: optional `=` fences, "<n> <word>" items, " in <float>s"
_SUMMARY = re.compile(
    rf"^\s*(?:=+\s*)?(?P<items>{_ITEM}(?:, {_ITEM})*) in \d+(?:\.\d+)?s"
    r"(?: \(\d+:\d{2}:\d{2}\))?(?:\s*=*)?\s*$"
)
_KEYS = {
    "passed": "passed",
    "failed": "failed",
    "skipped": "skipped",
    "error": "errors",
    "errors": "errors",
    "xfailed": "xfailed",
    "xpassed": "xpassed",
    "deselected": "deselected",
    "warning": "warnings",
    "warnings": "warnings",
    "rerun": "reruns",
    "reruns": "reruns",
}
_RAN = ("passed", "failed", "errors", "skipped", "xfailed", "xpassed")

KEEP_HEAD = 1500
KEEP_TAIL = 2500
MAX_UNCUT = KEEP_HEAD + KEEP_TAIL


def _summary_lines(output: str) -> list[tuple[int, str, dict[str, int]]]:
    """(offset in the ANSI-stripped text, line, counts) per real summary line."""
    found = []
    pos = 0
    for line in _ANSI.sub("", output).splitlines(keepends=True):
        m = _SUMMARY.match(line.rstrip("\r\n"))
        if m:
            counts: dict[str, int] = {}
            for item in m.group("items").split(", "):
                n, word = item.split(" ", 1)
                key = _KEYS[word]
                counts[key] = counts.get(key, 0) + int(n)
            found.append((pos, line.strip(), counts))
        pos += len(line)
    return found


def parse_summaries(output: str) -> dict | None:
    """{"lanes": [...], "totals": {...}, "ran": int}, one lane per summary
    line in order; None when the output has no pytest final line."""
    lanes = [c for _, _, c in _summary_lines(output)]
    if not lanes:
        return None
    totals: dict[str, int] = {}
    for lane in lanes:
        for k, v in lane.items():
            totals[k] = totals.get(k, 0) + v
    return {
        "lanes": lanes,
        "totals": totals,
        "ran": sum(totals.get(k, 0) for k in _RAN),
    }


def trim_output(output: str) -> str:
    """Head + tail of a long output, keeping every summary line that would
    fall in the omitted middle. Output up to 4000 chars is returned as is."""
    if len(output) <= MAX_UNCUT:
        return output
    text = _ANSI.sub("", output) if _ANSI.search(output) else output
    head, tail = text[:KEEP_HEAD], text[-KEEP_TAIL:]
    tail_start = len(text) - KEEP_TAIL
    middle = [
        line
        for pos, line, _ in _summary_lines(text)
        if KEEP_HEAD <= pos < tail_start
    ]
    omitted = max(tail_start - KEEP_HEAD, 0)
    parts = [head, f"\n... [{omitted} chars omitted] ...\n"]
    if middle:
        parts.append("\n".join(middle) + "\n... [end of kept summary lines] ...\n")
    parts.append(tail)
    return "".join(parts)


def count_mismatch(rc: int, counts: dict | None) -> str | None:
    """Why an exit-0 verify must still count as failed, or None."""
    if rc != 0 or not counts:
        return None
    totals = counts.get("totals") or {}
    failed, errors = totals.get("failed", 0), totals.get("errors", 0)
    if failed + errors <= 0:
        return None
    bits = []
    if failed:
        bits.append(f"{failed} failed")
    if errors:
        bits.append(f"{errors} errors")
    return "exit 0 but " + ", ".join(bits)
