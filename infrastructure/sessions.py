"""SQLite persistence is isolated from model execution and HTTP routes."""

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
    return {**dict(row), **{key: bool(row[key]) for key in SESSION_FLAGS}}


class SessionRepository:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.lock = threading.RLock()
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
        with self.connection() as database:
            row = database.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            raise KeyError(session_id)
        result = _session_record(row)
        result["history"] = json.loads(result["history"])
        repair_interrupted_turn(result["history"])
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
                "personalized=COALESCE(?, personalized), temporary=COALESCE(?, temporary) "
                "WHERE id = ?",
                (archived, personalized, temporary, session_id),
            )
            if updated.rowcount != 1:
                raise KeyError(session_id)
            row = database.execute(
                "SELECT personalized,temporary,archived FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        return _session_record(row)

    def delete(self, session_id):
        with self.connection() as database:
            deleted = database.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            if deleted.rowcount != 1:
                raise KeyError(session_id)
            database.execute("DELETE FROM events WHERE session_id = ?", (session_id,))
            database.execute("INSERT INTO session_cleanup VALUES (?)", (session_id,))

    def pending_cleanup(self):
        with self.connection() as database:
            return [row[0] for row in database.execute("SELECT session_id FROM session_cleanup")]

    def finish_cleanup(self, session_id):
        with self.connection() as database:
            database.execute("DELETE FROM session_cleanup WHERE session_id = ?", (session_id,))

    def save(self, session_id, title, history):
        serialized = json.dumps(json_value(history), ensure_ascii=False)
        with self.connection() as database:
            database.execute(
                "UPDATE sessions SET title = ?, history = ?, updated_at = ? WHERE id = ? AND archived = 0",
                (title, serialized, timestamp(), session_id),
            )

    def emit(self, session_id, kind, payload):
        with self.connection() as database:
            database.execute(
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

    def events(self, session_id, after=0, *, through=None):
        with self.connection() as database:
            rows = database.execute(
                "SELECT * FROM events WHERE session_id = ? AND id > ? AND (? IS NULL OR id <= ?) ORDER BY id LIMIT 200",
                (session_id, after, through, through),
            ).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    def cursor(self, session_id):
        with self.connection() as database:
            return database.execute(
                "SELECT COALESCE(MAX(id), 0) FROM events WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0]
