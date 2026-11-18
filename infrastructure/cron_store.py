"""Container-owned Cron plans and durable occurrences."""

import re
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from ..services.scheduler import Scheduler


class CronStore:
    def __init__(self, path, timezone):
        self.path = path
        self.zone = ZoneInfo(timezone)
        self.rules = Scheduler(path.parent, None)
        with self.transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS cron_sessions (
                    scope TEXT, session_id TEXT, active INTEGER NOT NULL, PRIMARY KEY(scope, session_id));
                CREATE TABLE IF NOT EXISTS cron_plans (
                    scope TEXT, session_id TEXT, id TEXT, cron TEXT NOT NULL, prompt TEXT NOT NULL,
                    recurring INTEGER NOT NULL, durable INTEGER NOT NULL, next_run_at INTEGER,
                    PRIMARY KEY(scope, session_id, id));
                CREATE INDEX IF NOT EXISTS cron_plans_due ON cron_plans(next_run_at);
                CREATE TABLE IF NOT EXISTS cron_due (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope TEXT, session_id TEXT, job_id TEXT, scheduled_at INTEGER,
                    available_at INTEGER NOT NULL, lease_token TEXT, lease_until INTEGER,
                    UNIQUE(scope, session_id, job_id, scheduled_at));
                CREATE INDEX IF NOT EXISTS cron_due_ready ON cron_due(scope, available_at);
            """)

    @contextmanager
    def transaction(self, *, write=True):
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db

    def now(self):
        return datetime.now(self.zone)

    def next_run(self, cron, now):
        candidate = self.rules.next_cron_run(cron, now.replace(second=0, microsecond=0))
        return int(candidate.timestamp()) if candidate else None

    @staticmethod
    def _id(value):
        if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
            raise ValueError("Invalid namespace or conversation ID")

    def rpc(self, scope, action, session_id=None, **args):
        self._id(scope)
        if session_id is not None:
            self._id(session_id)
        if action in {
            "migrate",
            "create",
            "list",
            "pause",
            "purge",
            "remove",
            "claim",
            "ack",
            "release",
        }:
            self._id(session_id)
        if action == "now":
            return self.now().isoformat()
        with self.transaction(write=action not in {"due", "list", "generation"}) as db:
            now = self.now()
            key = (scope, session_id)
            if action == "migrate":
                if db.execute(
                    "INSERT OR IGNORE INTO cron_sessions VALUES (?,?,?)", (*key, args["active"])
                ).rowcount:
                    for job in args["jobs"]:
                        self._create(db, key, now, job)
                return None
            if action == "boot":
                db.execute(
                    """DELETE FROM cron_due AS d WHERE scope=? AND EXISTS (
                    SELECT 1 FROM cron_plans p WHERE p.scope=d.scope AND p.session_id=d.session_id
                    AND p.id=d.job_id AND p.durable=0)""",
                    (scope,),
                )
                db.execute("DELETE FROM cron_plans WHERE scope=? AND durable=0", (scope,))
                return None
            if action == "generation":
                row = db.execute("SELECT seq FROM sqlite_sequence WHERE name='cron_due'").fetchone()
                return row[0] if row else 0
            if action == "create":
                db.execute("INSERT OR IGNORE INTO cron_sessions VALUES (?,?,1)", key)
                job = {**args, "id": "cron_" + uuid4().hex[:12]}
                self._create(db, key, now, job)
                return job
            if action == "pause":
                active = not args["paused"]
                updated = db.execute(
                    "UPDATE cron_sessions SET active=? WHERE scope=? AND session_id=? AND active<>?",
                    (active, *key, active),
                )
                if active and updated.rowcount:
                    for plan in db.execute(
                        "SELECT id,cron FROM cron_plans WHERE scope=? AND session_id=? AND next_run_at<?",
                        (*key, int(now.timestamp())),
                    ):
                        db.execute(
                            "UPDATE cron_plans SET next_run_at=? WHERE scope=? AND session_id=? AND id=?",
                            (self.next_run(plan["cron"], now), *key, plan["id"]),
                        )
                return None
            if action == "purge":
                db.execute("DELETE FROM cron_due WHERE scope=? AND session_id=?", key)
                db.execute("DELETE FROM cron_plans WHERE scope=? AND session_id=?", key)
                db.execute("DELETE FROM cron_sessions WHERE scope=? AND session_id=?", key)
                return None
            if action == "remove":
                return self._remove(db, key, args["job_id"])
            if action == "list":
                return self._list(db, key)
            if action == "due":
                return [
                    dict(row)
                    for row in db.execute(
                        """SELECT d.session_id, MAX(d.sequence) AS latest
                    FROM cron_due d JOIN cron_sessions s USING(scope,session_id)
                    WHERE d.scope=? AND s.active=1 AND d.available_at<=? AND COALESCE(d.lease_until,0)<=?
                    GROUP BY d.session_id""",
                        (scope, int(now.timestamp()), int(now.timestamp())),
                    )
                ]
            if action == "claim":
                return self._claim(db, key, int(now.timestamp()))
            if action in {"ack", "release"}:
                self._settle(db, key, action, args["token"], int(now.timestamp()))
                return None
            raise ValueError("Unknown Cron operation")

    def _create(self, db, key, now, job):
        error = self.rules.validate_cron(job["cron"])
        if error:
            raise ValueError(error)
        pending = job.get("pending_delivery", False)
        db.execute(
            "INSERT OR IGNORE INTO cron_plans VALUES (?,?,?,?,?,?,?,?)",
            (
                *key,
                job["id"],
                job["cron"],
                job["prompt"],
                job["recurring"],
                job["durable"],
                None if pending else self.next_run(job["cron"], now),
            ),
        )
        if pending:
            scheduled = int(now.replace(second=0, microsecond=0).timestamp())
            self._enqueue(db, key, job["id"], scheduled)

    @staticmethod
    def _enqueue(db, key, job_id, scheduled):
        db.execute(
            """INSERT OR IGNORE INTO cron_due(scope,session_id,job_id,scheduled_at,available_at)
            VALUES (?,?,?,?,?)""",
            (*key, job_id, scheduled, scheduled),
        )

    def _list(self, db, key):
        jobs = []
        for row in db.execute(
            """SELECT p.*, EXISTS (SELECT 1 FROM cron_due d WHERE d.scope=p.scope
                AND d.session_id=p.session_id AND d.job_id=p.id) AS queued FROM cron_plans p
                WHERE p.scope=? AND p.session_id=? ORDER BY p.rowid""",
            key,
        ):
            jobs.append(
                {
                    "id": row["id"],
                    "cron": row["cron"],
                    "prompt": row["prompt"],
                    "recurring": bool(row["recurring"]),
                    "durable": bool(row["durable"]),
                    "pending_delivery": bool(row["queued"]),
                    "status": "queued" if row["queued"] else "scheduled",
                    "next_run_at": datetime.fromtimestamp(row["next_run_at"], self.zone).isoformat()
                    if row["next_run_at"] is not None
                    else None,
                }
            )
        return jobs

    @staticmethod
    def _remove(db, key, job_id):
        db.execute(
            "DELETE FROM cron_due WHERE scope=? AND session_id=? AND job_id=?", (*key, job_id)
        )
        return bool(
            db.execute(
                "DELETE FROM cron_plans WHERE scope=? AND session_id=? AND id=?", (*key, job_id)
            ).rowcount
        )

    @staticmethod
    def _claim(db, key, now):
        row = db.execute(
            """SELECT d.job_id, d.scheduled_at, p.cron, p.prompt, p.recurring, p.durable
            FROM cron_due d JOIN cron_sessions s USING(scope,session_id)
            JOIN cron_plans p ON p.scope=d.scope AND p.session_id=d.session_id AND p.id=d.job_id
            WHERE d.scope=? AND d.session_id=? AND s.active=1 AND d.available_at<=?
                AND COALESCE(d.lease_until,0)<=? ORDER BY d.scheduled_at,d.job_id LIMIT 1""",
            (*key, now, now),
        ).fetchone()
        if row is None:
            return None
        token = uuid4().hex
        db.execute(
            "UPDATE cron_due SET lease_token=?, lease_until=? WHERE scope=? AND session_id=? AND job_id=? AND scheduled_at=?",
            (token, now + 300, *key, row["job_id"], row["scheduled_at"]),
        )
        return {**dict(row), "token": token}

    def _settle(self, db, key, action, token, now):
        row = db.execute(
            """SELECT d.job_id, p.recurring FROM cron_due d JOIN cron_plans p
            ON p.scope=d.scope AND p.session_id=d.session_id AND p.id=d.job_id
            WHERE d.scope=? AND d.session_id=? AND d.lease_token=?""",
            (*key, token),
        ).fetchone()
        if row is None:
            return
        if action == "release":
            db.execute(
                "UPDATE cron_due SET lease_token=NULL, lease_until=NULL, available_at=? WHERE scope=? AND session_id=? AND lease_token=?",
                (now + 5, *key, token),
            )
        elif row["recurring"]:
            db.execute(
                "DELETE FROM cron_due WHERE scope=? AND session_id=? AND lease_token=?",
                (*key, token),
            )
        else:
            self._remove(db, key, row["job_id"])

    def tick(self):
        now = self.now().replace(second=0, microsecond=0)
        with self.transaction() as db:
            plans = db.execute(
                """SELECT p.* FROM cron_plans p JOIN cron_sessions s USING(scope,session_id)
                WHERE s.active=1 AND p.next_run_at<=? ORDER BY p.next_run_at LIMIT 128""",
                (int(now.timestamp()),),
            ).fetchall()
            for plan in plans:
                key = (plan["scope"], plan["session_id"])
                scheduled = plan["next_run_at"]
                self._enqueue(db, key, plan["id"], scheduled)
                next_run = (
                    self.next_run(plan["cron"], now + timedelta(minutes=1))
                    if plan["recurring"]
                    else None
                )
                db.execute(
                    "UPDATE cron_plans SET next_run_at=? WHERE scope=? AND session_id=? AND id=?",
                    (next_run, *key, plan["id"]),
                )
            return len(plans)
