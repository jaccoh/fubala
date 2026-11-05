"""Chat sessions and the SSE message stream."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse
from sqlmodel import Session, select

from ..agent import run_agent, tool_log_line
from ..agent_tools import ToolBox
from ..db import session_scope
from ..guardrails import GuardedExecutor, ToolPolicy
from ..models import ChatMessage, ChatSession, utcnow
from ..providers import GatewayError, Usage
from ..registry import UnknownModelError
from ..schemas import (
    MessageCreate,
    MessageOut,
    SessionCreate,
    SessionDetailOut,
    SessionOut,
    SessionUpdate,
)
from ..settings_store import load_settings

router = APIRouter(prefix="/chat/sessions", tags=["chat"])


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _default_model(request: Request) -> str:
    app = request.app
    return app.state.settings.default_model or app.state.registry.default_model()


def _resolve_model_or_400(request: Request, model: str) -> str:
    try:
        request.app.state.registry.resolve(model)
    except UnknownModelError:
        raise HTTPException(status_code=400, detail=f"unknown model: {model}") from None
    return model


def _get_session(db: Session, session_id: str) -> ChatSession:
    session = db.get(ChatSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return session


def _session_out(s: ChatSession) -> SessionOut:
    return SessionOut.model_validate(s, from_attributes=True)


@router.post("", response_model=SessionOut, status_code=status.HTTP_201_CREATED)
def create_session(payload: SessionCreate, request: Request) -> SessionOut:
    model = _resolve_model_or_400(request, payload.model or _default_model(request))
    session = ChatSession(
        title=payload.title or "New chat", model=model, project_id=payload.project_id
    )
    with session_scope(request.app.state.engine) as db:
        db.add(session)
        db.flush()
        return _session_out(session)


@router.get("", response_model=list[SessionOut])
def list_sessions(request: Request) -> list[SessionOut]:
    with session_scope(request.app.state.engine) as db:
        rows = db.exec(select(ChatSession).order_by(ChatSession.updated_at.desc())).all()
        return [_session_out(s) for s in rows]


@router.get("/{session_id}", response_model=SessionDetailOut)
def get_session(session_id: str, request: Request) -> SessionDetailOut:
    with session_scope(request.app.state.engine) as db:
        session = _get_session(db, session_id)
        messages = db.exec(
            select(ChatMessage)
            .where(ChatMessage.session_id == session_id)
            .order_by(ChatMessage.id)
        ).all()
        out = SessionDetailOut.model_validate(session, from_attributes=True)
        out.messages = [MessageOut.model_validate(m, from_attributes=True) for m in messages]
        return out


@router.patch("/{session_id}", response_model=SessionOut)
def update_session(session_id: str, payload: SessionUpdate, request: Request) -> SessionOut:
    with session_scope(request.app.state.engine) as db:
        session = _get_session(db, session_id)
        if payload.model is not None:
            session.model = _resolve_model_or_400(request, payload.model)
        if payload.title is not None:
            session.title = payload.title
        if payload.project_id is not None:
            session.project_id = payload.project_id
        session.updated_at = utcnow()
        db.add(session)
        return _session_out(session)


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_session(session_id: str, request: Request) -> Response:
    with session_scope(request.app.state.engine) as db:
        _get_session(db, session_id)
        for msg in db.exec(
            select(ChatMessage).where(ChatMessage.session_id == session_id)
        ).all():
            db.delete(msg)
        db.delete(db.get(ChatSession, session_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{session_id}/messages")
def send_message(session_id: str, payload: MessageCreate, request: Request) -> StreamingResponse:
    """Append a user message and stream the assistant reply as SSE.

    Events: `start` (model chosen) → `delta`* (text chunks) → `done` (ids + usage)
    or `error`. On client disconnect the partial assistant message is persisted
    with finish_reason='stopped'; provider failures persist with 'error'.
    """
    app = request.app
    engine = app.state.engine

    with session_scope(engine) as db:
        session = _get_session(db, session_id)
        model = _resolve_model_or_400(request, payload.model or session.model)
        prior = db.exec(
            select(ChatMessage)
            .where(ChatMessage.session_id == session_id)
            .order_by(ChatMessage.id)
        ).all()
        history = [{"role": m.role, "content": m.content} for m in prior]
        history.append({"role": "user", "content": payload.content})
        system_prompt = load_settings(engine).get("chat_system_prompt")
        if system_prompt:
            history.insert(0, {"role": "system", "content": system_prompt})
        db.add(ChatMessage(session_id=session_id, role="user", content=payload.content))
        if payload.model and payload.model != session.model:
            session.model = payload.model
        session.updated_at = utcnow()
        db.add(session)
        project_id = session.project_id

    canonical = model
    settings = load_settings(engine)
    policy = ToolPolicy.from_settings(settings)

    # project roots when the session belongs to one; global root otherwise
    proj_root, proj_extras, proj_id = None, [], None
    if project_id:
        from ..models import Project

        with Session(engine) as db:
            project = db.get(Project, project_id)
            if project is not None:
                import json as _json

                proj_root = project.root
                proj_extras = _json.loads(project.extra_paths or "[]")
                proj_id = project.id

    toolbox = ToolBox(
        engine=engine,
        project_root=proj_root or settings.get("project_root"),
        deny_globs=policy.denied_patterns(),
        extra_roots=[
            p for p in ([proj_root] if proj_root else []) + proj_extras if p
        ] + [p for p in (settings.get("allowed_paths") or "").split(",") if p],
        board_writable=True,
    )
    schemas = toolbox.schemas() if policy.chat_tools_enabled else []
    tools = schemas or None
    guarded = GuardedExecutor(
        toolbox, policy, engine=engine,
        context={"session_id": session_id, "project_id": proj_id},
    )

    async def event_stream() -> AsyncIterator[str]:
        parts: list[str] = []
        usage: Usage | None = None

        def persist(finish_reason: str | None) -> int:
            with session_scope(engine) as db:
                msg = ChatMessage(
                    session_id=session_id,
                    role="assistant",
                    content="".join(parts),
                    model=canonical,
                    prompt_tokens=usage.prompt_tokens if usage else None,
                    completion_tokens=usage.completion_tokens if usage else None,
                    finish_reason=finish_reason,
                )
                db.add(msg)
                db.flush()
                s = db.get(ChatSession, session_id)
                if s is not None:
                    s.updated_at = utcnow()
                    db.add(s)
                return msg.id or -1

        try:
            yield _sse(
                "start",
                {
                    "model": canonical,
                    "tools": [t["function"]["name"] for t in (tools or [])],
                },
            )

            # run_agent drives sync callbacks; bridge them into an async queue
            queue: asyncio.Queue = asyncio.Queue()

            async def pump() -> None:
                nonlocal usage
                try:
                    result = await run_agent(
                        app.state.gateway,
                        model=canonical,
                        messages=history,
                        tools=tools,
                        execute=guarded,
                        on_delta=lambda text: queue.put_nowait(("delta", text)),
                        on_tool=lambda n, a, r: queue.put_nowait(("tool", (n, a, r))),
                    )
                    usage = result.usage
                except GatewayError as e:
                    queue.put_nowait(("gateway_error", str(e)))
                except Exception as e:  # pump must never hang the stream
                    queue.put_nowait(("gateway_error", f"{type(e).__name__}: {e}"))
                finally:
                    queue.put_nowait(("finished", None))

            task = asyncio.create_task(pump())
            while True:
                kind, payload = await queue.get()
                if kind == "delta":
                    parts.append(payload)
                    yield _sse("delta", {"text": payload})
                elif kind == "tool":
                    name, arguments, result = payload
                    parts.append(tool_log_line(name, arguments, result))
                    yield _sse(
                        "tool",
                        {"name": name, "arguments": arguments, "preview": result[:400]},
                    )
                elif kind == "gateway_error":
                    message_id = persist("error")
                    yield _sse("error", {"message": payload, "message_id": message_id})
                    return
                else:  # finished
                    break
            await task
        except GatewayError as e:
            message_id = persist("error")
            yield _sse("error", {"message": str(e), "message_id": message_id})
            return
        except (GeneratorExit, asyncio.CancelledError):
            persist("stopped")
            raise

        message_id = persist(None)
        yield _sse(
            "done",
            {
                "message_id": message_id,
                "session_id": session_id,
                "model": canonical,
                "prompt_tokens": usage.prompt_tokens if usage else None,
                "completion_tokens": usage.completion_tokens if usage else None,
            },
        )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
