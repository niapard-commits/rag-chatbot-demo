"""Regression tests for problems found by the red-team run (scripts/live_redteam.py)."""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.agent import clean_fields, clean_text
from app.booking import BookingError, Calendar
from app.main import create_app
from tests.conftest import NOW, TUESDAY


# ------------------------------------------------------------ blank messages must not crash the server
@pytest.mark.parametrize("text", ["   ", "\n", "\t \n"])
def test_blank_message_is_rejected_with_422_not_500(settings, text):
    client = TestClient(create_app(settings), raise_server_exceptions=False)
    assert client.post("/chat", json={"session_id": "a", "message": text}).status_code == 422


def test_odd_but_non_blank_messages_are_fine(settings):
    client = TestClient(create_app(settings), raise_server_exceptions=False)
    for text in ["😀", "\x00", "a" * 1000, "<script>alert(1)</script>", "'; DROP TABLE leads;--"]:
        assert client.post("/chat", json={"session_id": "a", "message": text}).status_code == 200


# ------------------------------------------------------------ text that reaches the CRM
def test_invisible_and_control_characters_are_removed():
    assert clean_text("Evil‮​ Name\x00", 60) == "Evil Name"
    assert clean_text("Line one\nline   two\ttab", 60) == "Line one line two tab"


@pytest.mark.parametrize("value", ["=HYPERLINK(\"http://evil\",\"x\")", "+cmd|' /C calc'!A0", "-2+3", "@SUM(1+1)", " = x"])
def test_spreadsheet_formulas_cannot_start_a_name_or_note(value):
    out = clean_fields({"name": "Anna " + value[:0] + "Smith", "note": value})
    assert out["name"] == "Anna Smith"
    assert not out.get("note", "x").startswith(("=", "+", "-", "@"))


def test_name_that_is_only_a_formula_is_dropped():
    assert "name" not in clean_fields({"name": "=1+1"})


def test_phone_must_look_like_a_phone_number():
    for bad in ["=1+1+1234567", "abc1234567890", "12345", "1" * 25, "+421 900 123 456; DROP"]:
        assert "phone" not in clean_fields({"phone": bad}), bad
    for good in ["+421 900 123 456", "0900 123 456", "(02) 1234 5678", "+44.20.7946.0958"]:
        assert clean_fields({"phone": good})["phone"] == good


def test_email_cannot_start_with_a_formula_character():
    assert "email" not in clean_fields({"email": "+x@example.com"})
    assert "email" not in clean_fields({"email": "-1@example.com"})
    assert clean_fields({"email": "a.b+tag@example.co.uk"})["email"] == "a.b+tag@example.co.uk"


# ------------------------------------------------------------ booking horizon
def test_booking_far_in_the_future_is_refused(settings):
    cal = Calendar(settings.db_path, max_days=180)
    with pytest.raises(BookingError, match="180 days"):
        cal.book(session_id="s", name="A", contact="a@b.co", service="Check-up", day="2099-12-31", slot="10:00", now=NOW)


def test_booking_inside_the_horizon_works(settings):
    cal = Calendar(settings.db_path, max_days=180)
    assert cal.book(session_id="s", name="A", contact="a@b.co", service="Check-up", day=TUESDAY, slot="10:00", now=NOW)


def test_horizon_edge_is_inclusive(settings):
    cal = Calendar(settings.db_path, max_days=7)          # NOW is Monday 2030-01-07
    cal.book(session_id="s", name="A", contact="a@b.co", service="Check-up", day="2030-01-14", slot="10:00", now=NOW)
    with pytest.raises(BookingError):
        cal.book(session_id="t", name="A", contact="a@b.co", service="Check-up", day="2030-01-15", slot="10:00", now=NOW)


# ------------------------------------------------------------ one slot, many visitors at once
def test_thirty_threads_one_slot_exactly_one_winner(settings):
    import threading
    cal, wins, odd = Calendar(settings.db_path), [], []

    def go(i):
        try:
            cal.book(session_id=f"s{i}", name=f"N{i}", contact="x@y.zz", service="Check-up", day=TUESDAY, slot="10:00", now=NOW)
            wins.append(i)
        except BookingError:
            pass
        except Exception as exc:
            odd.append(repr(exc))
    threads = [threading.Thread(target=go, args=(i,)) for i in range(30)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(wins) == 1 and not odd


# ------------------------------------------------------------ load and long conversations (offline model)
def test_many_threads_on_the_same_session_do_not_crash(agent):
    import threading
    errors = []

    def go(i):
        try:
            agent.handle("same", f"How much is whitening? {i}", now=NOW)
        except Exception as exc:
            errors.append(repr(exc))
    threads = [threading.Thread(target=go, args=(i,)) for i in range(20)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors


def test_evicted_session_starts_cleanly(agent):
    from app.agent import MAX_SESSIONS
    agent.handle("first", "I want to book a check-up, my name is Anna Test", now=NOW)
    for i in range(MAX_SESSIONS + 5):
        agent.handle(f"s{i}", "hello", now=NOW)
    assert "first" not in agent.sessions                                  # forgotten, but...
    r = agent.handle("first", "hello again", now=NOW)                    # ...the visitor is simply served again
    assert r["reply"] and r["state"].get("name") is None


def test_a_hundred_messages_in_one_session_keep_history_and_context_bounded(agent):
    from app.agent import MAX_CONTEXT_CHUNKS, MAX_HISTORY
    for i in range(100):
        agent.handle("long", ["price of filling", "opening hours", "do you treat children", "cancel appointment"][i % 4], now=NOW)
    sess = agent.sessions["long"]
    assert len(sess["history"]) <= 2 * MAX_HISTORY and len(sess["chunks"]) <= MAX_CONTEXT_CHUNKS
