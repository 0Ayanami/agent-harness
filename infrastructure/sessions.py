"""SQLite persistence is isolated from model execution and HTTP routes."""

import asyncio
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4

from ..core.messages import json_value, repair_interrupted_turn


SESSION_FLAGS = {"personalized": 1, "temporary": 0, "archived": 0}


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def _session_record(row):
    result = {**dict(row), **{key: bool(row[key]) for key in SESSION_FLAGS}}
    result.pop("revision", None)
    return result


class SessionRepository:
    def __init__(self, path, *, cache=None):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.lock = threading.RLock()
        self.cache = cache
        with self.connection() as database:
            database.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, history TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    personalized INTEGER NOT NULL DEFAULT 1,
                    temporary INTEGER NOT NULL DEFAULT 0,
                    archived INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                    kind TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS event_session ON events(session_id, id);
                CREATE TABLE IF NOT EXISTS session_cleanup (session_id TEXT PRIMARY KEY);
            """)
            database.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in database.execute("PRAGMA table_info(sessions)")}
            for name, default in SESSION_FLAGS.items():
                if name not in columns:
                    database.execute(
                        f"ALTER TABLE sessions ADD COLUMN {name} INTEGER NOT NULL DEFAULT {default}"
                    )
            if "revision" not in columns:
                database.execute("ALTER TABLE sessions ADD COLUMN revision INTEGER NOT NULL DEFAULT 0")

    def start(self):
        if self.cache is not None:
            self.cache.start()

    def close(self):
        if self.cache is not None:
            self.cache.close()

    def notification_version(self, session_id):
        return self.cache.version(session_id) if self.cache is not None else 0

    async def wait_for_events(self, session_id, version, *, timeout=2.0):
        if self.cache is not None:
            await self.cache.wait(session_id, version, timeout=timeout)
        else:
            await asyncio.sleep(min(timeout, 0.3))

    def cache_status(self):
        return self.cache.status() if self.cache is not None else {
            "configured": False, "cache_available": False, "notifications_available": False,
        }

    def _changed(self, session_id, revision=None):
        if self.cache is not None:
            if revision is not None:
                self.cache.discard(session_id, revision)
            self.cache.publish(session_id)

    @contextmanager
    def connection(self):
        with self.lock:
            database = sqlite3.connect(self.path, timeout=30)
            database.row_factory = sqlite3.Row
            try:
                with database:
                    yield database
            finally:
                database.close()

    def create(self, *, personalized=True, temporary=False):
        session_id, now = uuid4().hex, timestamp()
        with self.connection() as database:
            database.execute(
                "INSERT INTO sessions(id,title,history,created_at,updated_at,personalized,temporary) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (session_id, "新对话", "[]", now, now, personalized, temporary),
            )
        return self.get(session_id)

    def get(self, session_id):
        if self.cache is not None:
            # A Redis hit cannot establish existence or privacy settings. This
            # small authoritative read rejects stale versions after every write.
            with self.connection() as database:
                current = database.execute(
                    "SELECT revision,updated_at,personalized,temporary,archived FROM sessions WHERE id=?",
                    (session_id,),
                ).fetchone()
            if current is None:
                raise KeyError(session_id)
            cached = self.cache.get(session_id, current["revision"], {
                name: bool(current[name]) for name in SESSION_FLAGS
            }, current["updated_at"])
            if cached is not None:
                try:
                    repair_interrupted_turn(cached["history"])
                    return cached
                except (AttributeError, TypeError, KeyError, ValueError, IndexError):
                    # Redis is optional and untrusted as a source of records.
                    # A malformed history must never hide the valid SQL row.
                    pass
        with self.connection() as database:
            row = database.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            raise KeyError(session_id)
        result = _session_record(row)
        result["history"] = json.loads(result["history"])
        repair_interrupted_turn(result["history"])
        if self.cache is not None:
            self.cache.put(session_id, row["revision"], result)
        return result

    def exists(self, session_id):
        with self.connection() as database:
            return (
                database.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone()
                is not None
            )

    def list(self, *, archived=False):
        with self.connection() as database:
            return [
                _session_record(row)
                for row in database.execute(
                    "SELECT id,title,created_at,updated_at,personalized,temporary,archived "
                    "FROM sessions WHERE archived = ? ORDER BY updated_at DESC",
                    (archived,),
                )
            ]

    def update(self, session_id, *, archived=None, personalized=None, temporary=None):
        with self.connection() as database:
            updated = database.execute(
                "UPDATE sessions SET archived=COALESCE(?, archived), "
                "personalized=COALESCE(?, personalized), temporary=COALESCE(?, temporary), "
                "revision=revision+1 "
                "WHERE id = ?",
                (archived, personalized, temporary, session_id),
            )
            if updated.rowcount != 1:
                raise KeyError(session_id)
            row = database.execute(
                "SELECT personalized,temporary,archived,revision FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        self._changed(session_id, row["revision"] - 1)
        return _session_record(row)

    def delete(self, session_id):
        with self.connection() as database:
            previous = database.execute("SELECT revision FROM sessions WHERE id=?", (session_id,)).fetchone()
            deleted = database.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            if deleted.rowcount != 1:
                raise KeyError(session_id)
            database.execute("DELETE FROM events WHERE session_id = ?", (session_id,))
            database.execute("INSERT INTO session_cleanup VALUES (?)", (session_id,))
        self._changed(session_id, previous["revision"])

    def pending_cleanup(self):
        with self.connection() as database:
            return [row[0] for row in database.execute("SELECT session_id FROM session_cleanup")]

    def finish_cleanup(self, session_id):
        with self.connection() as database:
            database.execute("DELETE FROM session_cleanup WHERE session_id = ?", (session_id,))

    def save(self, session_id, title, history):
        serialized = json.dumps(json_value(history), ensure_ascii=False)
        with self.connection() as database:
            updated = database.execute(
                "UPDATE sessions SET title = ?, history = ?, updated_at = ?, revision=revision+1 "
                "WHERE id = ? AND archived = 0",
                (title, serialized, timestamp(), session_id),
            )
            revision = database.execute(
                "SELECT revision FROM sessions WHERE id=?", (session_id,)
            ).fetchone() if updated.rowcount else None
        if revision is not None:
            self._changed(session_id, revision["revision"] - 1)

    def emit(self, session_id, kind, payload):
        with self.connection() as database:
            inserted = database.execute(
                "INSERT INTO events(session_id, kind, payload, created_at) "
                "SELECT ?, ?, ?, ? FROM sessions WHERE id = ? AND archived = 0",
                (
                    session_id,
                    kind,
                    json.dumps(json_value(payload), ensure_ascii=False),
                    timestamp(),
                    session_id,
                ),
            )
        if inserted.rowcount:
            self._changed(session_id)

    def events(self, session_id, after=0, *, through=None):
        with self.connection() as database:
            rows = database.execute(
                "SELECT * FROM events WHERE session_id = ? AND id > ? AND (? IS NULL OR id <= ?) ORDER BY id LIMIT 200",
                (session_id, after, through, through),
            ).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    def event_page(self, session_id, after=0, *, through=None, snapshot=False):
        """Read existence, replay boundary and one page using one SQLite snapshot.

        SSE must not open separate connections or schedule separate worker calls
        for each of these small reads. A fixed replay boundary excludes later
        writes while still allowing the caller to paginate more than 200 rows.
        """
        with self.connection() as database:
            database.execute("BEGIN")
            exists = database.execute(
                "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
            ).fetchone() is not None
            if snapshot:
                through = database.execute(
                    "SELECT COALESCE(MAX(id), 0) FROM events WHERE session_id = ?", (session_id,)
                ).fetchone()[0]
            rows = database.execute(
                "SELECT * FROM events WHERE session_id = ? AND id > ? "
                "AND (? IS NULL OR id <= ?) ORDER BY id LIMIT 200",
                (session_id, after, through, through),
            ).fetchall() if exists else []
        return {
            "exists": exists,
            "through": through,
            "events": [{**dict(row), "payload": json.loads(row["payload"])} for row in rows],
        }

    def cursor(self, session_id):
        with self.connection() as database:
            return database.execute(
                "SELECT COALESCE(MAX(id), 0) FROM events WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0]
