"""One application use case for persistent sessions, execution and event delivery."""

import threading
from dataclasses import dataclass, field

from ..core.messages import json_value, repair_interrupted_turn
from ..infrastructure.sessions import SessionRepository
from .approvals import ApprovalBroker
from .runtime import AgentRuntime


@dataclass
class LiveSession:
    record: dict
    runtime: AgentRuntime
    approvals: ApprovalBroker
    status: str = "idle"
    active_request: str = "(no active request)"
    lock: object = field(default_factory=threading.RLock)
    worker: threading.Thread | None = None


class SessionService:
    def __init__(self, config, client_factory):
        self.config = config
        self.client_factory = client_factory
        self.repository = SessionRepository(config.data_dir / "sessions.sqlite3")
        self.sessions = {}
        self.lock = threading.RLock()
        self.memory_lock = threading.RLock()
        self.shutdown = threading.Event()
        self.pump = None

    def start(self):
        if self.pump is None:
            self.pump = threading.Thread(target=self._pump_events, daemon=True)
            self.pump.start()

    def get(self, session_id):
        with self.lock:
            if session_id not in self.sessions:
                record = self.repository.get(session_id)
                emit = lambda kind, payload: self.repository.emit(session_id, kind, payload)
                runtime = AgentRuntime(
                    self.config,
                    self.client_factory(),
                    state_dir=self.config.data_dir / "sessions" / session_id,
                    emit=emit,
                    memory_lock=self.memory_lock,
                )
                approvals = ApprovalBroker(emit, runtime.cancelled, self.config.approval_timeout)
                runtime.permissions.approve = approvals.ask
                runtime.scheduler.start_runtime_services()
                self.sessions[session_id] = LiveSession(record, runtime, approvals)
            return self.sessions[session_id]

    def create(self):
        record = self.repository.create()
        return self.snapshot(record["id"])

    def snapshot(self, session_id):
        session = self.get(session_id)
        # Avoid the execution lock: this endpoint must work while a worker awaits approval.
        return {
            **session.record,
            "history": json_value(session.record["history"]),
            "status": session.status,
            "approvals": session.approvals.snapshot(),
            "cursor": self.repository.cursor(session_id),
            "state": session.runtime.summary(),
        }

    def submit(self, session_id, message):
        session = self.get(session_id)
        with session.lock:
            if session.status == "running":
                raise RuntimeError("A turn is already running in this session")
            session.runtime.cancelled.clear()
            session.active_request = message
            if session.record["title"] == "新对话":
                session.record["title"] = message[:40]
            session.record["history"].append({"role": "user", "content": message})
            self._checkpoint(session)
            session.runtime.emit("user", {"text": message})
            self._launch(session)

    def _checkpoint(self, session):
        self.repository.save(
            session.record["id"], session.record["title"], session.record["history"]
        )

    def _launch(self, session):
        session.status = "running"
        session.runtime.emit("run_started", {})
        session.worker = threading.Thread(target=self._run, args=(session,), daemon=True)
        session.worker.start()

    def _run(self, session):
        outcome = "completed"
        try:
            session.runtime.run(
                session.record["history"],
                session.active_request,
                checkpoint=lambda: self._checkpoint(session),
            )
        except InterruptedError:
            outcome = "cancelled"
        except Exception as error:
            outcome = "failed"
            session.runtime.emit("error", {"message": f"{type(error).__name__}: {error}"})
        finally:
            repair_interrupted_turn(session.record["history"])
            with session.lock:
                self._checkpoint(session)
                session.status = "idle"
                session.runtime.emit("run_finished", {"outcome": outcome})

    def cancel(self, session_id):
        session = self.get(session_id)
        session.runtime.cancel()
        session.approvals.cancel_all()

    def _pump_events(self):
        while not self.shutdown.wait(0.3):
            with self.lock:
                sessions = list(self.sessions.values())
            for session in sessions:
                with session.lock:
                    if (
                        session.status == "idle"
                        and not session.runtime.cancelled.is_set()
                        and session.runtime.has_events()
                    ):
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
