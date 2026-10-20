"""Event port shared by CLI, browser and tests."""

from typing import Callable

EventSink = Callable[[str, dict], None]


def discard_event(kind: str, payload: dict) -> None:
    pass
