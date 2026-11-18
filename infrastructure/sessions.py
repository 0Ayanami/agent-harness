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
TEXT_EVENT_KINDS = {"user", "assistant"}
EMPTY_TEXT_VERSION = {"id": 0, "created_at": ""}


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def _session_record(row):
    result = {**dict(row), **{key: bool(row[key]) for key in SESSION_FLAGS}}
    result.pop("revision", None)
    return result


def _text_event(row, payload):
    visible = {key: value for key, value in payload.items()
               if key in {"text", "scope", "run_id", "stream_id"} and isinstance(value, str)}
    visible.setdefault("text", "")
    return {**dict(row), "payload": visible}


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
                CREATE INDEX IF NOT EXISTS event_session_text ON events(session_id, id)
                    WHERE kind IN ('user','assistant');
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

    def notify_draft(self, session_id):
        if self.cache is not None:
            self.cache.notify_local(session_id)

    async def wait_for_events(self, session_id, version, *, timeout=2.0):
        if self.cache is not None:
            await self.cache.wait(session_id, version, timeout=timeout)
        else:
            await asyncio.sleep(min(timeout, 0.3))

    def cache_status(self):
        return self.cache.status() if self.cache is not None else {
            "configured": False, "cache_available": False, "notifications_available": False,
        }

    def _changed(self, session_id, *, deleted=False):
        if self.cache is not None:
            if deleted:
                self.cache.discard(session_id)
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
        # Model context (including tool_use/tool_result) is restored only from SQL.
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
        self._changed(session_id)
        return _session_record(row)

    def delete(self, session_id):
        with self.connection() as database:
            deleted = database.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            if deleted.rowcount != 1:
                raise KeyError(session_id)
            database.execute("DELETE FROM events WHERE session_id = ?", (session_id,))
            database.execute("INSERT INTO session_cleanup VALUES (?)", (session_id,))
        self._changed(session_id, deleted=True)

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
        if updated.rowcount:
            self._changed(session_id)

    def emit(self, session_id, kind, payload):
        payload = json_value(payload)
        created_at = timestamp()
        warm_text = kind in TEXT_EVENT_KINDS and self.cache is not None and self.cache.can_access
        with self.connection() as database:
            inserted = database.execute(
                "INSERT INTO events(session_id, kind, payload, created_at) "
                "SELECT ?, ?, ?, ? FROM sessions WHERE id = ? AND archived = 0",
                (
                    session_id,
                    kind,
                    json.dumps(payload, ensure_ascii=False),
                    created_at,
                    session_id,
                ),
            )
            if inserted.rowcount and warm_text:
                previous = database.execute(
                    "SELECT id,created_at FROM events WHERE session_id=? AND id<? "
                    "AND kind IN ('user','assistant') ORDER BY id DESC LIMIT 1",
                    (session_id, inserted.lastrowid),
                ).fetchone()
        if inserted.rowcount:
            self._changed(session_id)
            if warm_text and self.cache.can_access:
                version = {"id": inserted.lastrowid, "created_at": created_at}
                event = _text_event({**version, "session_id": session_id, "kind": kind}, payload)
                self._warm_transcript(session_id, dict(previous) if previous else EMPTY_TEXT_VERSION,
                                      version, event)

    def _warm_transcript(self, session_id, previous, version, event):
        # A valid cached prefix avoids rereading historical text from SQLite.
        texts = self.cache.get(session_id, previous) if previous["id"] else []
        if texts is not None:
            texts.append(event)
        elif self.cache.can_access:
            texts = self._read_text_events(session_id, version["id"], limit=self.cache.MAX_RECORD_BYTES)
        if texts and texts[-1]["id"] == version["id"]:
            self.cache.put(session_id, version, texts)

    def _read_text_events(self, session_id, through, *, after=0, limit=None):
        texts = []
        size = 0
        with self.connection() as database:
            rows = database.execute(
                "SELECT * FROM events WHERE session_id=? AND kind IN ('user','assistant') "
                "AND id>? AND id<=? ORDER BY id", (session_id, after, through),
            )
            for row in rows:
                event = _text_event(row, json.loads(row["payload"]))
                if limit is not None:
                    size += len(json.dumps(event, ensure_ascii=False).encode())
                    if size > limit:
                        return None
                texts.append(event)
        return texts

    def events(self, session_id, after=0, *, through=None):
        with self.connection() as database:
            rows = database.execute(
                "SELECT * FROM events WHERE session_id = ? AND id > ? AND (? IS NULL OR id <= ?) ORDER BY id LIMIT 200",
                (session_id, after, through, through),
            ).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    def event_page(self, session_id, after=0, *, through=None, snapshot=False, cache_text=False):
        """Read existence, replay boundary and one page using one SQLite snapshot.

        A fixed replay boundary excludes later writes while allowing pagination
        beyond 200 rows. Historical web replay caches immutable text events;
        live SSE reads SQL directly. Model-history revisions are irrelevant here.
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
            use_cache = exists and cache_text and self.cache is not None and self.cache.can_access
            columns = "id,session_id,kind,created_at" if use_cache else "*"
            rows = database.execute(
                f"SELECT {columns} FROM events WHERE session_id = ? AND id > ? "
                "AND (? IS NULL OR id <= ?) ORDER BY id LIMIT 200",
                (session_id, after, through, through),
            ).fetchall() if exists else []
            if use_cache and rows:
                # Fetch tool/citation/approval payloads only from SQL. Text payloads
                # come from a separately versioned, bounded Redis read model.
                auxiliary = database.execute(
                    "SELECT * FROM events WHERE session_id=? AND id>? AND id<=? "
                    "AND kind NOT IN ('user','assistant') ORDER BY id",
                    (session_id, after, rows[-1]["id"]),
                ).fetchall()
                latest = database.execute(
                    "SELECT id,created_at FROM events WHERE session_id=? "
                    "AND kind IN ('user','assistant') AND (? IS NULL OR id<=?) ORDER BY id DESC LIMIT 1",
                    (session_id, through, through),
                ).fetchone()
        if use_cache and rows:
            version = dict(latest) if latest else EMPTY_TEXT_VERSION
            texts = self.cache.get(session_id, version)
            if texts is not None:
                cached_ids = {event["id"] for event in texts}
                if any(row["kind"] in TEXT_EVENT_KINDS and row["id"] not in cached_ids
                       for row in rows):
                    texts = None
            if texts is None:
                if self.cache.can_access:
                    texts = self._read_text_events(session_id, version["id"], limit=self.cache.MAX_RECORD_BYTES)
                if texts is not None and (not version["id"] or
                                          (texts and texts[-1]["id"] == version["id"])):
                    self.cache.put(session_id, version, texts)
                if texts is None:
                    texts = self._read_text_events(session_id, rows[-1]["id"], after=after)
            by_id = {event["id"]: event for event in texts}
            by_id.update({row["id"]: {**dict(row), "payload": json.loads(row["payload"])}
                          for row in auxiliary})
            events = [by_id[row["id"]] for row in rows if row["id"] in by_id]
        else:
            events = [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]
        return {
            "exists": exists,
            "through": through,
            "events": events,
        }

    def cursor(self, session_id):
        with self.connection() as database:
            return database.execute(
                "SELECT COALESCE(MAX(id), 0) FROM events WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0]

    def has_transcript(self, session_id):
        with self.connection() as database:
            return database.execute(
                "SELECT 1 FROM events WHERE session_id=? "
                "AND kind IN ('user','assistant','scheduled') LIMIT 1", (session_id,),
            ).fetchone() is not None
