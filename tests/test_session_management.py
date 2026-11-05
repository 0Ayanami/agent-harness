"""Conversation lifecycle, compatibility, and per-session memory settings."""

import json
import shutil
import sqlite3
import threading
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from agent_harness import HarnessConfig
from agent_harness.interfaces.web.app import create_app
from test_services import FakeClient, response, text, wait_until


HEADERS = {"X-Agent-Client": "web"}
SESSIONS = "/api/agent/sessions"


@pytest.fixture
def web(tmp_path):
    config = HarnessConfig(tmp_path, "test-model")
    model = FakeClient()
    app = create_app(config, lambda: model)
    with TestClient(app) as browser:
        yield browser, app.state.application, model


def _create(browser, options=None):
    arguments = {"json": options} if options is not None else {}
    result = browser.post(SESSIONS, headers=HEADERS, **arguments)
    assert result.status_code == 201
    return result.json()


def _send(browser, application, model, session_id, message):
    model.replies.append(response(text("finished")))
    result = browser.post(
        f"{SESSIONS}/{session_id}/messages", headers=HEADERS, json={"message": message}
    )
    assert result.status_code == 202
    wait_until(lambda: application.get(session_id).status == "idle")


def test_existing_conversation_keeps_history_and_uses_default_memory_settings(tmp_path):
    config = HarnessConfig(tmp_path, "test-model")
    config.data_dir.mkdir(parents=True)
    session_id = uuid4().hex
    history = [{"role": "user", "content": "existing question"}]
    with sqlite3.connect(config.data_dir / "sessions.sqlite3") as database:
        database.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT NOT NULL, history TEXT NOT NULL, "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        database.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
            (session_id, "existing title", json.dumps(history), "before", "before"),
        )

    path = f"{SESSIONS}/{session_id}"
    with TestClient(create_app(config, FakeClient)) as browser:
        default = browser.get(path).json()
        assert (default["personalized"], default["temporary"], default["archived"]) == (
            True,
            False,
            False,
        )
        assert default["history"] == history
        assert (
            browser.patch(
                path,
                headers=HEADERS,
                json={"personalized": False, "temporary": True, "archived": True},
            ).status_code
            == 200
        )

    with TestClient(create_app(config, FakeClient)) as reopened:
        assert reopened.get(SESSIONS).json() == []
        [archived] = reopened.get(SESSIONS, params={"archived": True}).json()
        assert archived["id"] == session_id
        assert (archived["personalized"], archived["temporary"], archived["archived"]) == (
            False,
            True,
            True,
        )
        assert reopened.get(path).json()["history"] == history
        new = _create(reopened)
        assert (new["personalized"], new["temporary"], new["archived"]) == (True, False, False)


def test_archive_stops_runtime_but_keeps_history_and_restores_durable_jobs(web):
    browser, application, model = web
    session_id = _create(browser)["id"]
    path = f"{SESSIONS}/{session_id}"
    _send(browser, application, model, session_id, "retain this message")
    session = application.get(session_id)
    scheduler = session.runtime.scheduler
    job = scheduler.schedule_job("0 0 31 2 *", "retain durable job")
    assert scheduler.thread.is_alive()
    assert browser.patch(path, headers=HEADERS, json={"archived": True}).json()["archived"]
    assert not scheduler.thread.is_alive()
    assert "retain this message" in browser.get(path).json()["history"][0]["content"]
    assert "retain this message" in browser.get(path + "/events?once=true").text
    assert session_id not in application.sessions
    assert (
        browser.post(
            path + "/messages", headers=HEADERS, json={"message": "cannot send"}
        ).status_code
        == 409
    )
    assert browser.get(SESSIONS).json() == []

    restored = browser.patch(path, headers=HEADERS, json={"archived": False}).json()
    assert restored["archived"] is False
    assert restored["state"]["crons"][0]["id"] == job.id
    assert application.get(session_id) is not session
    assert browser.get(SESSIONS).json()[0]["id"] == session_id
    assert browser.get(SESSIONS, params={"archived": True}).json() == []
    _send(browser, application, model, session_id, "continue after restoring")


def test_deleting_a_conversation_erases_its_history_events_and_local_state_only(web):
    browser, application, model = web
    session_id = _create(browser)["id"]
    other = _create(browser)["id"]
    _send(browser, application, model, session_id, "remove me")
    session = application.get(session_id)
    state = session.runtime.state_dir
    (state / "conversation.txt").write_text("only in this session", encoding="utf-8")
    other_state = application.get(other).runtime.state_dir
    global_memory = application.config.workdir / ".memory" / "preserve.txt"
    global_memory.parent.mkdir(exist_ok=True)
    global_memory.write_text("shared", encoding="utf-8")
    path = f"{SESSIONS}/{session_id}"

    assert browser.patch(path, headers=HEADERS, json={"archived": True}).status_code == 200
    assert browser.delete(path, headers=HEADERS).status_code == 204
    assert browser.get(path).status_code == 404
    assert browser.get(path + "/events?once=true").status_code == 404
    assert browser.get(SESSIONS, params={"archived": True}).json() == []
    application.repository.emit(session_id, "late_callback", {})
    assert application.repository.events(session_id) == []
    assert not state.exists()
    assert other_state.is_dir() and global_memory.read_text(encoding="utf-8") == "shared"


def test_cleanup_failure_is_retried_when_application_reopens(web, monkeypatch):
    browser, application, _ = web
    session_id = _create(browser)["id"]
    state = application.get(session_id).runtime.state_dir
    original = shutil.rmtree

    def busy_once(path, *args, **kwargs):
        if path == state:
            raise PermissionError("busy file")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(shutil, "rmtree", busy_once)
        assert browser.delete(f"{SESSIONS}/{session_id}", headers=HEADERS).status_code == 204
        assert state.exists()
        assert application.repository.pending_cleanup() == [session_id]

    reopened = create_app(application.config, FakeClient)
    with TestClient(reopened) as browser:
        assert browser.get(f"{SESSIONS}/{session_id}").status_code == 404
        assert not state.exists()
        assert reopened.state.application.repository.pending_cleanup() == []


def test_busy_session_rejects_archive_delete_and_memory_changes(web):
    browser, application, model = web
    session_id = _create(browser)["id"]
    path = f"{SESSIONS}/{session_id}"
    entered, release = threading.Event(), threading.Event()

    def block_model(**arguments):
        entered.set()
        release.wait(5)
        return response(text("finished"))

    model.messages.create = block_model
    assert (
        browser.post(path + "/messages", headers=HEADERS, json={"message": "busy"}).status_code
        == 202
    )
    try:
        assert entered.wait(3)
        assert browser.patch(path, headers=HEADERS, json={"archived": True}).status_code == 409
        assert browser.patch(path, headers=HEADERS, json={"personalized": False}).status_code == 409
        assert browser.delete(path, headers=HEADERS).status_code == 409
    finally:
        release.set()
    wait_until(lambda: application.get(session_id).status == "idle")


def test_idle_session_with_active_background_worker_cannot_be_archived_or_deleted(web):
    browser, application, _ = web
    session_id = _create(browser)["id"]
    session = application.get(session_id)
    state = session.runtime.background.background_state
    with state.lock:
        state.tasks["background"] = {"status": "running"}
    path = f"{SESSIONS}/{session_id}"
    assert browser.patch(path, headers=HEADERS, json={"archived": True}).status_code == 409
    assert browser.delete(path, headers=HEADERS).status_code == 409
    assert browser.get(path).json()["archived"] is False


def test_pending_scheduler_prevents_restore_and_delays_physical_deletion(web):
    browser, application, _ = web
    session_id = _create(browser)["id"]
    runtime = application.get(session_id).runtime
    actual_scheduler = runtime.scheduler.thread
    release = threading.Event()
    scheduler = threading.Thread(target=release.wait, daemon=True)
    scheduler.start()
    runtime.scheduler.thread = scheduler
    path = f"{SESSIONS}/{session_id}"
    try:
        assert browser.patch(path, headers=HEADERS, json={"archived": True}).status_code == 200
        assert browser.patch(path, headers=HEADERS, json={"archived": False}).status_code == 409
        assert browser.delete(path, headers=HEADERS).status_code == 204
        assert runtime.state_dir.is_dir()
    finally:
        release.set()
        scheduler.join(timeout=2)
        actual_scheduler.join(timeout=2)
    wait_until(lambda: not runtime.state_dir.exists())
    assert browser.get(path).status_code == 404


def test_reenabling_memory_does_not_backfill_private_turns(web, monkeypatch):
    browser, application, model = web
    session_id = _create(browser, {"personalized": True, "temporary": True})["id"]
    path = f"{SESSIONS}/{session_id}"
    memory = application.get(session_id).runtime.memory
    extracted = []
    monkeypatch.setattr(memory, "extract_memories", lambda messages: extracted.extend(messages))

    _send(browser, application, model, session_id, "temporary turn")
    assert (
        browser.patch(
            path, headers=HEADERS, json={"personalized": False, "temporary": False}
        ).status_code
        == 200
    )
    _send(browser, application, model, session_id, "personalization disabled")
    assert browser.patch(path, headers=HEADERS, json={"personalized": True}).status_code == 200
    _send(browser, application, model, session_id, "eligible turn")
    assert extracted == [{"role": "user", "content": "eligible turn"}]
    snapshot = browser.get(path).json()
    assert [message["content"] for message in snapshot["history"] if message["role"] == "user"] == [
        "temporary turn",
        "personalization disabled",
        "eligible turn",
    ]


def test_management_mutations_validate_flags_and_same_origin(web):
    browser, _, _ = web
    session_id = _create(browser)["id"]
    path = f"{SESSIONS}/{session_id}"
    for changes in ({}, {"temporary": None}, {"personalized": "false"}, {"unknown": True}):
        assert browser.patch(path, headers=HEADERS, json=changes).status_code == 422
    assert browser.post(SESSIONS, headers=HEADERS, json={"temporary": 1}).status_code == 422
    assert browser.patch(path, json={"archived": True}).status_code == 403
    assert (
        browser.delete(path, headers={**HEADERS, "Origin": "https://other.invalid"}).status_code
        == 403
    )
