import json
import urllib.parse
from datetime import datetime

import pytest

from app.booking import BookingError, Calendar
from app.db import connect
from app.gcal import GoogleCalendarClient, service_account_token_provider
from tests.conftest import NOW, TUESDAY
from tests.fakes import FakeGoogle


@pytest.fixture
def google_server():
    s = FakeGoogle()
    yield s
    s.close()


def client(server, **kw):
    return GoogleCalendarClient(server.CAL, lambda: "gtoken", "Europe/Bratislava", server.url,
                                retry_delays=(0.01,), **kw)


def make_calendar(settings, server, **kw):
    cal = Calendar(settings.db_path, google=client(server), business_name="Bright Smile Dental", **kw)
    cal.event_retry_delays = (0.01, 0.01)
    return cal


def test_events_in_google_block_the_slots_they_overlap(settings, google_server):
    # 2030-01-08 is winter time in Bratislava (UTC+1): 09:00Z-09:30Z is 10:00-10:30 local.
    google_server.busy = [("2030-01-08T09:00:00Z", "2030-01-08T09:30:00Z")]
    cal = make_calendar(settings, google_server)
    free = cal.free_slots(TUESDAY, NOW)
    assert "10:00" not in free
    assert "09:30" in free and "10:30" in free          # touching edges do not block


def test_a_long_event_blocks_every_slot_it_touches(settings, google_server):
    google_server.busy = [("2030-01-08T09:15:00+01:00", "2030-01-08T10:45:00+01:00")]   # 09:15-10:45 local
    cal = make_calendar(settings, google_server)
    free = cal.free_slots(TUESDAY, NOW)
    for blocked in ("09:00", "09:30", "10:00", "10:30"):
        assert blocked not in free
    assert "11:00" in free


def test_busy_times_are_cached_for_a_short_time(settings, google_server):
    cal = make_calendar(settings, google_server)
    cal.free_slots(TUESDAY, NOW)
    cal.free_slots(TUESDAY, NOW)
    assert sum(1 for r in google_server.requests if r[1] == "/freeBusy") == 1


def test_booking_creates_a_google_event_and_stores_its_id(settings, google_server):
    cal = make_calendar(settings, google_server)
    b = cal.book(session_id="s1", name="Anna", contact="anna@example.com", service="Cleaning",
                 day=TUESDAY, slot="10:00", now=NOW)
    cal.flush()
    (event,) = google_server.events.values()
    assert event["summary"] == "Cleaning: Anna"
    assert event["start"] == {"dateTime": "2030-01-08T10:00:00", "timeZone": "Europe/Bratislava"}
    assert event["end"] == {"dateTime": "2030-01-08T10:30:00", "timeZone": "Europe/Bratislava"}
    assert "anna@example.com" in event["description"] and "Bright Smile Dental" in event["description"]
    assert event["extendedProperties"]["private"]["chatbot_session"] == "s1"
    with connect(settings.db_path) as conn:
        assert conn.execute("SELECT google_event_id FROM appointments WHERE id=?", (b.id,)).fetchone()[0] == "ev1"
    # the calendar id with "@" is URL-encoded in the path
    posts = [r[1] for r in google_server.requests if r[0] == "POST" and "events" in r[1]]
    assert posts == ["/calendars/" + urllib.parse.quote(google_server.CAL, safe="") + "/events"]


def test_an_event_added_by_hand_blocks_the_booking(settings, google_server):
    google_server.busy = [("2030-01-08T09:00:00Z", "2030-01-08T09:30:00Z")]    # 10:00 local
    cal = make_calendar(settings, google_server)
    with pytest.raises(BookingError, match="already taken"):
        cal.book(session_id="s1", name="Anna", contact="a@b.co", service="Cleaning", day=TUESDAY,
                 slot="10:00", now=NOW)
    with connect(settings.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM appointments").fetchone()[0] == 0
    assert google_server.events == {}


def test_the_busy_check_at_booking_time_ignores_a_stale_cache(settings, google_server):
    cal = make_calendar(settings, google_server)
    assert "10:00" in cal.free_slots(TUESDAY, NOW)                 # cached as free
    google_server.busy = [("2030-01-08T09:00:00Z", "2030-01-08T09:30:00Z")]   # the owner adds an event now
    with pytest.raises(BookingError):
        cal.book(session_id="s1", name="Anna", contact="a@b.co", service="Cleaning", day=TUESDAY,
                 slot="10:00", now=NOW)


def test_two_visitors_cannot_take_the_same_slot_with_google_on(settings, google_server):
    cal = make_calendar(settings, google_server)
    cal.book(session_id="s1", name="Anna", contact="a@b.co", service="Cleaning", day=TUESDAY, slot="10:00", now=NOW)
    with pytest.raises(BookingError, match="already taken"):
        cal.book(session_id="s2", name="Bob", contact="b@b.co", service="Filling", day=TUESDAY, slot="10:00", now=NOW)
    cal.flush()
    assert len(google_server.events) == 1


def test_google_down_chat_and_booking_still_work(settings, caplog):
    dead = GoogleCalendarClient("x@y.z", lambda: "t", "Europe/Bratislava", "http://127.0.0.1:9", timeout=0.3,
                                retry_delays=())
    cal = Calendar(settings.db_path, google=dead)
    cal.event_retry_delays = ()
    assert "10:00" in cal.free_slots(TUESDAY, NOW)
    assert cal.google_degraded is True
    b = cal.book(session_id="s1", name="Anna", contact="a@b.co", service="Cleaning", day=TUESDAY, slot="10:00", now=NOW)
    cal.flush()
    assert b.slot == "10:00"                       # saved locally
    assert any("could not be created" in r.message for r in caplog.records)


def test_calendar_not_shared_with_the_service_account_is_reported(settings, google_server, caplog):
    google_server.freebusy_errors = [{"domain": "global", "reason": "notFound"}]
    cal = make_calendar(settings, google_server)
    assert "10:00" in cal.free_slots(TUESDAY, NOW)
    assert cal.google_degraded is True
    assert any("not reachable" in r.message and "notFound" in r.message for r in caplog.records)


def test_wrong_token_degrades_instead_of_failing(settings, google_server):
    bad = GoogleCalendarClient(google_server.CAL, lambda: "nope", "Europe/Bratislava", google_server.url,
                               retry_delays=())
    cal = Calendar(settings.db_path, google=bad)
    assert cal.free_slots(TUESDAY, NOW)
    assert cal.google_degraded is True


def test_recovers_when_google_comes_back(settings, google_server):
    cal = make_calendar(settings, google_server)
    cal.busy_cache_seconds = 0
    google_server.fail_next = [403]
    cal.free_slots(TUESDAY, NOW)
    assert cal.google_degraded is True
    cal.free_slots(TUESDAY, NOW)
    assert cal.google_degraded is False


def test_weekend_and_past_rules_still_apply(settings, google_server):
    cal = make_calendar(settings, google_server)
    with pytest.raises(BookingError, match="weekend"):
        cal.book(session_id="s1", name="A", contact="a@b.co", service="Cleaning", day="2030-01-12", slot="10:00", now=NOW)
    assert google_server.events == {}


def test_service_account_key_file_is_exchanged_for_a_token(tmp_path, google_server):
    """Uses the real google-auth code with a throw-away RSA key and the fake token endpoint."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    key_file = tmp_path / "sa.json"
    key_file.write_text(json.dumps({"type": "service_account", "project_id": "demo", "private_key_id": "k1",
                                    "private_key": pem, "client_email": "bot@demo.iam.gserviceaccount.com",
                                    "client_id": "1", "token_uri": google_server.url + "/token"}))
    provider = service_account_token_provider(str(key_file))
    assert provider() == "gtoken"
    assert provider() == "gtoken"                       # the second call reuses the valid token
    assert google_server.token_requests == 1
    cal = Calendar(str(tmp_path / "t.db"), google=GoogleCalendarClient(
        google_server.CAL, provider, "Europe/Bratislava", google_server.url))
    assert cal.google.busy(datetime(2030, 1, 8).date()) == []
