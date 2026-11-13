"""WhatsApp channel (Meta WhatsApp Cloud API).

Meta calls our webhook when a visitor writes to the business number. We pass the text to the same chatbot that
serves the website and send the answer back through the Graph API. Everything else (knowledge base, booking, CRM,
rate limits) is shared. One WhatsApp number = one conversation.

Needs a public HTTPS address for the webhook (the demo server already has one behind Caddy):
  GET  /whatsapp/webhook   Meta checks the address once, with WHATSAPP_VERIFY_TOKEN
  POST /whatsapp/webhook   incoming messages, signed with the app secret (X-Hub-Signature-256)
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from collections import deque

import requests

log = logging.getLogger("whatsapp")

TEXT_ONLY = "I can only read text messages. Please write your question."
ERROR_REPLY = "Sorry, something went wrong on my side. Please try again in a moment."
MAX_CHARS = 4000   # WhatsApp text messages are limited to 4096 characters


def session_id_for(wa_id: str) -> str:
    """One WhatsApp number = one chatbot session (the id may only hold letters, digits, _ and -)."""
    return "wa" + "".join(ch for ch in str(wa_id) if ch.isalnum())[:40]


def signature_ok(app_secret: str, body: bytes, header: str) -> bool:
    """Meta signs every webhook call: X-Hub-Signature-256 = 'sha256=' + HMAC-SHA256(app secret, raw body)."""
    expected = "sha256=" + hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header or "")


def extract_messages(payload: dict) -> list[dict]:
    """The text messages in one webhook call: [{id, from, text}] (text is None for photos, voice notes and so on).
    Status updates (sent, delivered, read) carry no messages and are skipped."""
    found = []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            for msg in (change.get("value") or {}).get("messages") or []:
                text = (msg.get("text") or {}).get("body") if msg.get("type") == "text" else None
                found.append({"id": msg.get("id"), "from": msg.get("from"), "text": (text or "").strip() or None})
    return found


def failed_statuses(payload: dict) -> list[dict]:
    """Delivery reports Meta could not deliver: [{id, status, code, title}]. Without these a refused reply is invisible."""
    found = []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            for st in (change.get("value") or {}).get("statuses") or []:
                if st.get("status") == "failed":
                    err = (st.get("errors") or [{}])[0]
                    found.append({"id": st.get("id"), "status": "failed", "code": err.get("code"),
                                  "title": err.get("title") or err.get("message")})
    return found


class WhatsAppChannel:
    def __init__(self, access_token: str, phone_number_id: str, api_base: str = "https://graph.facebook.com/v21.0"):
        self.access_token, self.phone_number_id, self.api_base = access_token, phone_number_id, api_base.rstrip("/")
        self._seen: deque = deque(maxlen=500)   # Meta repeats a call when we are slow: answer each message once

    def is_new(self, message_id: str | None) -> bool:
        if not message_id:
            return True
        if message_id in self._seen:
            return False
        self._seen.append(message_id)
        return True

    def send(self, to: str, text: str) -> None:
        try:
            r = requests.post(f"{self.api_base}/{self.phone_number_id}/messages",
                              headers={"Authorization": f"Bearer {self.access_token}"},
                              json={"messaging_product": "whatsapp", "to": to, "type": "text",
                                    "text": {"body": text[:MAX_CHARS]}}, timeout=15)
            if r.status_code >= 400:   # never log the request: it carries the access token
                log.warning("WhatsApp refused the message (HTTP %s): %s", r.status_code, r.text[:200])
        except requests.RequestException as exc:
            log.warning("WhatsApp is not reachable (%s)", type(exc).__name__)
