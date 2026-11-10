"""A slow SQLite read must not stop unrelated work on the ASGI event loop."""

import asyncio
import threading

import httpx

from agent_harness import HarnessConfig
from agent_harness.interfaces.web.app import create_app
from agent_harness.infrastructure.sessions import SessionRepository


def test_replay_boundary_excludes_later_events_and_reports_deletion(tmp_path):
    repository = SessionRepository(tmp_path / "sessions.sqlite3")
    sid = repository.create()["id"]
    for index in range(205):
        repository.emit(sid, "test", {"index": index})
    first = repository.event_page(sid, snapshot=True)
    assert first["exists"] and len(first["events"]) == 200
    repository.emit(sid, "test", {"index": "later"})
    last = repository.event_page(sid, first["events"][-1]["id"], through=first["through"])
    assert [event["payload"]["index"] for event in last["events"]] == list(range(200, 205))
    assert last["events"][-1]["id"] == first["through"]
    repository.delete(sid)
    missing = repository.event_page(sid, snapshot=True)
    assert not missing["exists"] and missing["events"] == []


def test_sse_database_wait_does_not_block_async_routes(tmp_path, monkeypatch):
    app = create_app(HarnessConfig(tmp_path, "test-model"), lambda: None)
    repository = app.state.application.repository
    session = repository.create()
    entered, release = threading.Event(), threading.Event()
    original_events = repository.event_page

    def blocked_events(*args, **kwargs):
        entered.set()
        assert release.wait(3), "The event loop could not release the database reader"
        return original_events(*args, **kwargs)

    monkeypatch.setattr(repository, "event_page", blocked_events)

    @app.get("/async-probe")
    async def probe():
        return {"responsive": True}

    async def exercise():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            replay = asyncio.create_task(client.get(
                f"/api/agent/sessions/{session['id']}/events?once=true"
            ))
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                response = await asyncio.wait_for(client.get("/async-probe"), 1)
                assert response.json() == {"responsive": True}
            finally:
                release.set()
                await replay

    try:
        asyncio.run(exercise())
    finally:
        app.state.application.close()
        app.state.retrieval.close()
        app.state.ingestion.close()
        app.state.library.close()
