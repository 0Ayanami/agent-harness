"""Real HTTP routes, SSE, workers and approvals; only the model is replaced."""

import threading

import pytest
from fastapi.testclient import TestClient

from agent_harness import HarnessConfig
from agent_harness.interfaces.web.app import create_app
from test_services import FakeClient, response, text, tool, wait_until

HEADERS = {"X-Agent-Client": "web"}


@pytest.fixture
def web(tmp_path):
    client = FakeClient()
    config = HarnessConfig(tmp_path, "test-model")
    app = create_app(config, lambda: client)
    with TestClient(app) as browser:
        session = browser.post("/api/sessions", headers=HEADERS).json()
        yield browser, app.state.application, client, session["id"]


def post_message(web, value):
    browser, application, _, session_id = web
    result = browser.post(
        f"/api/sessions/{session_id}/messages", headers=HEADERS, json={"message": value}
    )
    assert result.status_code == 202, result.text
    wait_until(lambda: application.get(session_id).status == "idle")
    return browser.get(f"/api/sessions/{session_id}").json()


def test_api_metadata_and_health(web):
    browser, _, _, _ = web
    assert browser.get("/").json()["name"] == "Agent Harness API"
    assert browser.get("/assets/app.js").status_code == 404
    assert browser.get("/api/health").json()["model"] == "test-model"


def test_multi_turn_chat_and_sse_cursor(web):
    browser, application, client, session_id = web
    client.replies = [response(text("first answer")), response(text("second answer"))]
    first = post_message(web, "first")
    second = post_message(web, "second")
    assert len(second["history"]) == 4
    assert second["history"][-1]["content"][0]["text"] == "second answer"
    stream = browser.get(f"/api/sessions/{session_id}/events?after={first['cursor']}&once=true")
    assert "text/event-stream" in stream.headers["content-type"]
    assert "second answer" in stream.text and "first answer" not in stream.text
    assert not application.get(session_id).approvals.snapshot()


def test_replay_includes_every_page_of_events(web):
    browser, application, _, session_id = web
    for index in range(225):
        application.repository.emit(session_id, "test", {"index": index})
    replay = browser.get(f"/api/agent/sessions/{session_id}/events?once=true")
    assert len([line for line in replay.text.splitlines() if line.startswith("data:")]) == 225
    assert '"index": 224' in replay.text


def test_frontend_alias_uses_same_session_and_permissions(web):
    browser, application, _, session_id = web
    assert browser.get(f"/api/agent/sessions/{session_id}").json()["id"] == session_id
    assert browser.post("/api/agent/sessions").status_code == 403
    assert (
        browser.post(
            "/api/agent/sessions",
            headers={
                **HEADERS,
                "Origin": "https://unrelated.invalid",
                "X-Forwarded-Host": "unrelated.invalid",
            },
        ).status_code
        == 403
    )
    assert browser.get("/api/agent/missing-route").status_code == 404


def test_development_proxy_only_allows_configured_frontend_origin(tmp_path):
    app = create_app(
        HarnessConfig(tmp_path, "test-model"),
        FakeClient,
        frontend_origins=("http://127.0.0.1:3000",),
    )
    with TestClient(app) as client:
        assert (
            client.post(
                "/api/sessions", headers={**HEADERS, "Origin": "http://127.0.0.1:3000"}
            ).status_code
            == 201
        )
        assert (
            client.post(
                "/api/sessions", headers={**HEADERS, "Origin": "http://127.0.0.1:3001"}
            ).status_code
            == 403
        )
        assert (
            client.post("/api/sessions", headers={"Origin": "http://127.0.0.1:3000"}).status_code
            == 403
        )


def test_export_is_served_with_same_origin_api_and_safe_paths(tmp_path):
    output = tmp_path / "out"
    (output / "zh").mkdir(parents=True)
    (output / "_next" / "static").mkdir(parents=True)
    (output / "index.html").write_text("Migrated root", encoding="utf-8")
    (output / "zh" / "index.html").write_text("Learn Claude Code", encoding="utf-8")
    (output / "_next" / "static" / "app.js").write_text("console.log('export');", encoding="utf-8")
    (tmp_path / "private.txt").write_text("private content", encoding="utf-8")
    app = create_app(HarnessConfig(tmp_path, "test-model"), FakeClient, frontend_directory=output)
    with TestClient(app) as client:
        assert client.get("/").text == "Migrated root"
        assert client.get("/zh/").text == "Learn Claude Code"
        assert client.get("/_next/static/app.js").status_code == 200
        assert client.get("/api/agent/health").json()["status"] == "ok"
        assert (
            client.post(
                "/api/agent/sessions", headers={**HEADERS, "Origin": "http://testserver"}
            ).status_code
            == 201
        )
        assert client.get("/api/agent/not-found").status_code == 404
        assert client.get("/%2e%2e/private.txt").status_code == 404
        assert (
            client.post("/api/agent/sessions", headers={**HEADERS, "Origin": "null"}).status_code
            == 403
        )
        assert (
            client.get("/api/agent/health", headers={"Host": "unrelated.invalid"}).status_code
            == 400
        )


def test_http_tool_execution_writes_real_file(web):
    _, application, client, session_id = web
    client.replies = [
        response(
            tool("write_file", {"path": "http.txt", "content": "written through HTTP"}),
            stop_reason="tool_use",
        ),
        response(text("done")),
    ]
    snapshot = post_message(web, "write a file")
    assert (application.config.workdir / "http.txt").read_text(
        encoding="utf-8"
    ) == "written through HTTP"
    assert snapshot["history"][2]["content"][0]["tool_use_id"] == "tool-1"
    assert any(
        event["kind"] == "tool_finished" for event in application.repository.events(session_id)
    )


@pytest.mark.parametrize("allow", [True, False])
def test_web_permission_approval_and_denial(web, allow):
    browser, application, client, session_id = web
    client.replies = [
        response(tool("bash", {"command": "echo approval-ok"}), stop_reason="tool_use"),
        response(text("finished")),
    ]
    browser.post(
        f"/api/sessions/{session_id}/messages",
        headers=HEADERS,
        json={"message": "run command"},
    )
    session = application.get(session_id)
    wait_until(lambda: bool(session.approvals.snapshot()))
    approval = browser.get(f"/api/sessions/{session_id}").json()["approvals"][0]
    assert (
        browser.post(
            f"/api/sessions/{session_id}/approvals/{approval['id']}",
            headers=HEADERS,
            json={"allow": allow},
        ).status_code
        == 200
    )
    wait_until(lambda: session.status == "idle")
    result = session.record["history"][2]["content"][0]["content"]
    assert ("approval-ok" in result) if allow else ("Permission denied" in result)


def test_busy_session_rejects_overlapping_turns(web):
    browser, application, client, session_id = web
    entered, release = threading.Event(), threading.Event()

    def blocking(**arguments):
        if arguments["max_tokens"] == 1000:
            return response(text("[]"))
        entered.set()
        release.wait(5)
        return response(text("done"))

    client.messages.create = blocking
    browser.post(
        f"/api/sessions/{session_id}/messages",
        headers=HEADERS,
        json={"message": "first"},
    )
    assert entered.wait(3)
    try:
        assert (
            browser.post(
                f"/api/sessions/{session_id}/messages",
                headers=HEADERS,
                json={"message": "overlap"},
            ).status_code
            == 409
        )
    finally:
        release.set()
    wait_until(lambda: application.get(session_id).status == "idle")


def test_cancel_pending_approval_then_continue(web):
    browser, application, client, session_id = web
    client.replies = [
        response(tool("bash", {"command": "echo should-not-run"}), stop_reason="tool_use")
    ]
    browser.post(
        f"/api/sessions/{session_id}/messages",
        headers=HEADERS,
        json={"message": "command"},
    )
    session = application.get(session_id)
    wait_until(lambda: bool(session.approvals.snapshot()))
    assert browser.post(f"/api/sessions/{session_id}/cancel", headers=HEADERS).status_code == 200
    wait_until(lambda: session.status == "idle")
    assert not session.approvals.snapshot()
    client.replies = [response(text("resumed"))]
    assert post_message(web, "continue")["history"][-1]["content"][0]["text"] == "resumed"


def test_sessions_isolate_histories_tasks_and_approvals(web):
    browser, application, _, session_id = web
    second_id = browser.post("/api/sessions", headers=HEADERS).json()["id"]
    first, second = application.get(session_id), application.get(second_id)
    first.runtime.tasks.create_task("first task")
    assert not second.runtime.tasks.list_tasks()
    assert first.runtime.tasks.repository.directory != second.runtime.tasks.repository.directory
    assert first.approvals is not second.approvals
    assert first.runtime.memory.lock is second.runtime.memory.lock


def test_restart_restores_history_and_event_log(web):
    browser, application, client, session_id = web
    client.replies = [response(text("persisted"))]
    post_message(web, "remember conversation")
    second_app = create_app(application.config, FakeClient)
    with TestClient(second_app) as reopened:
        record = reopened.get(f"/api/sessions/{session_id}").json()
        assert record["history"][-1]["content"][0]["text"] == "persisted"
        assert record["status"] == "idle"
        assert "persisted" in reopened.get(f"/api/sessions/{session_id}/events?once=true").text


def test_invalid_inputs_and_origin_are_rejected(web):
    browser, _, _, session_id = web
    assert browser.post("/api/sessions").status_code == 403
    assert (
        browser.post(
            "/api/sessions", headers={**HEADERS, "Origin": "https://unrelated.invalid"}
        ).status_code
        == 403
    )
    assert (
        browser.post(
            f"/api/sessions/{session_id}/messages",
            headers=HEADERS,
            json={"message": " "},
        ).status_code
        == 422
    )
    assert browser.get("/api/sessions/missing").status_code == 404
    assert (
        browser.post(
            f"/api/sessions/{session_id}/approvals/missing",
            headers=HEADERS,
            json={"allow": True},
        ).status_code
        == 404
    )


def test_model_error_is_visible_and_session_recovers(web):
    browser, application, client, session_id = web
    client.replies = [RuntimeError("provider unavailable")]
    post_message(web, "first")
    events = application.repository.events(session_id)
    assert any(
        event["kind"] == "error" and "provider unavailable" in event["payload"]["message"]
        for event in events
    )
    assert browser.get(f"/api/sessions/{session_id}").json()["status"] == "idle"


def test_background_completion_wakes_idle_session(web):
    browser, application, client, session_id = web
    session = application.get(session_id)
    release = threading.Event()

    def delayed_shell(command, cwd=None):
        release.wait(5)
        return "background finished", 0

    session.runtime.workspace._run_bash_process = delayed_shell
    client.replies = [
        response(
            tool("bash", {"command": "echo test", "run_in_background": True}),
            stop_reason="tool_use",
        ),
        response(text("background started")),
        response(text("background complete")),
    ]
    browser.post(
        f"/api/sessions/{session_id}/messages", headers=HEADERS, json={"message": "background"}
    )
    wait_until(lambda: bool(session.approvals.snapshot()))
    approval = session.approvals.snapshot()[0]
    browser.post(
        f"/api/sessions/{session_id}/approvals/{approval['id']}",
        headers=HEADERS,
        json={"allow": True},
    )
    wait_until(lambda: session.status == "idle")
    release.set()
    wait_until(
        lambda: any(
            event["kind"] == "assistant" and event["payload"]["text"] == "background complete"
            for event in application.repository.events(session_id)
        )
    )
    wait_until(lambda: session.status == "idle")
    assert any(
        event["kind"] == "background_completed"
        for event in application.repository.events(session_id)
    )
