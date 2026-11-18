import os
from datetime import datetime, timedelta, timezone
from threading import Event

import pytest
from fastapi.testclient import TestClient

from agent_harness.config import HarnessConfig
from agent_harness.infrastructure.cron_client import CronClient
from agent_harness.infrastructure.cron_store import CronStore
from agent_harness.infrastructure.sessions import SessionRepository
from agent_harness.interfaces.web.app import create_app
from agent_harness.services.scheduler import Scheduler
from test_services import FakeClient, response, text, wait_until


def test_atomic_due_delivery_survives_reopen_and_rejects_stale_ack(tmp_path, monkeypatch):
    path = tmp_path / "cron.db"
    scope, sid = "a" * 32, "b" * 32
    shanghai_offset = timezone(timedelta(hours=8))
    now = datetime(2026, 11, 17, 8, 15, tzinfo=shanghai_offset)
    monkeypatch.setattr("agent_harness.infrastructure.cron_store.ZoneInfo", lambda _: shanghai_offset)
    monkeypatch.setattr(CronStore, "now", lambda self: now)
    first, second = CronStore(path, "Asia/Shanghai"), CronStore(path, "Asia/Shanghai")
    first.rpc(scope, "create", sid, cron="15 8 * * *", prompt="work", recurring=False, durable=True)
    assert first.tick() == 1 and second.tick() == 0
    claimed = first.rpc(scope, "claim", sid)
    assert claimed["scheduled_at"] == int(now.timestamp())
    assert second.rpc(scope, "claim", sid) is None
    now += timedelta(minutes=6)
    recovered = CronStore(path, "Asia/Shanghai").rpc(scope, "claim", sid)
    assert recovered["scheduled_at"] == claimed["scheduled_at"]
    assert recovered["token"] != claimed["token"]
    first.rpc(scope, "ack", sid, token=claimed["token"])
    assert first.rpc(scope, "list", sid)[0]["status"] == "queued"
    second.rpc(scope, "ack", sid, token=recovered["token"])
    assert first.rpc(scope, "list", sid) == []


def test_container_detects_without_backend_and_dispatches_idle_session(tmp_path):
    url = os.getenv("HARNESS_CRON_TEST_URL")
    if not url:
        pytest.skip("HARNESS_CRON_TEST_URL is unset")
    config = HarnessConfig(tmp_path, "test", cron_service_url=url)
    repository = SessionRepository(config.data_dir / "sessions.sqlite3")
    sid = repository.create()["id"]
    archived = repository.create()["id"]
    repository.update(archived, archived=True)
    cron = CronClient(url, config.data_dir)
    try:
        cron.call(
            "create",
            sid,
            cron="* * * * *",
            prompt="Independent detector",
            recurring=False,
            durable=True,
        )
        wait_until(lambda: cron.call("due"), timeout=8)
        legacy_dir = config.data_dir / "sessions" / archived
        legacy_dir.mkdir(parents=True)
        Scheduler(legacy_dir, Event()).schedule_job(
            "* * * * *", "Paused legacy schedule", recurring=False
        )
        model = FakeClient([response(text("Detected task ran"))])
        app = create_app(config, lambda: model)
        with TestClient(app) as browser:
            wait_until(
                lambda: any(event["kind"] == "run_finished" for event in repository.events(sid)),
                timeout=12,
            )
            assert [
                e["payload"]["prompt"] for e in repository.events(sid) if e["kind"] == "scheduled"
            ] == ["Independent detector"]
            assert not repository.events(archived)
            assert cron.call("list", archived)[0]["prompt"] == "Paused legacy schedule"
            assert cron.call("due") == [] and cron.call("list", sid) == []
            assert len(model.calls) == 1
            session = app.state.application.get(sid)
            assert not hasattr(session.runtime.scheduler, "thread")
            assert (
                browser.get("/api/agent/scheduled-tasks").json()["server_time"].endswith("+08:00")
            )
    finally:
        for identifier in (sid, archived):
            cron.call("purge", identifier)
        cron.close()
