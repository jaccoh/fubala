# Spec — Story notes: append-only work-log surface (REST + MCP + UI)

## Goal
Agents and humans can append timestamped, redacted notes to any story via REST, MCP, and the story panel, giving MCP-driven agents a resumable STATE/VERIFIED/NEXT/BLOCKERS log that survives description immutability.

## Context
- `backend/app/models.py`: `StoryEvent.kind` docstring already reserves `"note"`; tables are created via SQLModel `create_all` at startup — new table needs no migration.
- `backend/app/routers/stories.py`: helpers `_get_story()`, `_commit_and_publish()` (records event + snapshots + publishes); SSE at `GET /stories/events` emits `event: {kind}` with JSON data. Existing action routes are distinct paths (`/{story_id}/queue` etc.) — no catch-all, so `POST /{story_id}/notes` has no route conflict.
- `backend/app/events.py`: `bus.publish(kind, data)`. `backend/app/runner.py:135`: `record_event()`.
- `backend/app/guardrails.py:65`: `redact_secrets()` — reuse; do not duplicate patterns.
- `backend/app/mcp.py`: `_get`/`_post` helpers (`_post` raises `RuntimeError("status: detail")`); `get_story` renders markdown sections.
- Frontend: `types.ts`, `api.ts`, `store.ts` (`connectBoard`, line 364), `components/StoryDetail.tsx`.
- External spec file (`bank_transactions_importer/docs/…/001-story-notes.md`) is unreachable from this repo; this spec is grounded in the story's ACs plus the code above. Decisions left implicit there are made explicit below.

## Implementation steps
1. **`models.py`**: add `StoryNote` table: `id: int` auto PK, `story_id: str` (FK `stories.id`, indexed), `body: str`, `author: str = "local"`, `created_at` default `utcnow()`. No update/delete anything — append-only by schema.
2. **`schemas.py`**: `StoryNoteCreate` (`body: str = Field(min_length=1, max_length=8192)`, `author: str = Field(default="local", max_length=100)`); `StoryNoteOut` (`id, story_id, body, author, created_at`); add `notes: list[StoryNoteOut] = []` to `StoryDetailOut`.
3. **`routers/stories.py`**: `POST /stories/{story_id}/notes` (201 + `StoryNoteOut`). Semantics — all decided here:
   - 404 unknown story (via `_get_story`).
   - Empty body → pydantic 422 (`min_length=1`); whitespace-only body → explicit 400 (`not body.strip()`).
   - **Redact first** (`redact_secrets(body)`), **then** enforce `len(body.encode("utf-8")) <= 8192` → 400 with actual byte count. Byte check is post-redaction because placeholders change length; it is bytes not chars so CJK/emoji state dumps are capped honestly. Cap is **per note**, not per story.
   - Allowed in **every** status including `done` (it is the resumable log, unlike mutable fields).
   - Persist row; `record_event(db, id, "note", {"author": author, "preview": body[:120]})`; bump `updated_at`; after scope, `bus.publish("story_note", {"story_id": id, "note": note_out.model_dump(mode="json")})`. Do **not** reuse `_commit_and_publish` (it publishes kind `"story"`); mirror its ordering.
4. **`GET /stories/{story_id}`**: query `StoryNote` `order_by(StoryNote.id)` (oldest-first), set `out.notes`.
5. **`mcp.py`**: `add_story_note(story_id: str, body: str, author: str = "mcp")` tool → POST, surfacing `RuntimeError` details like `story_action`; `get_story` renders last 20 notes under `## Notes` with an `…N older` prefix line when truncated, using `story.get("notes", [])` for forward-compat.
6. **Frontend**: `types.ts` add `StoryNote`, extend `StoryDetail`; `api.ts` add `addStoryNote(id, body)`; `store.ts` add `story_note` listener in `connectBoard` (if `openStoryId` matches, refetch detail) + `addStoryNote` action; `StoryDetail.tsx` add notes timeline (author, relative time, body) plus composer textarea. Render bodies as plain preformatted text — **not** the Markdown component (notes are external-agent output; injection containment).
7. **Tests** (`backend/tests/test_stories.py`, one client fixture per tmp DB):
   - 201 happy path returns full payload; unknown id → 404.
   - 8193-byte body → 400; exactly-8192-byte CJK body → 201.
   - Empty → 422; `"   "` → 400.
   - Body containing `sk-ant-…` stored and served redacted (assert `REDACTED` present, secret absent).
   - Two POSTs accumulate in insertion order; both in `GET` `notes`; a `note`-kind `StoryEvent` row exists.
   - Note on a `done` story → 201; `PATCH`/`DELETE` on `/stories/{id}/notes` → 405.
   - SSE: subscribe to `/stories/events`, post a note, assert a `story_note` event arrives.
   - MCP: monkeypatch `app.mcp._post`, assert `add_story_note` hits `/stories/{id}/notes` with the body and renders the error detail on failure; `get_story` includes `## Notes`.

## Risks & edge cases
- **Truncation vs cap**: a body that is valid chars may exceed bytes after redaction — the ordered redact→byte-check flow in step 3 is the contract; test covers the boundary on both sides.
- **Notes on `done` stories** are intentional (log must outlive the story); reviewers should not "fix" this into the editable-status gate.
- **No moderation path**: append-only means a bad note is permanent by design — flagged, not solved, this story.
- **SSE ordering**: publish after the DB scope commits, or consumers refetch a row that isn't there yet.
- Old `StoryEvent` rows already use kind `"note"` for manual edits (stories.py:243); the UI timeline must distinguish system history events from `StoryNote` rows — separate sections, never merged.

## Acceptance checks
- [ ] `POST /api/v1/stories/{id}/notes` → 201 `{id, story_id, body, author, created_at}`; 404 unknown story; 400 whitespace-only; 422 empty.
- [ ] 8KiB = 8192 **bytes** per note, enforced after redaction: 8193 bytes → 400, exact-8192-byte CJK body → 201.
- [ ] Note containing `sk-ant-…` is stored and served as `[REDACTED:…]` (secret string absent).
- [ ] Append-only: POSTs accumulate oldest-first; `PATCH`/`DELETE` on `/notes` → 405; notes allowed on `done` stories.
- [ ] `GET /stories/{id}` includes `notes`; a `note` `StoryEvent` exists; SSE stream emits `story_note`.
- [ ] MCP `add_story_note` posts successfully and reports failures; `get_story` output contains `## Notes`.
- [ ] Story panel shows notes timeline + composer; new notes appear live via SSE without refresh; bodies render as plain text.
- [ ] `cd backend && uv run pytest` all green; `npx tsc -b --noEmit && npm run build` clean.
