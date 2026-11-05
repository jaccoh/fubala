"""Project brief: a versioned Markdown description of a project.

The brief is written to be read by an LLM in one go and by a human. Versions
are append-only rows in `project_brief_versions`; the current brief is the
highest version. When a project opts in (`brief_in_spec_prompt`), the spec
writer gets ONLY the "Owner rules" and "Non-goals" sections plus the story's
epic description, never the whole brief. The coder never gets the brief.
"""

from __future__ import annotations

import math
import re

from sqlmodel import Session, select

from .models import Epic, Project, ProjectBriefVersion
from .schemas import as_utc_iso

DEFAULT_BRIEF_TOKEN_BUDGET = 8000

BRIEF_TEMPLATE = """\
## Purpose and owner goals
goal: what this project is for and what the owner wants from it

## Owner rules
rule: a hard constraint every story must respect

## Non-goals
non-goal: something this project deliberately does not do

## Live state
status: what runs where today, and its version

## Architecture map
component: path/to/module -- what it does

## How we work
workflow: branches, reviews, tests and deploys

## Open threads and next steps
next: the work in flight and what comes after it

## IDs and links
repo: https://git.example.com/owner/project
"""

GUARD_SECTIONS = ("owner rules", "non-goals")

_FENCE = re.compile(r"^\s{0,3}(```|~~~)")
# Only level-1 and level-2 headings end a section; ### stays inside it.
_SECTION_HEADING = re.compile(r"^\s{0,3}(#{1,2})(?:\s+(.*?))?\s*#*\s*$")


def approx_tokens(text: str) -> int:
    return math.ceil(len(text or "") / 4)


def brief_budget(project: Project) -> int:
    return int(project.brief_token_budget or DEFAULT_BRIEF_TOKEN_BUDGET)


def current_brief(db: Session, project_id: str) -> ProjectBriefVersion | None:
    return db.exec(
        select(ProjectBriefVersion)
        .where(ProjectBriefVersion.project_id == project_id)
        .order_by(ProjectBriefVersion.version.desc())
        .limit(1)
    ).first()


def brief_version(
    db: Session, project_id: str, version: int
) -> ProjectBriefVersion | None:
    return db.exec(
        select(ProjectBriefVersion).where(
            ProjectBriefVersion.project_id == project_id,
            ProjectBriefVersion.version == version,
        )
    ).first()


def brief_out(project: Project, row: ProjectBriefVersion | None) -> dict:
    body = row.body if row is not None else ""
    tokens = approx_tokens(body)
    budget = brief_budget(project)
    return {
        "project_id": project.id,
        "version": row.version if row is not None else 0,
        "body": body,
        "author": row.author if row is not None else "",
        "updated_at": as_utc_iso(row.created_at) if row is not None else None,
        "tokens": tokens,
        "budget": budget,
        "over_budget": tokens > budget,
    }


def _scan_sections(body: str, wanted) -> dict[str, str]:
    """The `##` sections of `body` whose heading text (trimmed, lowercased)
    satisfies `wanted`: {heading as written: content (stripped)}, in document
    order. A section runs until the next `#` or `##` heading; `###`
    subheadings stay inside it, and lines in fenced code blocks are never
    headings."""
    sections: dict[str, list[str]] = {}
    current: str | None = None
    in_fence = False
    for line in (body or "").splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
        elif not in_fence:
            match = _SECTION_HEADING.match(line)
            if match:
                title = (match.group(2) or "").strip()
                if len(match.group(1)) == 2 and wanted(title.lower()):
                    current = title
                    sections.setdefault(current, [])
                else:
                    current = None
                continue
        if current is not None:
            sections[current].append(line)
    return {title: "\n".join(lines).strip() for title, lines in sections.items()}


def extract_guard_sections(body: str) -> dict[str, str]:
    """The "Owner rules" and "Non-goals" `##` sections of a brief.

    Returns {heading text as written: section content (stripped)}, in
    document order. The heading match is exact after trim, case-insensitive.
    """
    return _scan_sections(body, lambda title: title in GUARD_SECTIONS)


def extract_section(body: str, title: str) -> str:
    """The stripped content of one `##` section (same heading and fence rules
    as extract_guard_sections), or "" when it is missing. If the heading
    occurs twice, the first non-empty one wins."""
    wanted = title.strip().lower()
    found = _scan_sections(body, lambda t: t == wanted)
    return next((c for c in found.values() if c), "")


def spec_prompt_block(
    db: Session, project_id: str | None, epic_id: str | None
) -> tuple[str, int | None]:
    """(text, version) for the spec writer's system prompt.

    ("", None) when the project has not opted in, has no brief, or both
    guard sections are missing or empty."""
    if not project_id:
        return "", None
    project = db.get(Project, project_id)
    if project is None or not project.brief_in_spec_prompt:
        return "", None
    row = current_brief(db, project_id)
    if row is None:
        return "", None
    sections = {t: c for t, c in extract_guard_sections(row.body).items() if c}
    if not sections:
        return "", None
    parts = [f"## Project brief v{row.version} (owner rules, non-goals)"]
    for title, content in sections.items():
        parts.append(f"### {title}\n{content}")
    if epic_id:
        epic = db.get(Epic, epic_id)
        if epic is not None and (epic.description or "").strip():
            parts.append(f"### Epic\n{epic.description.strip()}")
    parts.append(f"Write `brief v{row.version}` in the spec's Context section.")
    return "\n\n".join(parts), row.version
