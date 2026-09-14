"""Product clock rules, specified independently of a cron parser."""

import datetime as dt
from importlib.metadata import version

import pytest

from omakron.schedule import Schedule, ScheduleError, due_window


def instant(value):
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_parser_version_is_pinned():
    assert version("croniter") == "6.2.4"


@pytest.mark.parametrize(
    "expression",
    [
        "* * * * * *",
        "@daily",
        "0 25 * * *",
        "0 0 31 2 *",
        "0 0 L * *",
        "0 0 * * 1#2",
        "H * * * *",
        "*/0 * * * *",
        "61 * * * *",
        "0 0 * * ?",
    ],
)
def test_reject_unsupported_or_impossible_cron(expression):
    with pytest.raises(ScheduleError):
        Schedule(expression, "America/New_York")


def test_timezone_is_explicit_and_valid():
    for zone in ("", "Local", "Mars/Olympus", None):
        with pytest.raises(ScheduleError, match="timezone"):
            Schedule("0 9 * * *", zone)


def test_weekday_preview_has_five_exact_instants():
    s = Schedule("0 9 * * 1-5", "America/New_York")
    assert s.preview(instant("2026-09-11T13:00:00Z")) == [
        instant(f"2026-09-{day}T13:00:00Z") for day in (14, 15, 16, 17, 18)
    ]


def test_spring_gap_is_skipped_instead_of_shifted():
    s = Schedule("30 2 * * *", "America/New_York")
    assert s.preview(instant("2026-03-07T08:00:00Z"), 2) == [
        instant("2026-03-09T06:30:00Z"),
        instant("2026-03-10T06:30:00Z"),
    ]
    assert not s.matches(instant("2026-03-08T07:30:00Z"))


def test_autumn_minute_chooses_first_occurrence_only():
    s = Schedule("30 1 * * *", "America/New_York")
    first, repeated = instant("2026-11-01T05:30:00Z"), instant("2026-11-01T06:30:00Z")
    assert s.preview(instant("2026-11-01T04:00:00Z"), 2)[0] == first
    assert s.matches(first) and not s.matches(repeated)
    assert s.preview(first, 1) == [instant("2026-11-02T06:30:00Z")]
    assert s.preview(instant("2026-11-01T06:00:00Z"), 1) == [instant("2026-11-02T06:30:00Z")]


def test_half_hour_dst_gap_and_repeat():
    s = Schedule("45 1 * * *", "Australia/Lord_Howe")
    assert s.matches(instant("2026-04-04T14:45:00Z"))
    assert not s.matches(instant("2026-04-04T15:15:00Z"))
    spring = Schedule("15 2 * * *", "Australia/Lord_Howe")
    assert spring.preview(instant("2026-10-03T15:00:00Z"), 1) == [instant("2026-10-04T15:15:00Z")]


def test_day_of_month_and_weekday_use_or():
    s = Schedule("0 9 1 * 1", "UTC")
    assert s.preview(instant("2026-08-30T00:00:00Z"), 3) == [
        instant("2026-08-31T09:00:00Z"),
        instant("2026-09-01T09:00:00Z"),
        instant("2026-09-07T09:00:00Z"),
    ]


def test_leap_day_preview_is_bounded_but_not_limited_to_one_year():
    s = Schedule("0 0 29 2 *", "UTC")
    assert [v.year for v in s.preview(instant("2026-01-01T00:00:00Z"))] == [
        2028,
        2032,
        2036,
        2040,
        2044,
    ]


@pytest.mark.parametrize("delay,expected", [(59.999, True), (60, True), (60.001, False)])
def test_startup_grace_boundary(delay, expected):
    s = Schedule("0 9 * * *", "UTC")
    scheduled = instant("2026-09-12T09:00:00Z")
    result = due_window(
        s, scheduled - dt.timedelta(days=1), scheduled + dt.timedelta(seconds=delay)
    )
    assert bool(result.due) is expected
    if expected:
        assert result.due == scheduled


def test_forward_jump_skips_backlog_and_backward_jump_does_not_replay():
    s = Schedule("* * * * *", "UTC")
    checkpoint = instant("2026-09-12T09:00:00Z")
    now = instant("2026-09-13T09:00:05Z")
    result = due_window(s, checkpoint, now)
    assert result.due == instant("2026-09-13T09:00:00Z")
    assert result.gap is not None
    assert due_window(s, now, checkpoint).due is None
    assert due_window(s, now, now).due is None


def test_dispatch_and_preview_agree_across_dst():
    s = Schedule("30 1 * * *", "America/New_York")
    start = instant("2026-10-30T00:00:00Z")
    expected = s.preview(start)
    checkpoint = start
    actual = []
    while checkpoint < expected[-1]:
        now = checkpoint + dt.timedelta(minutes=1)
        result = due_window(s, checkpoint, now)
        if result.due:
            actual.append(result.due)
        checkpoint = now
    assert actual == expected
