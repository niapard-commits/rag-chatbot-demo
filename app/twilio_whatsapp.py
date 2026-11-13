"""WhatsApp through Twilio (the sandbox for testing, a registered sender for production).

Same idea as app/whatsapp.py, different provider: Twilio calls our webhook when a visitor writes to the Twilio
WhatsApp number, we pass the text to the same chatbot that serves the website and send the answer back through
Twilio's REST API. Knowledge base, booking, CRM and limits are shared with every other channel.

  POST /twilio/whatsapp   incoming messages, form-encoded, signed with the account's auth token (X-Twilio-Signature)

The answer goes back in the webhook response itself (TwiML), which also works on a Twilio trial account: a trial
account may not send free text through the Messages API. Twilio waits about 15 seconds for the response; if the
model needs longer, the call is acknowledged with an empty response and the reply is sent afterwards with the
Messages API (that works on an upgraded account).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from collections import deque
from urllib.parse import parse_qsl
from xml.sax.saxutils import escape

import requests

log = logging.getLogger("twilio")

TEXT_ONLY = "I can only read text messages. Please write your question."
ERROR_REPLY = "Sorry, something went wrong on my side. Please try again in a moment."
MAX_CHARS = 1600   # Twilio rejects a WhatsApp body longer than 1600 characters
EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'
API_BASE = "https://api.twilio.com/2010-04-01"


def twiml_reply(text: str) -> str:
    """The webhook response that makes Twilio send `text` back to the visitor."""
    return f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{escape(text[:MAX_CHARS])}</Message></Response>'


def session_id_for(sender: str) -> str:
    """'whatsapp:+421900111222' -> 'tw421900111222'. One WhatsApp number = one chatbot session."""
    number = str(sender).removeprefix("whatsapp:")
    return "tw" + "".join(ch for ch in number if ch.isalnum() and ch.isascii())[:40]


def parse_form(raw: bytes) -> dict[str, str]:
    """Twilio posts application/x-www-form-urlencoded; the signature needs every field, so none is dropped."""
    return dict(parse_qsl(raw.decode("utf-8", "replace"), keep_blank_values=True))


def signature_ok(auth_token: str, url: str, params: dict[str, str], header: str) -> bool:
    """X-Twilio-Signature = base64(HMAC-SHA1(auth token, url + every field name and value, fields sorted by name))."""
    data = url + "".join(k + params[k] for k in sorted(params))
    expected = base64.b64encode(hmac.new(auth_token.encode(), data.encode(), hashlib.sha1).digest()).decode()
    return hmac.compare_digest(expected, header or "")


def extract_message(params: dict[str, str]) -> dict | None:
    """The message in one webhook call: {id, from, text}; text is None for photos, voice notes and so on."""
    sender = params.get("From", "")
    if not sender.startswith("whatsapp:"):
        return None
    text = (params.get("Body") or "").strip()
    return {"id": params.get("MessageSid") or params.get("SmsMessageSid"), "from": sender, "text": text or None}


class TwilioChannel:
    def __init__(self, account_sid: str, auth_token: str, from_number: str):
        self.account_sid, self.auth_token = account_sid, auth_token
        self.from_number = from_number if from_number.startswith("whatsapp:") else f"whatsapp:{from_number}"
        self._seen: deque = deque(maxlen=500)   # Twilio repeats a call when we are slow: answer each message once

    def is_new(self, message_id: str | None) -> bool:
        if not message_id:
            return True
        if message_id in self._seen:
            return False
        self._seen.append(message_id)
        return True

    def send(self, to: str, text: str) -> None:
        try:
            r = requests.post(f"{API_BASE}/Accounts/{self.account_sid}/Messages.json",
                              auth=(self.account_sid, self.auth_token),
                              data={"From": self.from_number, "To": to, "Body": text[:MAX_CHARS]}, timeout=15)
            if r.status_code >= 400:   # never log the request: it carries the credentials
                log.warning("Twilio refused the message (HTTP %s): %s", r.status_code, r.text[:300])
        except requests.RequestException as exc:
            log.warning("Twilio is not reachable (%s)", type(exc).__name__)
