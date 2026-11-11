"""Appointment booking: weekday opening hours, fixed slots, no double booking.
SQLite is the source of truth. If a Google Calendar client is given (app/gcal.py), times that are busy in that calendar
are not offered, and every booking is also written there as an event."""
import logging
import re
import sqlite3
import time as _time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from .db import connect

log = logging.getLogger("chatbot.booking")


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
                 max_days: int = 180, google=None, business_name: str = ""):
        self.db_path, self.max_days = db_path, max_days
        self.open_hour, self.close_hour, self.slot_minutes = open_hour, close_hour, slot_minutes
        self.google, self.business_name = google, business_name
        self.google_degraded = False          # True while Google could not be reached (SQLite only)
        self._busy_cache: dict[str, tuple[float, set]] = {}
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="gcal-event")
        self._pending = set()
        self.event_retry_delays = (1.0, 3.0)
        self.busy_cache_seconds = 20.0

    def flush(self, timeout: float = 15.0) -> None:
        """Wait until queued Google events are created (used by tests and on shutdown)."""
        wait(list(self._pending), timeout=timeout)

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
        return {r["slot"] for r in rows} | self._google_busy_slots(day)

    def _google_busy_slots(self, day: str, fresh: bool = False) -> set[str]:
        """Slots that overlap an event in the Google calendar. Empty when Google is off or unreachable."""
        if not self.google:
            return set()
        cached = self._busy_cache.get(day)
        if cached and not fresh and _time.monotonic() - cached[0] < self.busy_cache_seconds:
            return cached[1]
        try:
            d = self._parse_day(day)
            intervals = self.google.busy(d)
        except Exception as exc:
            if not self.google_degraded:
                log.warning("Google Calendar is not reachable, using the local calendar only: %s", exc)
            self.google_degraded = True
            return set()
        self.google_degraded = False
        busy = set()
        for slot in self.all_slots():
            start = datetime.combine(d, time.fromisoformat(slot))
            end = start + timedelta(minutes=self.slot_minutes)
            if any(b_start < end and b_end > start for b_start, b_end in intervals):
                busy.add(slot)
        self._busy_cache[day] = (_time.monotonic(), busy)
        return busy

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
        if slot in self._google_busy_slots(day, fresh=True):    # an event the owner added to the calendar by hand
            self._raise_taken(day, slot, now)
        try:
            with connect(self.db_path) as conn:
                cur = conn.execute(
                    "INSERT INTO appointments (session_id, name, contact, service, day, slot) VALUES (?,?,?,?,?,?)",
                    (session_id, name, contact, service, day, slot))
                booking = Booking(cur.lastrowid, day, slot, service)
        except sqlite3.IntegrityError:
            self._raise_taken(day, slot, now)
        self._busy_cache.pop(day, None)
        self._queue_event(booking, session_id, name, contact)
        return booking

    def _raise_taken(self, day: str, slot: str, now: datetime | None):
        free = self.free_slots(day, now)
        hint = f" Free times that day: {', '.join(free[:6])}." if free else " That day has no free times."
        raise BookingError(f"Sorry, {slot} on {day} is already taken.{hint}") from None

    # ------------------------------------------------------------ Google Calendar event
    def _queue_event(self, booking: Booking, session_id: str, name: str, contact: str) -> None:
        if not self.google:
            return
        future = self._pool.submit(self._create_event, booking, session_id, name, contact)
        self._pending.add(future)
        future.add_done_callback(self._pending.discard)

    def _create_event(self, booking: Booking, session_id: str, name: str, contact: str) -> None:
        start = datetime.combine(self._parse_day(booking.day), time.fromisoformat(booking.slot))
        end = start + timedelta(minutes=self.slot_minutes)
        summary = f"{booking.service}: {name}"
        description = f"Booked by the chatbot{' of ' + self.business_name if self.business_name else ''}.\n" \
                      f"Visitor: {name}\nContact: {contact}\nService: {booking.service}"
        error: Exception | None = None
        for attempt in range(len(self.event_retry_delays) + 1):
            try:
                event_id = self.google.create_event(summary, description, start, end, session_id)
                with connect(self.db_path) as conn:
                    conn.execute("UPDATE appointments SET google_event_id = ? WHERE id = ?", (event_id, booking.id))
                return
            except Exception as exc:
                error = exc
                if attempt < len(self.event_retry_delays):
                    _time.sleep(self.event_retry_delays[attempt])
        log.error("Booking %s %s %s is saved locally but the Google Calendar event could not be created, "
                  "add it by hand: %s", booking.id, booking.day, booking.slot, error)

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
