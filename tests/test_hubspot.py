import pytest

from app.crm import CRM
from app.db import connect
from app.hubspot import HubSpot, CUSTOM_PROPERTIES
from tests.fakes import FakeHubSpot


@pytest.fixture
def hs_server():
    s = FakeHubSpot()
    yield s
    s.close()


def make_crm(settings, server, **kw):
    hs = HubSpot("tok", server.url, retry_delays=(0.01, 0.01), **kw)
    crm = CRM(settings.db_path, hubspot=hs)
    return crm


def test_lead_becomes_contact_with_standard_and_custom_fields(settings, hs_server):
    hs_server.existing_properties = set(CUSTOM_PROPERTIES)
    crm = make_crm(settings, hs_server)
    crm.save_lead("s1", "Anna Kovac", "anna@example.com", "+421900111222", "Cleaning", "asked about price", "warm", "Wants a price")
    crm.flush()
    (contact,) = hs_server.contacts.values()
    assert contact["firstname"] == "Anna" and contact["lastname"] == "Kovac"
    assert contact["email"] == "anna@example.com" and contact["phone"] == "+421900111222"
    assert contact["chatbot_service"] == "Cleaning" and contact["chatbot_score"] == "warm"


def test_same_chat_updates_one_contact_and_stores_the_id(settings, hs_server):
    crm = make_crm(settings, hs_server)
    crm.save_lead("s1", "Anna", "anna@example.com", None, None)
    crm.flush()
    crm.save_lead("s1", "Anna", "anna@example.com", "+421900111222", "Check-up", None, "hot", "Booked")
    crm.flush()
    assert len(hs_server.contacts) == 1
    with connect(settings.db_path) as conn:
        stored = conn.execute("SELECT hubspot_id FROM leads WHERE session_id='s1'").fetchone()[0]
    assert stored in hs_server.contacts
    # the second write was a PATCH by id, not another upsert
    assert [r[0] for r in hs_server.requests if "/contacts" in r[1]] == ["POST", "PATCH"]


def test_two_chats_with_the_same_email_do_not_create_duplicates(settings, hs_server):
    crm = make_crm(settings, hs_server)
    crm.save_lead("s1", "Anna", "anna@example.com", None, "Cleaning")
    crm.flush()
    crm.save_lead("s2", "Anna K", "anna@example.com", None, "Filling")
    crm.flush()
    assert len(hs_server.contacts) == 1


def test_phone_only_lead_is_created_then_updated_by_id(settings, hs_server):
    crm = make_crm(settings, hs_server)
    crm.save_lead("s1", "Peter", None, "+421900333444", "Filling")
    crm.flush()
    crm.save_lead("s1", "Peter", None, "+421900333444", "Filling", "asked about pain", "warm", "Pain")
    crm.flush()
    assert len(hs_server.contacts) == 1
    (contact,) = hs_server.contacts.values()
    assert contact["phone"] == "+421900333444" and "email" not in contact


def test_missing_custom_properties_still_saves_the_standard_fields(settings, hs_server, caplog):
    hs_server.properties_exist = False
    crm = make_crm(settings, hs_server)
    crm.save_lead("s1", "Anna", "anna@example.com", None, "Cleaning", None, "hot", "Booked")
    crm.flush()
    (contact,) = hs_server.contacts.values()
    assert contact["firstname"] == "Anna" and "chatbot_service" not in contact
    assert any("custom properties are missing" in r.message for r in caplog.records)


def test_booking_writes_the_appointment_to_the_contact(settings, hs_server):
    hs_server.existing_properties = set(CUSTOM_PROPERTIES)
    crm = make_crm(settings, hs_server)
    crm.save_lead("s1", "Anna", "anna@example.com", None, "Cleaning")
    crm.flush()
    crm.notify_booking("s1", "Anna", "anna@example.com", "Cleaning", "2030-01-08", "10:00")
    crm.flush()
    (contact,) = hs_server.contacts.values()
    assert contact["chatbot_appointment"] == "Cleaning, 2030-01-08 10:00"


def test_booking_without_a_saved_lead_still_reaches_hubspot(settings, hs_server):
    crm = make_crm(settings, hs_server)
    crm.notify_booking("s9", "Anna", "anna@example.com", "Cleaning", "2030-01-08", "10:00")
    crm.flush()
    assert len(hs_server.contacts) == 1


def test_contact_deleted_in_hubspot_is_created_again(settings, hs_server):
    crm = make_crm(settings, hs_server)
    crm.save_lead("s1", "Anna", "anna@example.com", None, None)
    crm.flush()
    hs_server.contacts.clear()
    crm.save_lead("s1", "Anna", "anna@example.com", "+421900111222", None)
    crm.flush()
    assert len(hs_server.contacts) == 1


def test_temporary_errors_are_retried(settings, hs_server):
    hs_server.fail_next = [429, 503]
    crm = make_crm(settings, hs_server)
    crm.save_lead("s1", "Anna", "anna@example.com", None, None)
    crm.flush()
    assert len(hs_server.contacts) == 1


def test_wrong_token_never_breaks_the_chat(settings, hs_server, caplog):
    hs = HubSpot("wrong", hs_server.url, retry_delays=(0.01,))
    crm = CRM(settings.db_path, hubspot=hs)
    assert crm.save_lead("s1", "Anna", "anna@example.com", None, None) is True   # saved locally
    crm.flush()
    assert crm.list_leads()[0]["name"] == "Anna"
    assert any("HubSpot sync failed" in r.message for r in caplog.records)


def test_hubspot_down_never_breaks_the_chat(settings, caplog):
    hs = HubSpot("tok", "http://127.0.0.1:9", timeout=0.5, retry_delays=(0.01,))
    crm = CRM(settings.db_path, hubspot=hs)
    crm.save_lead("s1", "Anna", "anna@example.com", None, None)
    crm.flush()
    assert crm.list_leads()[0]["email"] == "anna@example.com"


def test_setup_helpers(hs_server):
    hs = HubSpot("tok", hs_server.url, retry_delays=())
    hs.check_access()
    assert set(hs.missing_properties()) == set(CUSTOM_PROPERTIES)
    hs.create_properties(hs.missing_properties())
    assert hs.missing_properties() == []
    hs.create_properties(list(CUSTOM_PROPERTIES))      # creating again is harmless (409)
