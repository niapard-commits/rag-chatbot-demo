"""Telegram channel for the chatbot.

The bot reads messages from Telegram (long polling, so no public URL or webhook is needed) and passes each one to the
chatbot's own /chat endpoint. Everything else (knowledge base, booking, CRM, rate limits) is exactly the same as on
the website, because it is the same chatbot. One Telegram chat is one conversation.

Run:  TELEGRAM_BOT_TOKEN=... python -m app.telegram_bot     (CHATBOT_URL defaults to http://127.0.0.1:8000)
"""
from __future__ import annotations

import logging
import os
import time
from typing import Callable

import requests

log = logging.getLogger("telegram")

API = "https://api.telegram.org"
GREETING = ("Hello! I am the virtual assistant of Bright Smile Dental. I can answer questions about our services, "
            "prices and opening hours, and book an appointment for you. How can I help?")
TEXT_ONLY = "I can only read text messages. Please write your question."
ERROR_REPLY = "Sorry, something went wrong on my side. Please try again in a moment."
MAX_TELEGRAM_CHARS = 4000   # Telegram refuses messages above 4096 characters


def session_id_for(chat_id: int) -> str:
    """One Telegram chat = one chatbot session. The id must match [A-Za-z0-9_-] (a group id is negative)."""
    return "tg" + str(chat_id).replace("-", "m")


def ask_chatbot(base_url: str, chat_id: int, text: str, timeout: float = 60.0) -> str:
    """Send one message to POST /chat and return the text to show in Telegram."""
    try:
        r = requests.post(base_url.rstrip("/") + "/chat",
                          json={"session_id": session_id_for(chat_id), "message": text},
                          # The rate limit per visitor is per IP, so each Telegram chat gets its own "address".
                          headers={"X-Forwarded-For": f"telegram-{chat_id}"}, timeout=timeout)
    except requests.RequestException as exc:
        log.warning("The chatbot did not answer: %s", exc)
        return ERROR_REPLY
    if r.status_code == 200:
        return (r.json().get("reply") or ERROR_REPLY)[:MAX_TELEGRAM_CHARS]
    if r.status_code == 429:   # a limit: the chatbot's own message says what to do
        try:
            return str(r.json().get("detail") or ERROR_REPLY)
        except ValueError:
            return ERROR_REPLY
    log.warning("The chatbot answered with HTTP %s", r.status_code)
    return ERROR_REPLY


def handle_update(update: dict, ask: Callable[[int, str], str], send: Callable[[int, str], None],
                  typing: Callable[[int], None] = lambda chat_id: None) -> None:
    """Process one Telegram update. `ask` and `send` are passed in so this can be tested without a network."""
    message = update.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    if chat_id is None:
        return
    text = (message.get("text") or "").strip()
    if not text:
        send(chat_id, TEXT_ONLY)
        return
    if text.split()[0].split("@")[0] == "/start":
        send(chat_id, GREETING)
        return
    typing(chat_id)
    send(chat_id, ask(chat_id, text[:1000]))


class TelegramBot:
    def __init__(self, token: str, chatbot_url: str):
        if not token:
            raise SystemExit("TELEGRAM_BOT_TOKEN is not set")
        self.base, self.chatbot_url = f"{API}/bot{token}", chatbot_url

    def _call(self, method: str, http_timeout: float = 15.0, **params):
        r = requests.post(f"{self.base}/{method}", json=params, timeout=http_timeout)
        r.raise_for_status()
        return r.json().get("result")

    def send(self, chat_id: int, text: str) -> None:
        self._call("sendMessage", chat_id=chat_id, text=text)

    def typing(self, chat_id: int) -> None:
        try:
            self._call("sendChatAction", chat_id=chat_id, action="typing")
        except requests.RequestException:
            pass   # the "typing..." hint is optional

    def run(self) -> None:
        offset, failures = None, 0
        log.info("Telegram bot started, forwarding messages to %s", self.chatbot_url)
        while True:
            try:
                params = {"timeout": 30} if offset is None else {"timeout": 30, "offset": offset}   # long poll: wait up to 30 s
                updates = self._call("getUpdates", http_timeout=40.0, **params)
                failures = 0
            except requests.RequestException as exc:
                failures += 1
                wait = min(60, 2 ** min(failures, 6))
                # Never print the exception text as it is: it contains the request URL, and that URL holds the token.
                log.warning("Telegram is not reachable (%s), retrying in %ss", type(exc).__name__, wait)
                time.sleep(wait)
                continue
            for update in updates or []:
                offset = update["update_id"] + 1
                try:
                    handle_update(update, lambda cid, t: ask_chatbot(self.chatbot_url, cid, t), self.send, self.typing)
                except Exception as exc:   # one bad message must not stop the bot
                    log.error("Could not handle an update: %s", type(exc).__name__)


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    TelegramBot(os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("CHATBOT_URL", "http://127.0.0.1:8000")).run()


if __name__ == "__main__":
    main()
