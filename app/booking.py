"""Appointment booking: weekday opening hours, fixed slots, no double booking."""
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from .db import connect


class BookingError(Exception):
    """Raised with a message that is safe to show to the visitor."""


_TIME_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")


def normalize_time(value: str) -> str:
    """'9:30' / '09:30' -> '09:30'. Raises ValueError for anything that is not a valid 24h time."""
    m = _TIME_RE.match(value or "")
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ValueError(f"bad time: {value!r}")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


@dataclass
class Booking:
    id: int
    day: str
    slot: str
    service: str


class Calendar:
    def __init__(self, db_path: str, open_hour: int = 9, close_hour: int = 17, slot_minutes: int = 30,
                 max_days: int = 180):
        self.db_path, self.max_days = db_path, max_days
        self.open_hour, self.close_hour, self.slot_minutes = open_hour, close_hour, slot_minutes

    def all_slots(self) -> list[str]:
        slots, t = [], datetime.combine(date.today(), time(self.open_hour))
        end = datetime.combine(date.today(), time(self.close_hour))
        while t + timedelta(minutes=self.slot_minutes) <= end:
            slots.append(t.strftime("%H:%M"))
            t += timedelta(minutes=self.slot_minutes)
        return slots

    def taken(self, day: str) -> set[str]:
        with connect(self.db_path) as conn:
            rows = conn.execute("SELECT slot FROM appointments WHERE day = ?", (day,)).fetchall()
        return {r["slot"] for r in rows}

    def free_slots(self, day: str, now: datetime | None = None) -> list[str]:
        d = self._parse_day(day)
        if d.weekday() >= 5:
            return []
        now = now or datetime.now()
        busy = self.taken(day)
        free = []
        for s in self.all_slots():
            if s in busy:
                continue
            if datetime.combine(d, time.fromisoformat(s)) <= now:
                continue
            free.append(s)
        return free

    def book(self, *, session_id: str, name: str, contact: str, service: str, day: str, slot: str,
             now: datetime | None = None) -> Booking:
        d = self._parse_day(day)
        slot = self._normalize_slot(slot)
        if d.weekday() >= 5:
            raise BookingError("We are closed on weekends. Please choose a weekday (Monday to Friday).")
        if slot not in self.all_slots():
            raise BookingError(
                f"We take appointments between {self.open_hour:02d}:00 and {self.close_hour:02d}:00, "
                f"every {self.slot_minutes} minutes (for example 09:00 or 09:30).")
        if datetime.combine(d, time.fromisoformat(slot)) <= (now or datetime.now()):
            raise BookingError("That time is already in the past. Please choose a later date or time.")
        if d > (now or datetime.now()).date() + timedelta(days=self.max_days):
            raise BookingError(f"We can book up to {self.max_days} days ahead. Please choose an earlier date.")
        try:
            with connect(self.db_path) as conn:
                cur = conn.execute(
                    "INSERT INTO appointments (session_id, name, contact, service, day, slot) VALUES (?,?,?,?,?,?)",
                    (session_id, name, contact, service, day, slot))
                return Booking(cur.lastrowid, day, slot, service)
        except sqlite3.IntegrityError:
            free = self.free_slots(day, now)
            hint = f" Free times that day: {', '.join(free[:6])}." if free else " That day has no free times."
            raise BookingError(f"Sorry, {slot} on {day} is already taken.{hint}") from None

    def _normalize_slot(self, slot: str) -> str:
        try:
            return normalize_time(slot)
        except ValueError:
            raise BookingError("I did not understand the time. Please write it like 14:30.") from None

    @staticmethod
    def _parse_day(day: str) -> date:
        try:
            return date.fromisoformat(day)
        except (TypeError, ValueError):
            raise BookingError("I did not understand the date. Please write it like 2025-01-31.") from None
