"""Optional Redis text transcripts and lossy wakeups, never model history.

SQLite event cursors protect cache reads; Pub/Sub only wakes a durable event replay.
The single subscriber resolves asyncio waiters without occupying ASGI workers.
"""

import asyncio
import hashlib
import json
import logging
import threading
import time
from collections import OrderedDict


logger = logging.getLogger(__name__)

# Historical replay may finish after a newer message has already warmed the key.
# Compare and replace in one Redis operation so it cannot roll the key backwards.
PUT_TRANSCRIPT = """
local previous = redis.call('GET', KEYS[1])
if previous then
    local ok, data = pcall(cjson.decode, previous)
    if ok and type(data) == 'table' and type(data.version) == 'table'
       and type(data.version.id) == 'number' and data.version.id > tonumber(ARGV[1]) then
        return 0
    end
end
redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3])
return 1
"""


class RedisSessionCache:
    MAX_RECORD_BYTES = 2 * 1024 * 1024
    MAX_GENERATIONS = 4096

    def __init__(self, url, database_path, *, prefix="ayanami", ttl=300, client=None):
        self.ttl = ttl
        fingerprint = hashlib.sha256(str(database_path.resolve()).encode()).hexdigest()[:24]
        self.namespace = f"{prefix}:{fingerprint}"
        self.channel = f"{self.namespace}:events"
        self._lock = threading.RLock()
        self._versions = OrderedDict()
        self._waiters = {}
        self._disabled_until = 0.0
        self._command_ok = False
        self._ready = False
        self._stop = threading.Event()
        self._thread = None
        self._pubsub = None
        self.client = client
        if client is None:
            try:
                import redis
                from redis.backoff import NoBackoff
                from redis.retry import Retry

                self.client = redis.Redis.from_url(
                    url, protocol=2, decode_responses=True,
                    socket_connect_timeout=0.15, socket_timeout=0.15,
                    retry=Retry(NoBackoff(), 0), health_check_interval=0,
                    max_connections=64,
                )
            except (ImportError, ValueError):
                logger.warning("Redis session acceleration unavailable; using SQLite")

    def _key(self, session_id):
        # A new key format leaves old full-history records to expire naturally.
        return f"{self.namespace}:transcript:v1:{session_id}"

    @property
    def can_access(self):
        return self.client is not None and time.monotonic() >= self._disabled_until and not self._stop.is_set()

    def _command(self, function, default=None):
        if not self.can_access:
            return default
        try:
            result = function()
            with self._lock:
                self._command_ok = True
            return result
        except Exception as error:
            # Redis may echo its URL/password. Persist only the exception class.
            with self._lock:
                self._disabled_until = time.monotonic() + 2
                self._command_ok = False
            logger.warning("Redis session acceleration paused (%s)", type(error).__name__)
            return default

    def get(self, session_id, version):
        value = self._command(lambda: self.client.get(self._key(session_id)))
        if not value:
            return None
        try:
            if len(value.encode() if isinstance(value, str) else value) > self.MAX_RECORD_BYTES:
                return None
            envelope = json.loads(value)
            events = envelope["events"]
            if envelope["version"] != version or not isinstance(events, list):
                return None
            previous = 0
            for event in events:
                if (set(event) != {"id", "session_id", "kind", "payload", "created_at"}
                        or type(event["id"]) is not int or event["id"] <= previous
                        or event["session_id"] != session_id
                        or event["kind"] not in {"user", "assistant"}
                        or not isinstance(event["created_at"], str)):
                    return None
                payload = event["payload"]
                if (not isinstance(payload, dict) or "text" not in payload
                        or not payload.keys() <= {"text", "scope", "run_id", "stream_id"}
                        or any(not isinstance(item, str) for item in payload.values())):
                    return None
                previous = event["id"]
            if previous != version["id"] or (events and events[-1]["created_at"] != version["created_at"]):
                return None
            return events
        except (KeyError, TypeError, ValueError):
            return None

    def status(self):
        with self._lock:
            return {
                "configured": True,
                "cache_available": self._command_ok and self.can_access,
                "notifications_available": self._ready and not self._stop.is_set(),
            }

    def put(self, session_id, version, events):
        payload = json.dumps({"version": version, "events": events}, ensure_ascii=False)
        if len(payload.encode()) <= self.MAX_RECORD_BYTES:
            self._command(lambda: self.client.eval(
                PUT_TRANSCRIPT, 1, self._key(session_id), version["id"], payload, self.ttl,
            ))

    def discard(self, session_id):
        self._command(lambda: self.client.delete(self._key(session_id)))

    @property
    def ready(self):
        with self._lock:
            return self._ready

    def version(self, session_id):
        with self._lock:
            return self._versions.get(session_id, 0)

    @staticmethod
    def _resolve(future):
        if not future.done():
            future.set_result(None)

    def _wake(self, waiters):
        for loop, future in waiters:
            try:
                loop.call_soon_threadsafe(self._resolve, future)
            except RuntimeError:
                pass  # A disconnected request may already have closed its loop.

    def notify_local(self, session_id):
        with self._lock:
            self._versions[session_id] = self._versions.get(session_id, 0) + 1
            self._versions.move_to_end(session_id)
            while len(self._versions) > self.MAX_GENERATIONS:
                self._versions.popitem(last=False)
            waiters = list(self._waiters.get(session_id, ()))
        self._wake(waiters)

    def publish(self, session_id):
        # Local delivery remains immediate even during a Redis outage. Redis
        # Pub/Sub carries only the ID, never text or tool/approval content.
        self.notify_local(session_id)
        self._command(lambda: self.client.publish(self.channel, session_id))

    async def wait(self, session_id, version, *, timeout=2.0):
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        entry = (loop, future)
        with self._lock:
            if self._versions.get(session_id, 0) != version or self._stop.is_set():
                return
            self._waiters.setdefault(session_id, set()).add(entry)
            duration = min(timeout, 2.0 if self._ready else 0.3)
        try:
            await asyncio.wait_for(future, max(0.0, duration))
        except TimeoutError:
            pass
        finally:
            with self._lock:
                waiters = self._waiters.get(session_id)
                if waiters is not None:
                    waiters.discard(entry)
                    if not waiters:
                        self._waiters.pop(session_id, None)

    def _set_ready(self, value):
        with self._lock:
            changed = self._ready != value
            self._ready = value
            waiters = [entry for group in self._waiters.values() for entry in group] if changed else []
        self._wake(waiters)

    def start(self):
        if self.client is not None and self._thread is None:
            self._thread = threading.Thread(target=self._listen, name="redis-session-events", daemon=True)
            self._thread.start()

    def _listen(self):
        while not self._stop.is_set():
            pubsub = None
            try:
                pubsub = self.client.pubsub(ignore_subscribe_messages=False)
                self._pubsub = pubsub
                pubsub.subscribe(self.channel)
                while not self._stop.is_set():
                    message = pubsub.get_message(timeout=0.25)
                    if not message:
                        continue
                    if message["type"] == "subscribe":
                        self._set_ready(True)
                    elif message["type"] == "message":
                        identifier = message["data"]
                        if isinstance(identifier, bytes):
                            identifier = identifier.decode("utf-8", errors="ignore")
                        if isinstance(identifier, str) and 0 < len(identifier) <= 128:
                            self.notify_local(identifier)
            except Exception:
                self._set_ready(False)
                self._stop.wait(2)
            finally:
                if pubsub is not None:
                    try:
                        pubsub.close()
                    except Exception:
                        pass
                self._pubsub = None
                self._set_ready(False)

    def close(self):
        self._stop.set()
        self._set_ready(False)
        with self._lock:
            waiters = [entry for group in self._waiters.values() for entry in group]
        self._wake(waiters)
        if self._thread is not None:
            self._thread.join(timeout=1)
        if self.client is not None:
            try:
                self.client.close()
            except Exception:
                pass
