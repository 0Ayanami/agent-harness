"""Redis caches visible text only; model history and durable events remain in SQL."""

import asyncio
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
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

    def eval(self, script, key_count, key, version, value, ttl):
        previous = self.get(key)
        if previous:
            try:
                previous_id = json.loads(previous)["version"]["id"]
                if type(previous_id) is int and previous_id > version:
                    return 0
            except (KeyError, TypeError, ValueError):
                pass
        self.set(key, value, ex=ttl)
        return 1

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


def test_text_cache_is_used_by_replay_and_never_by_model_history(tmp_path):
    repository, cache, redis = components(tmp_path)
    original = repository.create()
    sid = original["id"]
    key = cache._key(sid)
    assert redis.values == {}, "Restoring model state does not touch the text cache"
    assert "revision" not in original
    history = [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "answer"},
            {"type": "tool_use", "id": "t1", "name": "read", "input": {"private": "tool input"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "private tool result"},
        ]},
    ]
    repository.save(sid, "Current title", history)
    repository.emit(sid, "user", {"text": "question"})
    repository.emit(sid, "tool_finished", {"name": "read", "output": "private tool result"})
    repository.emit(sid, "assistant", {"text": "answer", "scope": "main", "run_id": "run1"})
    expected = repository.event_page(sid, snapshot=True, cache_text=True)
    payload = redis.values[key]
    assert redis.expirations[key] == 45
    assert [event["kind"] for event in json.loads(payload)["events"]] == ["user", "assistant"]
    assert all(term not in payload for term in ("history", "tool_use", "private", "approvals"))
    assert expected["events"][1]["payload"]["output"] == "private tool result"

    statements = []
    connection = repository.connection

    @contextmanager
    def trace_connection():
        with connection() as database:
            database.set_trace_callback(statements.append)
            yield database

    repository.connection = trace_connection
    assert repository.event_page(sid, snapshot=True, cache_text=True) == expected
    assert not any("SELECT * FROM events" in sql and "kind IN" in sql for sql in statements), (
        "A cache hit must skip loading text payloads from SQL"
    )
    assert repository.get(sid)["history"] == history
    # A checkpoint changes full model history, not the visible conversation.
    repository.save(sid, "Compacted title", [{"role": "user", "content": "compacted model context"}])
    assert redis.values[key] == payload
    assert repository.get(sid)["history"][0]["content"] == "compacted model context"
    assert repository.event_page(sid, snapshot=True, cache_text=True) == expected
    repository.emit(sid, "tool_finished", {"name": "read", "output": "another private result"})
    expected = repository.event_page(sid, snapshot=True, cache_text=True)
    assert redis.values[key] == payload, "Tool events do not change the text-cache version"
    options = repository.update(sid, personalized=False, temporary=True, archived=True)
    assert options == {"personalized": False, "temporary": True, "archived": True}
    current = repository.get(sid)
    assert not current["personalized"] and current["temporary"] and current["archived"]
    assert redis.values[key] == payload, "Memory settings do not invalidate unchanged visible text"
    assert repository.event_page(sid, snapshot=True, cache_text=True) == expected
    repository.delete(sid)
    assert key not in redis.values
    redis.values[key] = payload  # A late reader cannot resurrect a deleted SQL session.
    with pytest.raises(KeyError):
        repository.get(sid)
    assert not repository.event_page(sid, snapshot=True, cache_text=True)["exists"]
    assert repository.events(sid) == []
    assert all(message == sid for _, message in redis.messages), "Wakeups contain IDs only"
    repository.close()


def test_new_text_and_redis_recovery_never_serve_a_stale_transcript(tmp_path, caplog):
    repository, cache, redis = components(tmp_path)
    sid = repository.create()["id"]
    repository.emit(sid, "user", {"text": "first"})
    repository.event_page(sid, snapshot=True, cache_text=True)
    key = cache._key(sid)
    stale = redis.values[key]
    redis.failure = True
    repository.save(sid, "committed", [{"role": "user", "content": "durable"}])
    repository.emit(sid, "assistant", {"text": "latest answer"})
    assert redis.values[key] == stale
    expected = repository.event_page(sid, snapshot=True, cache_text=True)
    assert expected["events"][-1]["payload"]["text"] == "latest answer"
    assert repository.get(sid)["title"] == "committed"
    assert not cache.status()["cache_available"]
    assert "secret-password" not in caplog.text and "private-host" not in caplog.text
    redis.failure = False
    cache._disabled_until = 0
    assert repository.get(sid)["history"][0]["content"] == "durable"
    assert repository.event_page(sid, snapshot=True, cache_text=True) == expected
    assert cache.status()["cache_available"]
    redis.values[key] = stale  # Even a racing refill must fail the SQL event-version check.
    assert repository.event_page(sid, snapshot=True, cache_text=True) == expected
    assert redis.values[key] != stale
    repository.close()


def test_corrupt_or_non_text_cache_entries_are_rebuilt_from_sql(tmp_path):
    repository, cache, redis = components(tmp_path)
    sid = repository.create()["id"]
    repository.emit(sid, "assistant", {"text": "durable text", "scope": "main", "extra": "not cached"})
    expected = repository.event_page(sid, snapshot=True, cache_text=True)
    key = cache._key(sid)
    original = json.loads(redis.values[key])
    invalid = []
    for field, value in (("payload", [123]), ("kind", "tool_finished"), ("session_id", "other"), ("id", True)):
        candidate = json.loads(json.dumps(original))
        candidate["events"][0][field] = value
        invalid.append(candidate)
    candidate = json.loads(json.dumps(original))
    candidate["events"][0]["payload"]["input"] = {"tool": "secret"}
    invalid.append(candidate)
    candidate = json.loads(json.dumps(original))
    candidate["version"]["created_at"] = "wrong backup timestamp"
    invalid.extend([candidate, {**original, "events": []}, {**original, "events": [123]}, 123])
    for payload in invalid:
        redis.values[key] = json.dumps(payload)
        assert repository.event_page(sid, snapshot=True, cache_text=True) == expected
    cached = repository.event_page(sid, snapshot=True, cache_text=True)
    cached["events"][0]["payload"]["text"] = "client mutation"
    assert repository.event_page(sid, snapshot=True, cache_text=True) == expected
    repository.close()


def test_oversized_transcript_and_fixed_replay_boundary_fall_back_safely(tmp_path):
    repository, cache, redis = components(tmp_path)
    sid = repository.create()["id"]
    repository.emit(sid, "user", {"text": "first"})
    first = repository.event_page(sid, snapshot=True, cache_text=True)
    cache.MAX_RECORD_BYTES = 100
    repository.emit(sid, "assistant", {"text": "a" * 200})
    page = repository.event_page(sid, snapshot=True, cache_text=True)
    assert len(page["events"]) == 2
    assert repository.event_page(sid, through=first["through"], cache_text=True)["events"] == first["events"]
    assert len(json.loads(redis.values[cache._key(sid)])["events"]) == 1
    repository.close()


def test_paged_replay_keeps_its_boundary_and_rebuilds_incomplete_cached_text(tmp_path):
    repository, cache, redis = components(tmp_path)
    sid = repository.create()["id"]
    for index in range(205):
        repository.emit(sid, "assistant" if index % 2 else "tool_finished", {
            "text": f"answer {index}", "output": f"private tool {index}",
        })
    first = repository.event_page(sid, snapshot=True, cache_text=True)
    assert len(first["events"]) == 200
    repository.emit(sid, "user", {"text": "after the snapshot"})
    second = repository.event_page(
        sid, first["events"][-1]["id"], through=first["through"], cache_text=True,
    )
    assert len(second["events"]) == 5
    assert second["events"][-1]["id"] == first["through"]
    assert repository.event_page(sid, snapshot=True, cache_text=True)["through"] > first["through"]
    # A structurally valid JSON value can still have lost an intermediate text.
    key = cache._key(sid)
    incomplete = json.loads(redis.values[key])
    incomplete["events"].pop(0)
    redis.values[key] = json.dumps(incomplete)
    restored = repository.event_page(sid, snapshot=True, cache_text=True)
    assert len(restored["events"]) == 200
    assert restored["events"][1]["payload"]["text"] == "answer 1"
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


def test_real_redis_text_cache_ttl_and_sqlite_model_reopen(tmp_path, real_redis):
    url, prefix, admin = real_redis
    path = tmp_path / "sessions.sqlite3"
    cache = RedisSessionCache(url, path, prefix=prefix, ttl=30)
    repository = SessionRepository(path, cache=cache)
    keys = set()
    try:
        sid = repository.create()["id"]
        key = cache._key(sid)
        keys.add(key)
        assert admin.get(key) is None
        repository.save(sid, "persisted", [{"role": "user", "content": "Redis is a cache"}])
        repository.emit(sid, "user", {"text": "visible question"})
        first = json.loads(admin.get(key))
        assert first["events"][0]["payload"]["text"] == "visible question"
        repository.emit(sid, "assistant", {"text": "visible answer", "stream_id": "draft-1"})
        warmed = json.loads(admin.get(key))
        assert warmed["events"][-1]["payload"] == {"text": "visible answer", "stream_id": "draft-1"}
        # A historical replay finishing late must not overwrite the newer cache.
        cache.put(sid, first["version"], first["events"])
        assert json.loads(admin.get(key)) == warmed
        expected = repository.event_page(sid, snapshot=True, cache_text=True)
        assert repository.get(sid)["title"] == "persisted"
        assert 0 < admin.ttl(key) <= 30
        assert json.loads(admin.get(key))["events"][0]["payload"]["text"] == "visible question"
        assert "Redis is a cache" not in admin.get(key)
        repository.close()
        repository = SessionRepository(path, cache=RedisSessionCache(url, path, prefix=prefix, ttl=30))
        assert repository.get(sid)["title"] == "persisted"
        assert repository.get(sid)["history"][0]["content"] == "Redis is a cache"
        assert repository.event_page(sid, snapshot=True, cache_text=True) == expected
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
        admin.delete(cache._key(sid))


def test_real_redis_application_keeps_live_state_and_sse_in_sqlite(tmp_path, real_redis):
    url, prefix, admin = real_redis
    app = create_app(HarnessConfig(tmp_path, "test-model", redis_url=url, redis_prefix=prefix), FakeClient)
    repository = app.state.application.repository
    keys = set()
    try:
        with TestClient(app) as client:
            sid = client.post("/api/agent/sessions", headers={"X-Agent-Client": "web"}).json()["id"]
            cache = repository.cache
            keys.add(cache._key(sid))
            wait_until(lambda: cache.ready, timeout=5)
            repository.emit(sid, "user", {"text": "visible text"})
            repository.emit(sid, "durable", {"source": "sqlite"})
            replay = client.get(f"/api/agent/sessions/{sid}/events?once=true")
            assert "durable" in replay.text and "sqlite" in replay.text and "visible text" in replay.text
            health = client.get("/api/agent/health").json()["redis"]
            assert health == {"configured": True, "cache_available": True, "notifications_available": True}
            changed = client.patch(f"/api/agent/sessions/{sid}", headers={"X-Agent-Client": "web"}, json={
                "personalized": False, "temporary": True,
            })
            assert changed.status_code == 200 and not changed.json()["personalized"]
            assert changed.json()["temporary"]
            payload = admin.get(cache._key(sid))
            assert "approvals" not in payload and "sqlite" not in payload and "history" not in payload
            assert client.delete(f"/api/agent/sessions/{sid}", headers={"X-Agent-Client": "web"}).status_code == 204
            assert client.get(f"/api/agent/sessions/{sid}/events?once=true").status_code == 404
    finally:
        if keys:
            admin.delete(*keys)


def test_web_text_replay_preserves_tools_citations_and_lightweight_snapshots(tmp_path):
    app = create_app(HarnessConfig(tmp_path, "test-model"), FakeClient)
    repository = app.state.application.repository
    cache = RedisSessionCache("redis://unused", repository.path, client=FakeRedis())
    repository.cache = cache
    # A fake command client intentionally has no subscriber thread.
    cache.start = lambda: None
    headers = {"X-Agent-Client": "web"}
    with TestClient(app) as client:
        sid = client.post("/api/agent/sessions", headers=headers).json()["id"]
        repository.emit(sid, "user", {"text": "question"})
        repository.emit(sid, "tool_started", {"name": "library_search", "input": {"query": "question"}})
        repository.emit(sid, "assistant", {"text": "answer [S1]", "run_id": "r1", "scope": "main"})
        repository.emit(sid, "library_answer", {"text": "answer [S1]", "run_id": "r1", "citations": [{"id": "S1"}]})
        repository.emit(sid, "run_finished", {"outcome": "completed"})
        path = f"/api/agent/sessions/{sid}"
        assert client.get(path + "?include_history=false").json()["history"] == []
        first = client.get(path + "/events?once=true")
        payload = cache.client.values[cache._key(sid)]
        assert "library_search" not in payload and "citations" not in payload
        assert '"tool_started"' in first.text and '"citations"' in first.text
        assert first.text == client.get(path + "/events?once=true").text
        assert json.loads(payload)["events"][-1]["payload"]["run_id"] == "r1"


def test_legacy_lightweight_snapshot_filters_tool_blocks_but_sql_restores_them(tmp_path):
    app = create_app(HarnessConfig(tmp_path, "test-model"), FakeClient)
    repository = app.state.application.repository
    sid = repository.create()["id"]
    history = [
        {"role": "user", "content": "legacy question"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "legacy answer"},
            {"type": "tool_use", "id": "t1", "name": "read", "input": {}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "private output"},
        ]},
    ]
    repository.save(sid, "legacy", history)
    with TestClient(app) as client:
        light = client.get(f"/api/agent/sessions/{sid}?include_history=false").json()
        assert light["history"] == [
            {"role": "user", "content": "legacy question"},
            {"role": "assistant", "content": "legacy answer"},
        ]
        assert repository.get(sid)["history"] == history
        assert app.state.application.get(sid).record["history"] == history
