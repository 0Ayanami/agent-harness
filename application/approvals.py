"""Blocking approval port for workers; browsers answer it through a separate request."""

import threading
import time
from dataclasses import dataclass, field
from uuid import uuid4


@dataclass
class PendingApproval:
    id: str
    name: str
    arguments: dict
    answer: bool | None = None
    ready: threading.Event = field(default_factory=threading.Event)


class ApprovalBroker:
    def __init__(self, emit, cancelled, timeout):
        self.emit = emit
        self.cancelled = cancelled
        self.timeout = timeout
        self.lock = threading.RLock()
        self.pending = {}

    def ask(self, name, arguments):
        approval = PendingApproval(uuid4().hex, name, arguments)
        with self.lock:
            self.pending[approval.id] = approval
        self.emit("approval_requested", {"id": approval.id, "name": name, "input": arguments})
        deadline = time.monotonic() + self.timeout
        while not approval.ready.wait(0.1):
            if self.cancelled.is_set() or time.monotonic() >= deadline:
                break
        allowed = approval.answer is True and not self.cancelled.is_set()
        with self.lock:
            self.pending.pop(approval.id, None)
        self.emit("approval_resolved", {"id": approval.id, "allow": allowed})
        return allowed

    def answer(self, approval_id, allow):
        with self.lock:
            approval = self.pending.get(approval_id)
            if approval is None or approval.ready.is_set():
                raise KeyError(approval_id)
            approval.answer = allow
            approval.ready.set()

    def snapshot(self):
        with self.lock:
            return [
                {"id": entry.id, "name": entry.name, "input": entry.arguments}
                for entry in self.pending.values()
            ]

    def cancel_all(self):
        with self.lock:
            for approval in self.pending.values():
                approval.answer = False
                approval.ready.set()
