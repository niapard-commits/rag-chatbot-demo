"""A whole conversation with HubSpot and Google Calendar switched on (fake servers): the visitor books a visit,
the bot saves the lead, creates the HubSpot contact and the calendar event."""
import pytest

from app.agent import ChatAgent
from app.booking import Calendar
from app.crm import CRM
from app.gcal import GoogleCalendarClient
from app.hubspot import CUSTOM_PROPERTIES, HubSpot
from app.llm import FallbackLLM, MockLLM
from app.rag.retriever import BM25Retriever
from tests.conftest import NOW, TUESDAY
from tests.fakes import FakeGoogle, FakeHubSpot


@pytest.fixture
def servers():
    hs, g = FakeHubSpot(), FakeGoogle()
    hs.existing_properties = set(CUSTOM_PROPERTIES)
    yield hs, g
    hs.close()
    g.close()


@pytest.fixture
def full_agent(settings, servers):
    hs, g = servers
    crm = CRM(settings.db_path, hubspot=HubSpot("tok", hs.url, retry_delays=(0.01,)))
    cal = Calendar(settings.db_path, google=GoogleCalendarClient(g.CAL, lambda: "gtoken", "Europe/Bratislava", g.url,
                                                                 retry_delays=(0.01,)), business_name="Bright Smile Dental")
    return ChatAgent(settings, BM25Retriever(settings.knowledge_dir), FallbackLLM([MockLLM()]), crm, cal)


def say(agent, text, session="s1"):
    return agent.handle(session, text, now=NOW)


def test_booking_reaches_hubspot_and_google_calendar(full_agent, servers):
    hs, g = servers
    say(full_agent, "I want to book a teeth whitening")
    say(full_agent, "My name is Anna Novak")
    say(full_agent, "anna@example.com")
    say(full_agent, f"{TUESDAY} at 10:00")
    reply = say(full_agent, "yes")
    full_agent.crm.flush()
    full_agent.calendar.flush()
    assert reply["booking"]["time"] == "10:00"
    (contact,) = hs.contacts.values()
    assert contact["email"] == "anna@example.com" and contact["firstname"] == "Anna"
    assert contact["chatbot_score"] == "hot" and contact["chatbot_service"] == "Teeth whitening"
    assert contact["chatbot_appointment"] == f"Teeth whitening, {TUESDAY} 10:00"
    (event,) = g.events.values()
    assert event["summary"] == "Teeth whitening: Anna Novak"
    assert event["start"]["dateTime"] == f"{TUESDAY}T10:00:00"


def test_hand_made_event_is_respected_in_the_conversation(full_agent, servers):
    hs, g = servers
    g.busy = [(f"{TUESDAY}T09:00:00Z", f"{TUESDAY}T09:30:00Z")]       # 10:00 local is busy
    reply = say(full_agent, "Book a cleaning, I'm Anna Novak, anna@example.com, " + TUESDAY + " 10:00")
    assert reply["booking"] is None and "already taken" in reply["reply"]     # said before asking to confirm
    assert "10:00" not in reply["reply"].split("Free times")[-1]       # 10:00 is not offered as free
    assert g.events == {}


def test_chat_works_when_both_services_are_down(settings):
    crm = CRM(settings.db_path, hubspot=HubSpot("t", "http://127.0.0.1:9", timeout=0.3, retry_delays=()))
    dead = GoogleCalendarClient("x@y.z", lambda: "t", "Europe/Bratislava", "http://127.0.0.1:9", timeout=0.3,
                                retry_delays=())
    cal = Calendar(settings.db_path, google=dead)
    cal.event_retry_delays = ()
    agent = ChatAgent(settings, BM25Retriever(settings.knowledge_dir), FallbackLLM([MockLLM()]), crm, cal)
    say(agent, "I want to book a cleaning")
    say(agent, "My name is Anna Novak")
    say(agent, "anna@example.com")
    say(agent, f"{TUESDAY} at 10:00")
    reply = say(agent, "yes")
    crm.flush()
    cal.flush()
    assert reply["booking"]["time"] == "10:00"
    assert crm.list_leads()[0]["email"] == "anna@example.com"


def test_app_is_wired_from_settings_and_health_reports_it(settings, servers, tmp_path):
    """create_app() builds both adapters from the settings (real service-account sign-in against the fake token endpoint)."""
    import json
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from fastapi.testclient import TestClient
    from app.main import create_app

    hs, g = servers
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    key_file = tmp_path / "sa.json"
    key_file.write_text(json.dumps({"type": "service_account", "project_id": "demo", "private_key_id": "k1",
                                    "private_key": pem, "client_email": "bot@demo.iam.gserviceaccount.com",
                                    "client_id": "1", "token_uri": g.url + "/token"}))
    settings.hubspot_token, settings.hubspot_base_url = "tok", hs.url
    settings.google_calendar_id, settings.google_service_account_file = g.CAL, str(key_file)
    settings.google_api_base = g.url
    client = TestClient(create_app(settings))
    health = client.get("/health").json()
    assert health["hubspot"] is True and health["google_calendar"] is True and health["google_calendar_degraded"] is False
    r = client.post("/chat", json={"session_id": "w1", "message": "My name is Eva Maly, email eva@example.com"})
    assert r.status_code == 200
    client.app.state.agent.crm.flush()
    assert any(c.get("email") == "eva@example.com" for c in hs.contacts.values())


def test_bad_key_file_switches_google_off_but_the_app_starts(settings, tmp_path):
    from fastapi.testclient import TestClient
    from app.main import create_app
    bad = tmp_path / "broken.json"
    bad.write_text("{not json")
    settings.google_calendar_id, settings.google_service_account_file = "x@y.z", str(bad)
    health = TestClient(create_app(settings)).get("/health").json()
    assert health["google_calendar"] is False and health["status"] == "ok"
