"""CRM layer. Leads are always saved in the local SQLite database.
If CRM_WEBHOOK_URL is set, every lead and appointment is also POSTed there (n8n, Make, Zapier, HubSpot forms, your API).
If a HubSpot token is configured, every lead is also written to HubSpot as a contact (see app/hubspot.py).

The webhook is sent in the background with up to 3 attempts, so a slow or broken CRM never delays the visitor's reply
(a hanging webhook used to make the chat wait for the full timeout). A permanent failure is logged and the lead stays in
SQLite. Every event carries the session_id: use it in the CRM to update one contact instead of creating duplicates."""
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, wait

from .db import connect
from .hubspot import HubSpot

log = logging.getLogger("chatbot.crm")


class CRM:
    def __init__(self, db_path: str, webhook_url: str = "", timeout: float = 5.0, webhook_secret: str = "",
                 hubspot: HubSpot | None = None):
        self.db_path, self.webhook_url, self.timeout = db_path, webhook_url, timeout
        self.webhook_secret, self.hubspot = webhook_secret, hubspot
        self._hubspot_lock = threading.Lock()   # one HubSpot write at a time: no duplicate contacts for one chat
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="crm-webhook")
        self._pending = set()
        self.retry_delays = (1.0, 3.0)     # pauses between the 3 attempts

    def save_lead(self, session_id: str, name: str, email: str | None, phone: str | None,
                  service: str | None, note: str | None = None, score: str | None = None,
                  score_reason: str | None = None) -> bool:
        """Insert or update the lead of this chat session. Returns True if it is a new lead.
        An update never erases a value that is already stored (for example the service, which the chat forgets
        after a booking): a missing value in the new data keeps the old one."""
        with connect(self.db_path) as conn:
            exists = conn.execute("SELECT 1 FROM leads WHERE session_id = ?", (session_id,)).fetchone()
            conn.execute(
                """INSERT INTO leads (session_id, name, email, phone, service, note, score, score_reason)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(session_id) DO UPDATE SET name=excluded.name,
                   email=COALESCE(excluded.email, email), phone=COALESCE(excluded.phone, phone),
                   service=COALESCE(excluded.service, service), note=COALESCE(excluded.note, note),
                   score=COALESCE(excluded.score, score), score_reason=COALESCE(excluded.score_reason, score_reason)""",
                (session_id, name, email, phone, service, note, score, score_reason))
            row = dict(conn.execute("SELECT * FROM leads WHERE session_id = ?", (session_id,)).fetchone())
        self._send_webhook({"event": "lead", "session_id": session_id, "name": row["name"], "email": row["email"],
                            "phone": row["phone"], "service": row["service"], "note": row["note"],
                            "score": row["score"], "score_reason": row["score_reason"]})
        self._sync_hubspot(session_id)
        return not exists

    def notify_booking(self, session_id: str, name: str, contact: str, service: str, day: str, slot: str) -> None:
        self._send_webhook({"event": "appointment", "session_id": session_id, "name": name,
                            "contact": contact, "service": service, "day": day, "slot": slot})
        self._sync_hubspot(session_id, appointment=f"{service}, {day} {slot}",
                           fallback={"name": name, "email": contact if "@" in contact else None,
                                     "phone": None if "@" in contact else contact, "service": service})

    def list_leads(self) -> list[dict]:
        with connect(self.db_path) as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM leads ORDER BY id DESC LIMIT 200")]

    def _send_webhook(self, payload: dict) -> None:
        if not self.webhook_url:
            return
        future = self._pool.submit(self._deliver, payload)
        self._pending.add(future)
        future.add_done_callback(self._pending.discard)

    def _sync_hubspot(self, session_id: str, appointment: str | None = None, fallback: dict | None = None) -> None:
        """Queue a write of this chat's lead to HubSpot. The data is read from SQLite when the job runs, so a quick
        series of updates always sends the newest values."""
        if not self.hubspot:
            return
        future = self._pool.submit(self._hubspot_job, session_id, appointment, fallback)
        self._pending.add(future)
        future.add_done_callback(self._pending.discard)

    def _hubspot_job(self, session_id: str, appointment: str | None, fallback: dict | None) -> None:
        try:
            with self._hubspot_lock:
                with connect(self.db_path) as conn:
                    row = conn.execute("SELECT * FROM leads WHERE session_id = ?", (session_id,)).fetchone()
                lead = dict(row) if row else dict(fallback or {})
                if not lead.get("name"):
                    return
                if appointment:
                    lead["appointment"] = appointment
                contact_id = self.hubspot.sync_lead(lead, lead.get("hubspot_id") if row else None)
                if row and contact_id and contact_id != row["hubspot_id"]:
                    with connect(self.db_path) as conn:
                        conn.execute("UPDATE leads SET hubspot_id = ? WHERE session_id = ?", (contact_id, session_id))
        except Exception as exc:    # the lead is safe in SQLite; a CRM problem must never break the chat
            log.warning("HubSpot sync failed, the lead stays in SQLite only: %s", exc)

    def flush(self, timeout: float = 15.0) -> None:
        """Wait until queued webhooks are delivered (used by tests and on shutdown)."""
        wait(list(self._pending), timeout=timeout)

    def _deliver(self, payload: dict) -> None:
        headers = {"Content-Type": "application/json"}
        if self.webhook_secret:  # lets the receiver (n8n) refuse requests that do not come from this bot
            headers["X-Webhook-Secret"] = self.webhook_secret
        data = json.dumps(payload).encode("utf-8")
        for attempt in range(len(self.retry_delays) + 1):
            try:
                req = urllib.request.Request(self.webhook_url, data=data, headers=headers, method="POST")
                urllib.request.urlopen(req, timeout=self.timeout).close()
                return
            except urllib.error.HTTPError as exc:
                if 400 <= exc.code < 500 and exc.code != 429:   # our request is wrong: retrying will not help
                    log.warning("CRM webhook rejected the %s event (HTTP %s); not retrying", payload.get("event"), exc.code)
                    return
                error = exc
            except Exception as exc:
                error = exc
            if attempt < len(self.retry_delays):
                time.sleep(self.retry_delays[attempt])
        log.warning("CRM webhook failed after %d attempts, the %s stays in SQLite only: %s",
                    len(self.retry_delays) + 1, payload.get("event"), error)
