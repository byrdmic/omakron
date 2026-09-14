"""Durable scheduling decisions, serialized with edits and queue claims."""

from __future__ import annotations

import datetime as dt
import json

from omakron.schedule import STARTUP_GRACE_S, Schedule, due_window, utc
from omakron.store import Store, parse_utc


def stamp(value: dt.datetime) -> str:
    return utc(value).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Scheduler:
    def __init__(self, store: Store, deadline_s: float):
        self.store = store
        self.deadline_s = deadline_s

    @property
    def enabled(self) -> bool:
        row = self.store.conn.execute(
            "SELECT value FROM schema_meta WHERE key='dispatch_enabled'"
        ).fetchone()
        return row is None or row["value"] == "true"

    def set_dispatch(self, enabled: bool, now: dt.datetime) -> None:
        with self.store._tx():
            self.store.conn.execute(
                "INSERT INTO schema_meta(key,value) VALUES('dispatch_enabled',?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps(enabled),),
            )
            for routine in self.store.list_routines():
                self.store.skip_scheduled_queue(routine.id, "dispatch disabled before start")
                self._checkpoint(routine.id, now)

    def _checkpoint(self, routine_id: str, now: dt.datetime) -> None:
        self.store.conn.execute(
            "INSERT INTO schedule_state(routine_id,checkpoint) VALUES(?,?)"
            " ON CONFLICT(routine_id) DO UPDATE SET checkpoint=MAX(checkpoint,excluded.checkpoint)",
            (routine_id, stamp(now)),
        )

    def tick(self, now: dt.datetime) -> list[str]:
        now = utc(now)
        made = []
        with self.store._tx():
            self.store.expire_queue(stamp(now))
            enabled = self.enabled
            for routine in self.store.list_routines():
                if routine.schedule_kind == "manual":
                    continue
                row = self.store.conn.execute(
                    "SELECT checkpoint FROM schedule_state WHERE routine_id=?", (routine.id,)
                ).fetchone()
                checkpoint = (
                    parse_utc(row["checkpoint"])
                    if row
                    else now - dt.timedelta(seconds=STARTUP_GRACE_S, microseconds=1)
                )
                if enabled and routine.enabled:
                    window = due_window(Schedule(routine.cron, routine.timezone), checkpoint, now)
                    if window.gap:
                        self.store.conn.execute(
                            "INSERT INTO schedule_gaps(routine_id,start_at,end_at,reason)"
                            " VALUES(?,?,?,?)",
                            (
                                routine.id,
                                stamp(window.gap[0]),
                                stamp(window.gap[1]),
                                "service did not observe this interval; missed occurrences skipped",
                            ),
                        )
                    if window.due is not None:
                        run_id = self._enqueue(routine, window.due, now)
                        if run_id:
                            made.append(run_id)
                self._checkpoint(routine.id, now)
        return made

    def _enqueue(self, routine, due: dt.datetime, now: dt.datetime) -> str | None:
        schedule = Schedule(routine.cron, routine.timezone)
        slot = routine.timezone + "|" + due.astimezone(schedule.zone).strftime("%Y-%m-%dT%H:%M")
        existing = self.store.conn.execute(
            "SELECT 1 FROM schedule_slots WHERE routine_id=? AND local_slot=?", (routine.id, slot)
        ).fetchone()
        if existing:
            return None
        overlap = self.store.conn.execute(
            "SELECT 1 FROM runs WHERE routine_id=? AND status IN ('queued','claimed','running')",
            (routine.id,),
        ).fetchone()
        run, _ = self.store.enqueue_run(
            routine,
            trigger="scheduled",
            parameter=None,
            idempotency_key=f"scheduled:{routine.id}:{slot}",
            deadline_s=self.deadline_s,
            enqueued_at=stamp(now),
        )
        self.store.conn.execute(
            "UPDATE runs SET scheduled_at=?, local_slot=? WHERE id=?", (stamp(due), slot, run.id)
        )
        self.store.conn.execute(
            "INSERT INTO schedule_slots(routine_id,local_slot,revision,run_id) VALUES(?,?,?,?)",
            (routine.id, slot, routine.revision, run.id),
        )
        if overlap:
            self.store.finish_run(run.id, status="skipped", problems=["same-routine overlap"])
        return run.id
