"""Lead qualification: hot / warm / cold with a reason, kept in SQLite and sent to the CRM."""
import sqlite3

from app.agent import clean_fields
from app.crm import CRM
from app.db import init_db
from tests.conftest import NOW, TUESDAY
from tests.test_crm import receiver  # noqa: F401  (fixture)


def say(agent, session, text):
    return agent.handle(session, text, now=NOW)


def lead(crm, n=0):
    return crm.list_leads()[n]


# ------------------------------------------------------------ validation of what the model reports
def test_valid_score_is_kept_with_its_reason():
    out = clean_fields({"score": "Hot", "score_reason": "  Has a toothache.  "})
    assert out["score"] == "hot" and out["score_reason"] == "Has a toothache."


def test_invalid_score_is_dropped_together_with_its_reason():
    assert clean_fields({"score": "super hot", "score_reason": "x"}) == {}
    assert clean_fields({"score": None, "score_reason": "x"}) == {}
    assert clean_fields({"score": 5}) == {}


def test_reason_is_shortened():
    assert len(clean_fields({"score": "warm", "score_reason": "x" * 500})["score_reason"]) == 200


# ------------------------------------------------------------ scoring in the conversation
def test_urgent_problem_is_a_hot_lead(agent, crm):
    say(agent, "s1", "I have a terrible toothache")
    say(agent, "s1", "My name is Anna Novak, phone +421 900 111 222")
    assert lead(crm)["score"] == "hot" and "urgent" in lead(crm)["score_reason"].lower()


def test_price_question_is_a_warm_lead(agent, crm):
    say(agent, "s1", "How much is teeth whitening?")
    say(agent, "s1", "My name is Peter Kovac, phone +421 900 333 444")
    assert lead(crm)["score"] == "warm"


def test_no_signal_means_no_score(agent, crm):
    say(agent, "s1", "What are your opening hours?")
    say(agent, "s1", "My name is Eva Maly, email eva@example.com")
    assert lead(crm)["score"] is None


def test_score_changes_when_the_visitor_shows_more_interest(agent, crm):
    say(agent, "s1", "How much is a cleaning?")
    say(agent, "s1", "My name is Eva Maly, email eva@example.com")
    assert lead(crm)["score"] == "warm"
    say(agent, "s1", "I want to book it, I have a terrible toothache")
    assert lead(crm)["score"] == "hot" and len(crm.list_leads()) == 1


def test_confirmed_booking_makes_the_lead_hot_and_keeps_it_hot(agent, crm):
    say(agent, "s1", "How much is teeth whitening?")                      # warm
    say(agent, "s1", "My name is Anna Novak, email anna@example.com")
    assert lead(crm)["score"] == "warm"
    say(agent, "s1", "I want to book a teeth whitening")
    say(agent, "s1", f"{TUESDAY} at 10:00")
    r = say(agent, "s1", "yes")
    assert r["booking"] is not None
    assert lead(crm)["score"] == "hot" and "Booked Teeth whitening" in lead(crm)["score_reason"]
    say(agent, "s1", "How much is a filling?")                            # the mock would say "warm" here
    assert lead(crm)["score"] == "hot"


def test_booking_does_not_erase_the_service_stored_on_the_lead(agent, crm):
    say(agent, "s1", "I want to book a teeth whitening")
    say(agent, "s1", "My name is Anna Novak")
    say(agent, "s1", "anna@example.com")
    say(agent, "s1", f"{TUESDAY} at 10:00")
    say(agent, "s1", "yes")
    say(agent, "s1", "thanks")                                            # state no longer has a service
    assert lead(crm)["service"] == "Teeth whitening"


# ------------------------------------------------------------ storage and CRM
def test_score_goes_to_the_webhook(settings, receiver):  # noqa: F811
    crm = CRM(settings.db_path, receiver.url)
    crm.save_lead("s1", "Anna", "a@b.co", None, "Check-up", None, "hot", "Wants to come today.")
    crm.flush()
    payload = receiver.requests[0][1]
    assert payload["score"] == "hot" and payload["score_reason"] == "Wants to come today."


def test_update_without_a_score_keeps_the_old_one(crm):
    crm.save_lead("s1", "Anna", "a@b.co", None, "Check-up", None, "warm", "Asked the price.")
    crm.save_lead("s1", "Anna", "a@b.co", "+421 900 123 456", "Check-up", None, None, None)
    row = lead(crm)
    assert row["score"] == "warm" and row["phone"] == "+421 900 123 456"


def test_old_database_without_score_columns_is_migrated(tmp_path):
    path = str(tmp_path / "old.db")
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE leads (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, session_id TEXT, "
                     "name TEXT, email TEXT, phone TEXT, service TEXT, note TEXT, UNIQUE(session_id))")
        conn.execute("INSERT INTO leads (session_id, name) VALUES ('old', 'Old Lead')")
    init_db(path)
    crm = CRM(path)
    assert lead(crm)["name"] == "Old Lead" and lead(crm)["score"] is None
    crm.save_lead("old", "Old Lead", "o@l.co", None, None, None, "cold", "Browsing.")
    assert lead(crm)["score"] == "cold"
