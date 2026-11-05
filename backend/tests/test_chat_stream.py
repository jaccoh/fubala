"""SSE message streaming: shape, persistence, error and 404 paths."""

from conftest import FakeGateway, parse_sse

from app.providers import GatewayError


def test_send_message_streams_and_persists(client, gateway):
    sid = client.post("/api/v1/chat/sessions", json={}).json()["id"]

    res = client.post(f"/api/v1/chat/sessions/{sid}/messages", json={"content": "hi"})
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/event-stream")

    events = parse_sse(res.text)
    kinds = [e for e, _ in events]
    assert kinds[0] == "start"
    assert kinds.count("delta") == 3
    assert kinds[-1] == "done"

    start = events[0][1]
    done = events[-1][1]
    assert start["model"] == "echo/dev"
    assert "list_stories" in start["tools"] and "get_story" in start["tools"]
    assert done["model"] == "echo/dev"
    assert done["session_id"] == sid
    assert isinstance(done["message_id"], int)
    assert done["prompt_tokens"] == 3 and done["completion_tokens"] == 5

    # both messages persisted
    detail = client.get(f"/api/v1/chat/sessions/{sid}").json()
    roles = [m["role"] for m in detail["messages"]]
    assert roles == ["user", "assistant"]
    assistant = detail["messages"][1]
    assert assistant["content"] == "Hello there!"
    assert assistant["model"] == "echo/dev"
    assert assistant["finish_reason"] is None
    assert assistant["prompt_tokens"] == 3

    # the gateway saw full history including the new user message
    assert gateway.calls[-1]["messages"][-1] == {"role": "user", "content": "hi"}


def test_per_message_model_override(client, gateway):
    sid = client.post("/api/v1/chat/sessions", json={}).json()["id"]
    res = client.post(
        f"/api/v1/chat/sessions/{sid}/messages",
        json={"content": "hi", "model": "zai/glm-4.7"},
    )
    assert res.status_code == 200
    assert events_model(res.text) == "zai/glm-4.7"
    assert gateway.calls[-1]["model"] == "zai/glm-4.7"

    # override sticks as the new session model (matches chat-app UX)
    session = client.get(f"/api/v1/chat/sessions/{sid}").json()
    assert session["model"] == "zai/glm-4.7"


def events_model(sse_text: str) -> str:
    events = parse_sse(sse_text)
    return events[0][1]["model"]


def test_send_message_unknown_session_404(client):
    res = client.post("/api/v1/chat/sessions/missing/messages", json={"content": "hi"})
    assert res.status_code == 404


def test_gateway_error_emits_error_event_and_persists_partial(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import create_app

    class ErrorGateway(FakeGateway):
        async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
            from app.providers import Delta

            yield Delta(text="partial ")
            raise GatewayError("[echo] boom")

    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "err.db"))
    app = create_app(gateway=ErrorGateway())
    with TestClient(app) as c:
        sid = c.post("/api/v1/chat/sessions", json={}).json()["id"]
        res = c.post(f"/api/v1/chat/sessions/{sid}/messages", json={"content": "hi"})
        events = parse_sse(res.text)
        kinds = [e for e, _ in events]
        assert "error" in kinds and kinds[-1] == "error"
        assert "[echo] boom" in events[-1][1]["message"]

        detail = c.get(f"/api/v1/chat/sessions/{sid}").json()
        assistant = detail["messages"][-1]
        assert assistant["role"] == "assistant"
        assert assistant["content"] == "partial "
        assert assistant["finish_reason"] == "error"
