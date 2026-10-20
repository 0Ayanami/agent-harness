"""SQLite persistence is isolated from model execution and HTTP routes."""

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4

from ..core.messages import json_value, repair_interrupted_turn


def timestamp():
    return datetime.now(timezone.utc).isoformat()


class SessionRepository:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.lock = threading.RLock()
        with self.connection() as database:
            database.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, history TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                    kind TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS event_session ON events(session_id, id);
            """)

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

    def create(self):
        session_id, now = uuid4().hex, timestamp()
        with self.connection() as database:
            database.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
                (session_id, "新对话", "[]", now, now),
            )
        return self.get(session_id)

    def get(self, session_id):
        with self.connection() as database:
            row = database.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            raise KeyError(session_id)
        result = dict(row)
        result["history"] = json.loads(result["history"])
        repair_interrupted_turn(result["history"])
        return result

    def list(self):
        with self.connection() as database:
            return [
                dict(row)
                for row in database.execute(
                    "SELECT id, title, created_at, updated_at FROM sessions ORDER BY updated_at DESC"
                )
            ]

    def save(self, session_id, title, history):
        serialized = json.dumps(json_value(history), ensure_ascii=False)
        with self.connection() as database:
            database.execute(
                "UPDATE sessions SET title = ?, history = ?, updated_at = ? WHERE id = ?",
                (title, serialized, timestamp(), session_id),
            )

    def emit(self, session_id, kind, payload):
        with self.connection() as database:
            database.execute(
                "INSERT INTO events(session_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
                (
                    session_id,
                    kind,
                    json.dumps(json_value(payload), ensure_ascii=False),
                    timestamp(),
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
