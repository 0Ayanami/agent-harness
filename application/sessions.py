"""One application use case for persistent sessions, execution and event delivery."""

import logging
import re
import shutil
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime

from ..core.messages import extract_text, json_value, repair_interrupted_turn
from ..infrastructure.cron_client import CronClient, RemoteScheduler
from ..infrastructure.sessions import SessionRepository
from ..infrastructure.session_cache import RedisSessionCache
from ..services.library_rag import library_options
from ..services.scheduler import Scheduler
from .approvals import ApprovalBroker
from .runtime import AgentRuntime


logger = logging.getLogger(__name__)
SESSION_ID = re.compile(r"[0-9a-f]{32}\Z")


class SessionConflict(RuntimeError):
    """The conversation cannot be changed in its current lifecycle state."""


@dataclass
class LiveSession:
    record: dict
    runtime: AgentRuntime
    approvals: ApprovalBroker
    status: str = "idle"
    active_request: str = "(no active request)"
    active_library: dict | None = None
    lock: object = field(default_factory=threading.RLock)
    worker: threading.Thread | None = None
    retired: bool = False
    cancelled_cron_generation: int = 0
    assistant_draft: tuple[str, str] | None = None


class SessionService:
    def __init__(
        self, config, client_factory, *, retrieval=None, library=None,
        resume_schedules=False, stream_answers=False,
    ):
        self.config = config
        self.client_factory = client_factory
        self.retrieval = retrieval
        self.library = library
        self.resume_schedules = resume_schedules
        self.stream_answers = stream_answers
        database_path = config.data_dir / "sessions.sqlite3"
        cache = RedisSessionCache(
            config.redis_url, database_path,
            prefix=config.redis_prefix, ttl=config.redis_cache_ttl,
        ) if config.redis_url else None
        self.repository = SessionRepository(database_path, cache=cache)
        self.session_root = config.data_dir / "sessions"
        self.sessions = {}
        self.retiring_schedulers = {}
        self.lock = threading.RLock()
        self.memory_lock = threading.RLock()
        self.shutdown = threading.Event()
        self.pump = None
        self.cron = CronClient(config.cron_service_url, config.data_dir) if resume_schedules and config.cron_service_url else None
        self._cron_initialized = False
        self._cron_booted = False

    def start(self):
        if self.pump is None:
            self.repository.start()
            for session_id in self.repository.pending_cleanup():
                self._cleanup_deleted(session_id)
            if self.cron:
                try:
                    self._initialize_cron()
                except (OSError, ValueError):
                    logger.warning("Cron synchronization will retry after startup")
            if self.resume_schedules and not self.cron:
                for record in self.repository.list():
                    session_id = record["id"]
                    scheduler = self._stored_scheduler(session_id)
                    if scheduler and scheduler.list_jobs():
                        try:
                            self.get(session_id)
                        except Exception as error:
                            logger.warning(
                                "Could not resume schedules for %s (%s)",
                                session_id,
                                type(error).__name__,
                            )
            self.pump = threading.Thread(target=self._pump_events, daemon=True)
            self.pump.start()

    def _open(self, record):
        session_id = record["id"]
        emit = lambda kind, payload: self.repository.emit(session_id, kind, payload)
        runtime = AgentRuntime(
            self.config,
            self.client_factory(),
            state_dir=self.session_root / session_id,
            emit=emit,
            memory_lock=self.memory_lock,
            retrieval=self.retrieval,
            library=self.library,
            scheduler=self._remote_scheduler(session_id) if self.cron else None,
            personalized=record["personalized"],
            temporary=record["temporary"],
        )
        approvals = ApprovalBroker(emit, runtime.cancelled, self.config.approval_timeout)
        runtime.permissions.approve = approvals.ask
        runtime.scheduler.start_runtime_services()
        session = LiveSession(record, runtime, approvals)
        self.sessions[session_id] = session
        return session

    def get(self, session_id):
        with self.lock:
            session = self.sessions.get(session_id)
            if session is not None:
                return session
            record = self.repository.get(session_id)
            if record["archived"]:
                raise SessionConflict("Restore the archived conversation first")
            return self._open(record)

    def create(self, *, personalized=True, temporary=False):
        record = self.repository.create(personalized=personalized, temporary=temporary)
        return self.snapshot(record["id"])

    def snapshot(self, session_id, *, include_history=True):
        # Avoid the execution lock: snapshots must work while a worker awaits approval.
        with self.lock:
            session = self.sessions.get(session_id)
            record = self.repository.get(session_id) if session is None else session.record
            if session is None and not record["archived"]:
                session = self._open(record)
            snapshot = {
                **record,
                "status": session.status if session else "idle",
                "approvals": session.approvals.snapshot() if session else [],
                "cursor": self.repository.cursor(session_id),
            }
        if include_history:
            snapshot["history"] = json_value(snapshot["history"])
        elif self.repository.has_transcript(session_id):
            # Web restores visible text from the cached event replay, without
            # serializing model/tool context a second time in its snapshot.
            snapshot["history"] = []
        else:
            # Sessions imported before durable events still need a text fallback.
            snapshot["history"] = [
                {"role": message["role"], "content": text}
                for message in snapshot["history"]
                if message["role"] in {"user", "assistant"}
                and (text := extract_text(message["content"]))
            ]
        # The shared memory lock may be held by an extraction in a different session.
        if session is None:
            snapshot["state"] = {
                "tasks": [],
                "todos": [],
                "teammates": {},
                "crons": [],
                "memory_catalog": "",
            }
        else:
            try:
                snapshot["state"] = session.runtime.summary()
            except FileNotFoundError:
                if not session.retired:
                    raise
                # A concurrent delete may remove task files after the snapshot captured the record.
                return self.snapshot(session_id, include_history=include_history)
        return snapshot

    def _retire(self, session):
        session.retired = True
        session.approvals.cancel_all()
        self.sessions.pop(session.record["id"])
        try:
            session.runtime.close()
        except Exception:
            logger.exception("Could not close retired conversation runtime")
        # A timed-out scheduler may still be completing a durable file write.
        thread = getattr(session.runtime.scheduler, "thread", None)
        if thread is not None and thread.is_alive():
            self.retiring_schedulers[session.record["id"]] = thread

    def update(self, session_id, *, archived=None, personalized=None, temporary=None):
        changes = {"archived": archived, "personalized": personalized, "temporary": temporary}
        with self.lock:
            session = self.sessions.get(session_id)
            if session is None:
                thread = self.retiring_schedulers.get(session_id)
                if archived is False and thread is not None and thread.is_alive():
                    raise SessionConflict("Wait for the conversation scheduler to stop")
                self._update_options(session_id, **changes)
            else:
                with session.lock:
                    if session.status == "running":
                        raise SessionConflict("Wait for the current turn to finish")
                    if archived is True and session.runtime.has_live_workers():
                        raise SessionConflict("Wait for background work and teammates to finish")
                    options = self._update_options(session_id, **changes)
                    session.record.update(options)
                    if options["archived"]:
                        self._retire(session)
                    elif personalized is not None or temporary is not None:
                        session.runtime.set_memory_options(
                            personalized=options["personalized"], temporary=options["temporary"]
                        )
        return self.snapshot(session_id)

    def _update_options(self, session_id, **changes):
        archived = changes.get("archived")
        if not self.cron or archived is None:
            return self.repository.update(session_id, **changes)
        previous = self.repository.get(session_id)["archived"]
        if previous == archived:
            return self.repository.update(session_id, **changes)
        self.cron.call("pause", session_id, paused=archived)
        try:
            return self.repository.update(session_id, **changes)
        except Exception:
            self.cron.call("pause", session_id, paused=previous)
            raise

    def delete(self, session_id):
        with self.lock:
            session = self.sessions.get(session_id)
            if session is None:
                self.repository.delete(session_id)
            else:
                with session.lock:
                    if session.status == "running" or session.runtime.has_live_workers():
                        raise SessionConflict("Wait for the turn and background work to finish")
                    self.repository.delete(session_id)
                    self._retire(session)
            thread = self.retiring_schedulers.get(session_id)
            if thread is None or not thread.is_alive():
                self._cleanup_deleted(session_id)

    def _cleanup_deleted(self, session_id):
        path = self.session_root / session_id
        try:
            if not SESSION_ID.fullmatch(session_id) or path.resolve() != path:
                raise ValueError("Unsafe session state path")
            if self.cron:
                self.cron.call("purge", session_id)
            try:
                shutil.rmtree(path)
            except FileNotFoundError:
                pass
        except (OSError, ValueError, RuntimeError):
            logger.warning("Could not clean deleted conversation state for %s", session_id)
            return
        self.repository.finish_cleanup(session_id)

    def _remote_scheduler(self, session_id):
        return RemoteScheduler(self.session_root / session_id, self.shutdown, self.cron, session_id)

    def _stored_scheduler(self, session_id, *, legacy=False):
        if not SESSION_ID.fullmatch(session_id):
            return None
        if self.cron and not legacy:
            return self._remote_scheduler(session_id)
        state_dir = self.session_root / session_id
        scheduler = Scheduler(state_dir, self.shutdown)
        try:
            path = scheduler.DURABLE_PATH
            if not path.is_file() or path.resolve() != path:
                return None
        except (OSError, RuntimeError):
            return None
        scheduler.load_durable_jobs()
        return scheduler

    @staticmethod
    def _scheduled_items(scheduler, record, now, job_id=None):
        return [
            {**job, "session_id": record["id"], "session_title": record["title"]}
            for job in scheduler.describe_jobs(now, job_id)
        ]

    def scheduled_tasks(self, session_id=None):
        now = datetime.fromisoformat(self.cron.call("now")) if self.cron else datetime.now()
        with self.lock:
            records = (
                self.repository.list() if session_id is None else [self.repository.get(session_id)]
            )
            sources = []
            for record in records:
                if record["archived"]:
                    raise SessionConflict("Restore the archived conversation first")
                identifier = record["id"]
                session = self.sessions.get(identifier)
                scheduler = (
                    session.runtime.scheduler if session else self._stored_scheduler(identifier)
                )
                if scheduler is not None:
                    sources.append((scheduler, dict(session.record) if session else record))
        items = [
            item
            for scheduler, record in sources
            for item in self._scheduled_items(scheduler, record, now)
        ]
        return {"items": items, "server_time": (now if self.cron else now.astimezone()).isoformat()}

    def create_scheduled_task(self, session_id, *, cron_expr, prompt, recurring, persistent):
        cron = " ".join(cron_expr.split())
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("Task instructions must not be empty")
        session = self.get(session_id)
        with session.lock:
            if session.retired:
                raise SessionConflict("The conversation has been archived or deleted")
            scheduler = session.runtime.scheduler
            with scheduler.cron_lock:
                job = scheduler.schedule_job(cron, prompt, recurring=recurring, durable=persistent)
                if isinstance(job, str):
                    raise ValueError(job)
                return self._scheduled_items(scheduler, session.record, datetime.now(), job.id)[0]

    def delete_scheduled_task(self, session_id, job_id):
        with self.lock:
            record = self.repository.get(session_id)
            if record["archived"]:
                raise SessionConflict("Restore the archived conversation first")
            session = self.sessions.get(session_id)
            scheduler = session.runtime.scheduler if session else self._stored_scheduler(session_id)
            return scheduler.remove_job(job_id) if scheduler else False

    def submit(self, session_id, message, library=None):
        options = library_options(library)
        session = self.get(session_id)
        with session.lock:
            if session.retired:
                raise SessionConflict("The conversation has been archived or deleted")
            if session.status == "running":
                raise SessionConflict("A turn is already running in this session")
            session.runtime.cancelled.clear()
            session.active_request = message
            session.active_library = options
            if session.record["title"] == "新对话":
                session.record["title"] = message[:40]
            session.record["history"].append({"role": "user", "content": message})
            self._checkpoint(session)
            session.runtime.emit(
                "user", {"text": message, **({"library": options} if options else {})}
            )
            self._launch(session, memory_source=[{"role": "user", "content": message}])

    def _checkpoint(self, session):
        self.repository.save(
            session.record["id"], session.record["title"], session.record["history"]
        )

    def _launch(self, session, *, memory_source=None):
        session.status = "running"
        session.runtime.emit("run_started", {})
        session.worker = threading.Thread(
            target=self._run, args=(session, memory_source or []), daemon=True
        )
        session.worker.start()

    def _run(self, session, memory_source):
        outcome = "completed"
        session_id = session.record["id"]
        last_wakeup = 0.0

        def update_draft(draft):
            nonlocal last_wakeup
            if session.assistant_draft == draft:
                return
            session.assistant_draft = draft
            now = time.monotonic()
            if draft is None or now - last_wakeup >= 0.05:
                last_wakeup = now
                self.repository.notify_draft(session_id)

        # A scheduler/background follow-up must not inherit a previous user scope.
        options, session.active_library = session.active_library, None
        try:
            arguments = {
                "checkpoint": lambda: self._checkpoint(session),
                "memory_source": memory_source,
            }
            if options is not None:
                arguments["library"] = options
            if self.stream_answers:
                arguments["on_draft"] = update_draft
            session.runtime.run(
                session.record["history"],
                session.active_request,
                **arguments,
            )
        except InterruptedError:
            outcome = "cancelled"
        except Exception as error:
            outcome = "failed"
            session.runtime.emit("error", {"message": f"{type(error).__name__}: {error}"})
        finally:
            update_draft(None)
            repair_interrupted_turn(session.record["history"])
            with session.lock:
                self._checkpoint(session)
                session.status = "idle"
                session.runtime.emit("run_finished", {"outcome": outcome})

    def assistant_draft(self, session_id):
        with self.lock:
            session = self.sessions.get(session_id)
            if session is not None and not session.runtime.cancelled.is_set():
                return session.assistant_draft
        return None

    def cancel(self, session_id):
        session = self.get(session_id)
        with session.lock:
            if session.retired:
                raise SessionConflict("The conversation has been archived or deleted")
            session.runtime.cancel()
            self.repository.notify_draft(session_id)
            session.approvals.cancel_all()
            try:
                session.cancelled_cron_generation = session.runtime.scheduler.due_generation()
            except OSError:
                # Cancellation of the in-process run still works during a detector outage.
                logger.warning("Could not record the current Cron generation")

    def _initialize_cron(self):
        if self._cron_initialized:
            return
        with self.lock:
            if not self._cron_booted:
                self.cron.call("boot")
                self._cron_booted = True
            for archived in (False, True):
                for record in self.repository.list(archived=archived):
                    sid = record["id"]
                    legacy = self._stored_scheduler(sid, legacy=True)
                    if legacy:
                        self.cron.call("migrate", sid, active=not archived,
                                       jobs=[asdict(job) for job in legacy.read_durable_jobs()])
                    self.cron.call("pause", sid, paused=archived)
            for sid in self.repository.pending_cleanup():
                self._cleanup_deleted(sid)
            self._cron_initialized = True

    def _due_sessions(self):
        if not self.cron:
            return {}
        try:
            self._initialize_cron()
            due = {row["session_id"]: row["latest"] for row in self.cron.call("due")}
            for sid in due:
                try:
                    self.get(sid)
                except (KeyError, SessionConflict):
                    # Local SQLite is authoritative for whether this session may run.
                    continue
                except Exception:
                    logger.exception("Could not open a conversation for Cron dispatch")
            return due
        except (OSError, ValueError):
            return {}

    def _pump_events(self):
        while not self.shutdown.wait(0.3):
            due = self._due_sessions()
            with self.lock:
                sessions = list(self.sessions.values())
                for session_id, thread in list(self.retiring_schedulers.items()):
                    if not thread.is_alive():
                        del self.retiring_schedulers[session_id]
                        if not self.repository.exists(session_id):
                            self._cleanup_deleted(session_id)
            for session in sessions:
                with session.lock:
                    if session.retired or session.status != "idle":
                        continue
                    if session.runtime.cancelled.is_set():
                        generation = due.get(session.record["id"], 0)
                        is_new = (generation > session.cancelled_cron_generation if self.cron
                                  else session.runtime.scheduler.has_new_due_job(session.cancelled_cron_generation))
                        if not is_new:
                            continue
                        session.runtime.cancelled.clear()
                    if session.record["id"] in due:
                        try:
                            session.runtime.scheduler.stage_due()
                        except OSError:
                            continue
                    if session.runtime.has_events():
                        self._launch(session)

    def close(self):
        self.shutdown.set()
        with self.lock:
            sessions = list(self.sessions.values())
        for session in sessions:
            session.approvals.cancel_all()
            session.runtime.close()
            if session.worker:
                session.worker.join(timeout=1)
        if self.pump:
            self.pump.join(timeout=1)
        self.repository.close()
        if self.cron:
            self.cron.close()
