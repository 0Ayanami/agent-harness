"""Small shared records for coordination; services own the algorithms."""

import threading
from dataclasses import dataclass, field

from .models import ProtocolState


@dataclass
class CoordinationState:
    active_teammates: dict[str, str] = field(default_factory=dict)
    plan_gates: dict[str, str] = field(default_factory=dict)
    plan_request_ids: dict[str, str] = field(default_factory=dict)
    pending_requests: dict[str, ProtocolState] = field(default_factory=dict)
    team_lock: object = field(default_factory=threading.RLock)


@dataclass
class BackgroundState:
    tasks: dict[str, dict] = field(default_factory=dict)
    results: dict[str, str] = field(default_factory=dict)
    lock: object = field(default_factory=threading.RLock)
