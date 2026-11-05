"""Epics: the grouping layer above stories."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from ..db import session_scope
from ..models import Epic, Story, StoryStatus, utcnow

router = APIRouter(prefix="/epics", tags=["epics"])


class EpicCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=20_000)


class EpicUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    status: str | None = Field(default=None, pattern="^(open|done|archived)$")


def epic_out(db: Session, epic: Epic) -> dict:
    stories = db.exec(select(Story).where(Story.epic_id == epic.id)).all()
    return {
        "id": epic.id,
        "title": epic.title,
        "description": epic.description,
        "status": epic.status,
        "story_count": len(stories),
        "done_count": sum(1 for s in stories if s.status == StoryStatus.DONE),
    }


@router.get("")
def list_epics(request: Request) -> list[dict]:
    with session_scope(request.app.state.engine) as db:
        epics = db.exec(select(Epic).order_by(Epic.updated_at.desc())).all()
        return [epic_out(db, e) for e in epics]


@router.post("", status_code=status.HTTP_201_CREATED)
def create_epic(payload: EpicCreate, request: Request) -> dict:
    with session_scope(request.app.state.engine) as db:
        epic = Epic(title=payload.title, description=payload.description)
        db.add(epic)
        db.flush()
        return epic_out(db, epic)


@router.patch("/{epic_id}")
def update_epic(epic_id: str, payload: EpicUpdate, request: Request) -> dict:
    with session_scope(request.app.state.engine) as db:
        epic = db.get(Epic, epic_id)
        if epic is None:
            raise HTTPException(status_code=404, detail="epic not found")
        for field in ("title", "description", "status"):
            value = getattr(payload, field)
            if value is not None:
                setattr(epic, field, value)
        epic.updated_at = utcnow()
        db.add(epic)
        db.flush()
        return epic_out(db, epic)


@router.delete("/{epic_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_epic(epic_id: str, request: Request) -> Response:
    with session_scope(request.app.state.engine) as db:
        epic = db.get(Epic, epic_id)
        if epic is None:
            raise HTTPException(status_code=404, detail="epic not found")
        for story in db.exec(select(Story).where(Story.epic_id == epic_id)).all():
            story.epic_id = None
            db.add(story)
        db.delete(epic)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
