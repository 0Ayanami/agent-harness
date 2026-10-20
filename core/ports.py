"""The engine depends on contracts, not concrete transports or service implementations."""

from typing import Any, Callable, Protocol

from .models import RecoveryState

ToolPool = Callable[[], tuple[list[dict], dict]]
SystemPrompt = Callable[[list], str]


class ModelPort(Protocol):
    def complete(self, *, state: RecoveryState | None = None, **arguments) -> Any: ...
    def is_prompt_too_long_error(self, error: Exception) -> bool: ...


class ContextPort(Protocol):
    def prepare_context(self, messages: list, active_request: str) -> list: ...
    def compact_history(self, messages: list, active_request: str) -> list: ...
    def reactive_compact(self, messages: list, active_request: str) -> list: ...


class DispatcherPort(Protocol):
    def execute(self, call: dict, handlers: dict, *, allow_background: bool = True) -> str: ...
