"""HTTP management reuses the conversation Scheduler and its original delivery path."""

import json
import threading
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from agent_harness import HarnessConfig
from agent_harness.application.sessions import SessionService
from agent_harness.interfaces.web.app import create_app
from agent_harness.services.scheduler import Scheduler
from test_services import FakeClient, response, text, wait_until


HEADERS = {"X-Agent-Client": "web"}
SESSIONS = "/api/agent/sessions"
TASKS = "/api/agent/scheduled-tasks"
FUTURE = "0 0 31 2 *"


def _session(browser):
    created = browser.post(SESSIONS, headers=HEADERS)
    assert created.status_code == 201
    return created.json()["id"]


def _tasks(session_id):
    return f"{SESSIONS}/{session_id}/scheduled-tasks"


def _schedule(browser, session_id, cron=FUTURE, prompt="Check project status", **options):
    return browser.post(
        _tasks(session_id),
        headers=HEADERS,
        json={"cron_expr": cron, "prompt": prompt, **options},
    )


def test_schedule_management_includes_tool_jobs_and_respects_session_lifecycle(tmp_path):
    config = HarnessConfig(tmp_path, "test")
    app = create_app(config, FakeClient)
    with TestClient(app) as browser:
        session_id = _session(browser)
        other = _session(browser)
        assert browser.get(TASKS).json()["items"] == []

        payload = {"cron_expr": "  15   8 * * 1  ", "prompt": " Check project status "}
        assert browser.post(_tasks(session_id), json=payload).status_code == 403
        for cron in ("* * * *", "² * * * *", "0 0 1 1 8"):
            assert _schedule(browser, session_id, cron=cron).status_code == 422
        assert _schedule(browser, session_id, prompt=" ").status_code == 422
        task = browser.post(_tasks(session_id), headers=HEADERS, json=payload)
        assert task.status_code == 201
        job = task.json()
        assert job["cron"] == "15 8 * * 1"
        assert job["prompt"] == "Check project status"
        assert job["session_id"] == session_id and job["session_title"]
        assert job["durable"] and job["recurring"] and job["status"] == "scheduled"
        next_run = datetime.fromisoformat(job["next_run_at"])
        assert (next_run.weekday(), next_run.hour, next_run.minute) == (0, 8, 15)
        assert next_run.utcoffset() is not None

        scheduler = app.state.application.get(other).runtime.scheduler
        scheduler.run_schedule_cron(FUTURE, "Created by the Agent", durable=False)
        assert [item["id"] for item in browser.get(_tasks(session_id)).json()["items"]] == [
            job["id"]
        ]
        listing = browser.get(TASKS).json()
        assert datetime.fromisoformat(listing["server_time"]).utcoffset() is not None
        assert {item["prompt"] for item in listing["items"]} == {
            "Check project status",
            "Created by the Agent",
        }
        assert (
            browser.patch(
                f"{SESSIONS}/{session_id}", headers=HEADERS, json={"archived": True}
            ).status_code
            == 200
        )
        assert browser.get(_tasks(session_id)).status_code == 409
        assert _schedule(browser, session_id).status_code == 409
        assert {item["session_id"] for item in browser.get(TASKS).json()["items"]} == {other}
        assert (
            browser.patch(
                f"{SESSIONS}/{session_id}", headers=HEADERS, json={"archived": False}
            ).status_code
            == 200
        )
        path = f"{_tasks(session_id)}/{job['id']}"
        assert browser.delete(path).status_code == 403
        assert browser.delete(path, headers=HEADERS).status_code == 204
        assert browser.delete(path, headers=HEADERS).status_code == 404
        assert browser.get(_tasks(session_id)).json()["items"] == []
        assert browser.delete(f"{SESSIONS}/{session_id}", headers=HEADERS).status_code == 204
        assert browser.get(_tasks(session_id)).status_code == 404


def test_web_restores_and_fires_durable_job_without_opening_its_conversation(tmp_path):
    config = HarnessConfig(tmp_path, "test")
    unopened = SessionService(config, FakeClient)
    active = unopened.repository.create()["id"]
    archived = unopened.repository.create()["id"]
    unopened.repository.update(archived, archived=True)
    for session_id, prompt in ((active, "Run the restored task"), (archived, "Do not run")):
        state_dir = config.data_dir / "sessions" / session_id
        state_dir.mkdir(parents=True)
        scheduler = Scheduler(state_dir, threading.Event())
        scheduler.schedule_job("* * * * *", prompt, recurring=False)
        removed = scheduler.schedule_job("* * * * *", "Removed before due", recurring=False)
        scheduler.cancel_job(removed.id)

    # CLI keeps opening only its own conversation.
    unopened.start()
    assert unopened.sessions == {}
    unopened.close()

    model = FakeClient([response(text("Restored scheduled answer"))])
    app = create_app(config, lambda: model)
    with TestClient(app) as browser:
        application = app.state.application
        wait_until(
            lambda: any(
                event["kind"] == "run_finished" for event in application.repository.events(active)
            )
        )
        replay = browser.get(f"{SESSIONS}/{active}/events?once=true").text
        assert "Run the restored task" in replay and "Restored scheduled answer" in replay
        assert "Removed before due" not in replay
        assert application.repository.events(archived) == []
        assert archived not in application.sessions
        assert len(model.calls) == 1
        assert browser.get(TASKS).json()["items"] == []


def test_new_occurrence_wakes_a_stopped_conversation(tmp_path):
    model = FakeClient([response(text("Later schedule ran"))])
    app = create_app(HarnessConfig(tmp_path, "test"), lambda: model)
    with TestClient(app) as browser:
        session_id = _session(browser)
        assert browser.post(f"{SESSIONS}/{session_id}/cancel", headers=HEADERS).status_code == 200
        assert _schedule(browser, session_id, cron="* * * * *", recurring=False).status_code == 201
        application = app.state.application
        wait_until(
            lambda: any(
                event["kind"] == "run_finished"
                for event in application.repository.events(session_id)
            )
        )
        assert "Later schedule ran" in browser.get(f"{SESSIONS}/{session_id}/events?once=true").text


def test_failed_schedule_persistence_does_not_leave_a_ghost_task(tmp_path, monkeypatch):
    scheduler = Scheduler(tmp_path, threading.Event())
    job = scheduler.schedule_job(FUTURE, "Existing task")

    def fail():
        raise OSError("disk unavailable")

    monkeypatch.setattr(scheduler, "save_durable_jobs", fail)
    with pytest.raises(OSError):
        scheduler.schedule_job(FUTURE, "Failed task")
    with pytest.raises(OSError):
        scheduler.remove_job(job.id)
    assert scheduler.list_jobs() == json.loads(scheduler.DURABLE_PATH.read_text(encoding="utf-8"))
