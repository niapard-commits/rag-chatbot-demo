"""Google Calendar adapter (a service account that has been given access to one calendar).

What it does for the booking:
  * before a time is offered or booked, the busy times of the calendar are read (Free/Busy API), so an event that the
    owner has put into the calendar by hand blocks that slot for the chatbot too;
  * after a booking is saved, an event is created in the calendar (in the background, with retries).
The local SQLite calendar stays the source of truth for double-booking (UNIQUE day+slot); Google is added on top.
If Google is unreachable the chat keeps working with SQLite only and a warning is logged.

Setup (see README): create a service account in Google Cloud, enable the Google Calendar API, download the JSON key,
and share the calendar with the service account's e-mail address with the permission "Make changes to events"."""
import logging
import threading
from datetime import date, datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from .http_json import request_json

log = logging.getLogger("chatbot.gcal")
SCOPE = "https://www.googleapis.com/auth/calendar"


def service_account_token_provider(key_file: str) -> Callable[[], str]:
    """Returns a function that gives a valid access token (refreshed when it expires)."""
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account
    creds = service_account.Credentials.from_service_account_file(key_file, scopes=[SCOPE])
    lock = threading.Lock()

    def token() -> str:
        with lock:
            if not creds.valid:
                creds.refresh(Request())
            return creds.token
    return token


class GoogleCalendarClient:
    def __init__(self, calendar_id: str, token_provider: Callable[[], str], timezone: str = "Europe/Bratislava",
                 base_url: str = "https://www.googleapis.com/calendar/v3", timeout: float = 6.0,
                 retry_delays: tuple = (1.0, 3.0)):
        self.calendar_id, self.token_provider = calendar_id, token_provider
        self.tz, self.base_url = ZoneInfo(timezone), base_url.rstrip("/")
        self.timeout, self.retry_delays = timeout, retry_delays

    def _call(self, method: str, path: str, payload: dict | None = None, retry_delays: tuple | None = None):
        headers = {"Authorization": f"Bearer {self.token_provider()}"}
        return request_json(method, self.base_url + path, headers, payload, self.timeout,
                            self.retry_delays if retry_delays is None else retry_delays)

    def busy(self, day: date) -> list[tuple[datetime, datetime]]:
        """Busy intervals of the calendar on this day, as naive local datetimes."""
        start = datetime(day.year, day.month, day.day, tzinfo=self.tz)
        end = start + timedelta(days=1)
        body = {"timeMin": start.isoformat(), "timeMax": end.isoformat(), "timeZone": str(self.tz),
                "items": [{"id": self.calendar_id}]}
        # One quick retry only: this runs while a visitor waits for the answer.
        answer = self._call("POST", "/freeBusy", body, retry_delays=(0.5,))
        info = (answer.get("calendars") or {}).get(self.calendar_id) or {}
        if info.get("errors"):      # for example "notFound": the calendar was not shared with the service account
            raise RuntimeError(f"Google Calendar {self.calendar_id}: {info['errors']}")
        result = []
        for item in info.get("busy", []):
            s = datetime.fromisoformat(item["start"]).astimezone(self.tz).replace(tzinfo=None)
            e = datetime.fromisoformat(item["end"]).astimezone(self.tz).replace(tzinfo=None)
            result.append((s, e))
        return result

    def create_event(self, summary: str, description: str, start: datetime, end: datetime,
                     session_id: str = "") -> str:
        body = {"summary": summary, "description": description,
                "start": {"dateTime": start.isoformat(), "timeZone": str(self.tz)},
                "end": {"dateTime": end.isoformat(), "timeZone": str(self.tz)},
                "extendedProperties": {"private": {"chatbot_session": session_id}}}
        answer = self._call("POST", f"/calendars/{_quote(self.calendar_id)}/events", body)
        return answer["id"]

    def delete_event(self, event_id: str) -> None:
        self._call("DELETE", f"/calendars/{_quote(self.calendar_id)}/events/{event_id}")


def _quote(value: str) -> str:
    from urllib.parse import quote
    return quote(value, safe="")
