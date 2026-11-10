"""Calendar boundaries must agree with the scheduler's actual matching rules."""

import threading
from datetime import datetime

import pytest

from agent_harness.services.scheduler import Scheduler


@pytest.mark.parametrize(
    ("expression", "now", "expected", "last_fired"),
    [
        (
            "* * * * *",
            datetime(2026, 11, 9, 9, 0, 45),
            datetime(2026, 11, 9, 9, 0, 45),
            None,
        ),
        (
            "* * * * *",
            datetime(2026, 11, 9, 9, 0, 45),
            datetime(2026, 11, 9, 9, 1),
            "2026-11-09 09:00",
        ),
        (
            "0 9 * * *",
            datetime(2026, 11, 9, 9, 1),
            datetime(2026, 11, 10, 9),
            None,
        ),
        (
            "*/15 8-10 * * 1-5",
            datetime(2026, 11, 7, 10, 59),
            datetime(2026, 11, 9, 8),
            None,
        ),
        (
            "0 0 31 * *",
            datetime(2026, 4, 1),
            datetime(2026, 5, 31),
            None,
        ),
        (
            "0 0 29 2 *",
            datetime(2096, 3, 1),
            datetime(2104, 2, 29),
            None,
        ),
        # A restricted day-of-month and day-of-week use OR, as execution does.
        (
            "0 9 13 * 1",
            datetime(2026, 11, 10),
            datetime(2026, 11, 13, 9),
            None,
        ),
        (
            "0 9 13 * 1",
            datetime(2026, 11, 14),
            datetime(2026, 11, 16, 9),
            None,
        ),
    ],
)
def test_next_occurrence_obeys_calendar_and_minute_deduplication(
    tmp_path, expression, now, expected, last_fired
):
    scheduler = Scheduler(tmp_path, threading.Event())
    assert scheduler.validate_cron(expression) is None
    occurrence = scheduler.next_cron_run(expression, now, last_fired)
    assert occurrence == expected
    assert scheduler.cron_matches(expression, occurrence)


def test_syntactically_valid_impossible_date_has_no_next_occurrence(tmp_path):
    scheduler = Scheduler(tmp_path, threading.Event())
    expression = "0 0 31 2 *"
    assert scheduler.validate_cron(expression) is None
    assert scheduler.next_cron_run(expression, datetime(2026, 11, 9)) is None

