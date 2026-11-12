"""Redis hot records are optional; SQL versions and durable events stay authoritative."""

import asyncio
import json
import os
import sqlite3
import threading
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from agent_harness import HarnessConfig
from agent_harness.infrastructure.session_cache import RedisSessionCache
from agent_harness.infrastructure.sessions import SessionRepository
from agent_harness.interfaces.web.app import create_app
from test_services import FakeClient, wait_until


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.expirations = {}
        self.messages = []
        self.failure = False

    def check(self):
        if self.failure:
            raise ConnectionError("redis://secret-password@private-host")

    def get(self, key):
        self.check()
        return self.values.get(key)

    def set(self, key, value, *, ex):
        self.check()
        self.values[key] = value
        self.expirations[key] = ex

    def delete(self, key):
        self.check()
        return self.values.pop(key, None)

    def publish(self, channel, message):
        self.check()
        self.messages.append((channel, message))

    def close(self):
        pass


def components(tmp_path, client=None):
    client = client or FakeRedis()
    path = tmp_path / "sessions.sqlite3"
    cache = RedisSessionCache("redis://unused", path, prefix="test_ayanami", ttl=45, client=client)
    return SessionRepository(path, cache=cache), cache, client


def test_revision_cache_cannot_restore_deleted_archived_or_private_records(tmp_path):
    repository, cache, redis = components(tmp_path)
    original = repository.create()
    sid = original["id"]
    old_key = cache._key(sid, 0)
    old_payload = redis.values[old_key]
    assert redis.expirations[old_key] == 45
    assert "revision" not in original
    repository.save(sid, "Current title", [{"role": "user", "content": "current history"}])
    redis.values[old_key] = old_payload  # A late reader fills a retired revision.
    assert repository.get(sid)["title"] == "Current title"
    options = repository.update(sid, personalized=False, temporary=True, archived=True)
    assert options == {"personalized": False, "temporary": True, "archived": True}
    current = repository.get(sid)
    assert not current["personalized"] and current["temporary"] and current["archived"]
    assert current["history"][0]["content"] == "current history"
    repository.delete(sid)
    redis.values[old_key] = old_payload
    with pytest.raises(KeyError):
        repository.get(sid)
    assert repository.events(sid) == []
    assert all(message == sid for _, message in redis.messages), "Wakeups contain IDs only"
    repository.close()


def test_failed_invalidation_and_recovery_never_serve_old_history(tmp_path, caplog):
    repository, cache, redis = components(tmp_path)
    sid = repository.create()["id"]
    old_key = cache._key(sid, 0)
    redis.failure = True
    repository.save(sid, "committed", [{"role": "user", "content": "durable"}])
    assert old_key in redis.values  # DEL failed, so the old value really survived.
    assert repository.get(sid)["title"] == "committed"
    assert not cache.status()["cache_available"]
    assert "secret-password" not in caplog.text and "private-host" not in caplog.text
    redis.failure = False
    cache._disabled_until = 0
    assert repository.get(sid)["history"][0]["content"] == "durable"
    assert cache.status()["cache_available"]
    repository.close()


def test_corrupt_cached_history_flags_and_restore_timestamp_are_cache_misses(tmp_path):
    repository, cache, redis = components(tmp_path)
    sid = repository.create()["id"]
    key = cache._key(sid, 0)
    original = json.loads(redis.values[key])
    for change in (
        {"history": [123]},
        {"history": [{"role": "assistant", "content": [123]}]},
        {"personalized": False},
        {"updated_at": "future-backup-revision-reuse", "title": "wrong restored title"},
    ):
        payload = json.loads(json.dumps(original))
        payload["record"].update(change)
        redis.values[key] = json.dumps(payload)
        assert repository.get(sid) == original["record"]
    # Returned history objects are independent of the immutable stored payload.
    cached = repository.get(sid)
    cached["history"].append({"role": "user", "content": "client mutation"})
    assert repository.get(sid)["history"] == []
    repository.close()


def test_rolled_back_sql_write_never_sends_a_notification(tmp_path):
    repository, _, redis = components(tmp_path)
    sid = repository.create()["id"]
    with repository.connection() as database:
        database.execute(
            "CREATE TRIGGER fail_session_update BEFORE UPDATE ON sessions "
            "BEGIN SELECT RAISE(ABORT,'synthetic failed commit'); END"
        )
    before = list(redis.messages)
    with pytest.raises(sqlite3.IntegrityError):
        repository.update(sid, personalized=False)
    assert redis.messages == before
    assert repository.get(sid)["personalized"]
    repository.close()


def test_legacy_session_schema_adds_private_revision_without_changing_history(tmp_path):
    path = tmp_path / "sessions.sqlite3"
    with sqlite3.connect(path) as database:
        database.execute(
            "CREATE TABLE sessions(id TEXT PRIMARY KEY,title TEXT,history TEXT,"
            "created_at TEXT,updated_at TEXT)"
        )
        database.execute("INSERT INTO sessions VALUES (?,?,?,?,?)", (
            "legacy", "legacy title", '[{"role":"user","content":"kept"}]', "old", "old",
        ))
    repository = SessionRepository(path)
    record = repository.get("legacy")
    assert record["history"][0]["content"] == "kept" and record["personalized"]
    assert not record["temporary"] and not record["archived"] and "revision" not in record
    repository.update("legacy", temporary=True)
    assert repository.get("legacy")["temporary"]


def test_async_wakeups_share_one_notifier_and_close_the_read_wait_race(tmp_path):
    repository, cache, _ = components(tmp_path)
    sid = repository.create()["id"]
    cache._set_ready(True)

    async def exercise():
        version = repository.notification_version(sid)
        repository.emit(sid, "first", {})
        # It was published between the durable read and waiter registration.
        await asyncio.wait_for(repository.wait_for_events(sid, version), 0.2)
        version = repository.notification_version(sid)
        tasks = [asyncio.create_task(repository.wait_for_events(sid, version)) for _ in range(40)]
        await asyncio.sleep(0)
        marker = []
        asyncio.get_running_loop().call_soon(marker.append, "event loop remains available")
        thread = threading.Thread(target=lambda: repository.emit(sid, "second", {}))
        thread.start()
        await asyncio.wait_for(asyncio.gather(*tasks), 1)
        thread.join(timeout=1)
        assert marker and not cache._waiters
        assert [event["kind"] for event in repository.events(sid)] == ["first", "second"]

    asyncio.run(exercise())
    repository.close()


def test_lost_pubsub_hint_and_waiter_cancellation_keep_durable_replay(tmp_path):
    repository, cache, _ = components(tmp_path)
    sid = repository.create()["id"]
    cache._set_ready(True)
    with repository.connection() as database:
        database.execute("INSERT INTO events(session_id,kind,payload,created_at) VALUES (?,?,?,?)", (
            sid, "no-pubsub", "{}", "synthetic",
        ))

    async def exercise():
        version = repository.notification_version(sid)
        await repository.wait_for_events(sid, version, timeout=0.01)
        assert repository.event_page(sid)["events"][0]["kind"] == "no-pubsub"
        task = asyncio.create_task(repository.wait_for_events(sid, version))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not cache._waiters

    asyncio.run(exercise())
    repository.close()


@pytest.fixture
def real_redis(tmp_path):
    url = os.getenv("HARNESS_REDIS_TEST_URL")
    if not url:
        pytest.skip("Set HARNESS_REDIS_TEST_URL for isolated real Redis integration")
    redis = pytest.importorskip("redis")
    prefix = "test_ayanami_" + uuid4().hex
    client = redis.Redis.from_url(url, protocol=2, decode_responses=True)
    assert client.ping()
    yield url, prefix, client
    # No scan, KEYS, FLUSHDB or wildcard deletion. Each test records its exact keys.
    client.close()


def test_real_redis_hot_record_ttl_and_sqlite_reopen(tmp_path, real_redis):
    url, prefix, admin = real_redis
    path = tmp_path / "sessions.sqlite3"
    cache = RedisSessionCache(url, path, prefix=prefix, ttl=30)
    repository = SessionRepository(path, cache=cache)
    keys = set()
    try:
        sid = repository.create()["id"]
        keys.add(cache._key(sid, 0))
        repository.save(sid, "persisted", [{"role": "user", "content": "Redis is a cache"}])
        assert repository.get(sid)["title"] == "persisted"
        keys.add(cache._key(sid, 1))
        assert 0 < admin.ttl(cache._key(sid, 1)) <= 30
        assert json.loads(admin.get(cache._key(sid, 1)))["record"]["history"][0]["content"] == "Redis is a cache"
        repository.close()
        repository = SessionRepository(path, cache=RedisSessionCache(url, path, prefix=prefix, ttl=30))
        assert repository.get(sid)["title"] == "persisted"
        repository.delete(sid)
        with pytest.raises(KeyError):
            repository.get(sid)
    finally:
        repository.close()
        if keys:
            admin.delete(*keys)


def test_real_redis_cross_client_pubsub_wakes_async_replay(tmp_path, real_redis):
    url, prefix, admin = real_redis
    path = tmp_path / "sessions.sqlite3"
    cache = RedisSessionCache(url, path, prefix=prefix, ttl=30)
    repository = SessionRepository(path, cache=cache)
    sid = repository.create()["id"]
    repository.start()
    try:
        wait_until(lambda: cache.ready, timeout=5)

        async def exercise():
            version = cache.version(sid)
            task = asyncio.create_task(repository.wait_for_events(sid, version))
            await asyncio.sleep(0)
            # Independent command client imitates another producer. The SQL row
            # commits first; Pub/Sub contains no data needed for replay.
            with repository.connection() as database:
                database.execute("INSERT INTO events(session_id,kind,payload,created_at) VALUES (?,?,?,?)", (
                    sid, "external", '{"ok":true}', "synthetic",
                ))
            await asyncio.to_thread(admin.publish, cache.channel, sid)
            await asyncio.wait_for(task, 1)
            assert cache.version(sid) > version
            assert repository.event_page(sid)["events"][0]["payload"] == {"ok": True}

        asyncio.run(exercise())
    finally:
        repository.close()
        admin.delete(cache._key(sid, 0))


def test_real_redis_application_keeps_live_state_and_sse_in_sqlite(tmp_path, real_redis):
    url, prefix, admin = real_redis
    app = create_app(HarnessConfig(tmp_path, "test-model", redis_url=url, redis_prefix=prefix), FakeClient)
    repository = app.state.application.repository
    keys = set()
    try:
        with TestClient(app) as client:
            sid = client.post("/api/agent/sessions", headers={"X-Agent-Client": "web"}).json()["id"]
            cache = repository.cache
            keys.add(cache._key(sid, 0))
            wait_until(lambda: cache.ready, timeout=5)
            health = client.get("/api/agent/health").json()["redis"]
            assert health == {"configured": True, "cache_available": True, "notifications_available": True}
            repository.emit(sid, "durable", {"source": "sqlite"})
            replay = client.get(f"/api/agent/sessions/{sid}/events?once=true")
            assert "durable" in replay.text and "sqlite" in replay.text
            changed = client.patch(f"/api/agent/sessions/{sid}", headers={"X-Agent-Client": "web"}, json={
                "personalized": False, "temporary": True,
            })
            assert changed.status_code == 200 and not changed.json()["personalized"]
            assert changed.json()["temporary"]
            with repository.connection() as database:
                revision = database.execute("SELECT revision FROM sessions WHERE id=?", (sid,)).fetchone()[0]
            keys.add(cache._key(sid, revision))
            assert all("approvals" not in json.loads(admin.get(key))["record"] for key in keys if admin.get(key))
            assert client.delete(f"/api/agent/sessions/{sid}", headers={"X-Agent-Client": "web"}).status_code == 204
            assert client.get(f"/api/agent/sessions/{sid}/events?once=true").status_code == 404
    finally:
        if keys:
            admin.delete(*keys)
