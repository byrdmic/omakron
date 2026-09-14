"""One evaluator for previews and dispatch, with explicit wall-clock semantics.

croniter 6.2.4 parses five numeric cron fields using POSIX day OR matching.
It iterates naive local candidates only. zoneinfo maps each candidate to its
first real instant, so nonexistent times and repeated second folds never run.
"""

from __future__ import annotations

import datetime as dt
import enum
import re
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import CroniterBadDateError, croniter

STARTUP_GRACE_S = 60
PARSER_VERSION = "6.2.4"
CONVENTION = "Day of month OR weekday when both are restricted; Sunday is 0 or 7."


class ScheduleKind(enum.StrEnum):
    MANUAL = "manual"
    CRON = "cron"


class ScheduleError(ValueError):
    pass


def utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        raise ScheduleError("time must include a timezone")
    return value.astimezone(dt.UTC)


class Schedule:
    def __init__(self, expression: str, timezone: str):
        if not isinstance(timezone, str) or not timezone:
            raise ScheduleError("an explicit IANA timezone is required")
        try:
            self.zone = ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ScheduleError(f"unknown timezone: {timezone}") from exc
        if not isinstance(expression, str) or len(expression) > 200:
            raise ScheduleError("cron must be a five-field expression")
        fields = expression.split()
        if len(fields) != 5:
            raise ScheduleError("cron requires five fields: minute hour day month weekday")
        for field in fields:
            if not re.fullmatch(r"[0-9*,/\-]+", field):
                raise ScheduleError("use numeric cron fields, *, lists, ranges, and steps only")
        self.expression = " ".join(fields)
        self.timezone = timezone
        if not croniter.is_valid(self.expression):
            raise ScheduleError("cron contains an invalid value, range, or step")
        # Prove at least one date exists. A fixed leap-cycle base keeps validation
        # independent of today's clock, and allows leap day schedules.
        try:
            self._iterator(dt.datetime(2024, 1, 1)).get_next(dt.datetime)
        except CroniterBadDateError as exc:
            raise ScheduleError("cron has no matching calendar date") from exc

    def _iterator(self, local: dt.datetime):
        return croniter(self.expression, local, day_or=True, max_years_between_matches=8)

    def _first_instant(self, local: dt.datetime) -> dt.datetime | None:
        value = local.replace(tzinfo=self.zone, fold=0).astimezone(dt.UTC)
        if value.astimezone(self.zone).replace(tzinfo=None) != local:
            return None
        return value

    def preview(self, after: dt.datetime, count: int = 5) -> list[dt.datetime]:
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 100:
            raise ScheduleError("preview count must be between 1 and 100")
        after = utc(after)
        iterator = self._iterator(after.astimezone(self.zone).replace(tzinfo=None))
        result = []
        # A timezone can skip an entire day. Bound work without truncating a
        # valid five-occurrence yearly or leap-day preview.
        for _ in range(10000):
            local = iterator.get_next(dt.datetime)
            candidate = self._first_instant(local)
            if candidate is not None and candidate > after:
                result.append(candidate)
                if len(result) == count:
                    return result
        raise ScheduleError("could not find the requested real occurrences")

    def matches(self, value: dt.datetime) -> bool:
        value = utc(value)
        if value.second or value.microsecond:
            return False
        local = value.astimezone(self.zone)
        if local.fold:
            return False
        return croniter.match(self.expression, local.replace(tzinfo=None), day_or=True)

    def latest(self, at: dt.datetime) -> dt.datetime | None:
        """The latest candidate, accepted only within the launch grace window."""
        at = utc(at)
        local = at.astimezone(self.zone).replace(tzinfo=None)
        candidate = self._iterator(local + dt.timedelta(microseconds=1)).get_prev(dt.datetime)
        return self._first_instant(candidate)


@dataclass(frozen=True)
class DueWindow:
    due: dt.datetime | None
    gap: tuple[dt.datetime, dt.datetime] | None


def due_window(schedule: Schedule, checkpoint: dt.datetime, now: dt.datetime) -> DueWindow:
    """At most one recent occurrence, plus a summarized unobserved interval.

    The persisted checkpoint only moves forward. Runs at exactly 60 seconds
    late are permitted, and anything older is skipped. A long outage never
    expands a backlog into model calls.
    """
    checkpoint, now = utc(checkpoint), utc(now)
    if now <= checkpoint:
        return DueWindow(None, None)
    oldest = now - dt.timedelta(seconds=STARTUP_GRACE_S)
    gap = (checkpoint, oldest) if oldest > checkpoint else None
    candidate = schedule.latest(now)
    if candidate is None or candidate <= checkpoint or candidate < oldest or candidate > now:
        return DueWindow(None, gap)
    return DueWindow(candidate, gap)
