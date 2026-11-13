import requests

from app import telegram_bot as tb


def run(update, answer="ok"):
    sent, asked = [], []

    def ask(chat_id, text):
        asked.append((chat_id, text))
        return answer

    tb.handle_update(update, ask, lambda chat_id, text: sent.append((chat_id, text)))
    return sent, asked


def msg(text=None, chat_id=42):
    m = {"chat": {"id": chat_id}}
    if text is not None:
        m["text"] = text
    return {"update_id": 1, "message": m}


def test_text_goes_to_the_chatbot_and_the_reply_comes_back():
    sent, asked = run(msg("How much is whitening?"), answer="220 EUR")
    assert asked == [(42, "How much is whitening?")] and sent == [(42, "220 EUR")]


def test_start_shows_the_greeting_without_calling_the_chatbot():
    sent, asked = run(msg("/start"))
    assert asked == [] and "virtual assistant" in sent[0][1]
    sent, asked = run(msg("/start@radianrtest_bot"))
    assert asked == [] and sent[0][1] == tb.GREETING


def test_photos_and_stickers_get_a_polite_text_only_answer():
    sent, asked = run(msg(None))
    assert asked == [] and sent == [(42, tb.TEXT_ONLY)]


def test_updates_without_a_message_are_ignored():
    sent, asked = run({"update_id": 2, "edited_message": {"chat": {"id": 1}}})
    assert sent == [] and asked == []


def test_a_group_chat_id_makes_a_valid_session_id():
    assert tb.session_id_for(-1001234) == "tgm1001234" and tb.session_id_for(55) == "tg55"


class FakeResponse:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


def test_ask_chatbot_sends_the_session_and_reads_the_reply(monkeypatch):
    seen = {}

    def post(url, json, headers, timeout):
        seen.update(url=url, json=json, headers=headers)
        return FakeResponse(200, {"reply": "Hello"})

    monkeypatch.setattr(tb.requests, "post", post)
    assert tb.ask_chatbot("http://bot:8000/", 7, "hi") == "Hello"
    assert seen["url"] == "http://bot:8000/chat" and seen["json"] == {"session_id": "tg7", "message": "hi"}
    assert seen["headers"]["X-Forwarded-For"] == "telegram-7"


def test_ask_chatbot_shows_the_limit_message_and_survives_errors(monkeypatch):
    monkeypatch.setattr(tb.requests, "post", lambda *a, **k: FakeResponse(429, {"detail": "Please wait a moment."}))
    assert tb.ask_chatbot("http://x", 1, "hi") == "Please wait a moment."
    monkeypatch.setattr(tb.requests, "post", lambda *a, **k: FakeResponse(500, {}))
    assert tb.ask_chatbot("http://x", 1, "hi") == tb.ERROR_REPLY

    def boom(*a, **k):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(tb.requests, "post", boom)
    assert tb.ask_chatbot("http://x", 1, "hi") == tb.ERROR_REPLY


def test_the_token_is_required():
    import pytest
    with pytest.raises(SystemExit):
        tb.TelegramBot("", "http://x")
