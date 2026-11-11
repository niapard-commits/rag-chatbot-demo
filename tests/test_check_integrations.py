import pytest

from app.config import Settings
from app.hubspot import CUSTOM_PROPERTIES
from scripts import check_integrations as chk
from tests.fakes import FakeGoogle, FakeHubSpot


def settings_for(hs=None, g=None, token="tok"):
    s = Settings()
    s.hubspot_token = token if hs else ""
    s.hubspot_base_url = hs.url if hs else s.hubspot_base_url
    return s


def test_everything_off_is_fine(capsys):
    s = Settings()
    s.hubspot_token, s.google_calendar_id, s.google_service_account_file = "", "", ""
    assert chk.check_hubspot(s, False, False) is True and chk.check_calendar(s, False) is True
    assert capsys.readouterr().out.count("[skip]") == 2


def test_half_configured_calendar_is_an_error(capsys):
    s = Settings()
    s.google_calendar_id, s.google_service_account_file = "x@y.z", ""
    assert chk.check_calendar(s, False) is False
    assert "BOTH" in capsys.readouterr().out


def test_hubspot_missing_properties_are_reported_then_created(capsys):
    hs = FakeHubSpot()
    try:
        s = settings_for(hs)
        assert chk.check_hubspot(s, create=False, write=False) is False
        assert "custom properties do not exist" in capsys.readouterr().out
        assert chk.check_hubspot(s, create=True, write=True) is True
        out = capsys.readouterr().out
        assert "all 5 custom properties exist" in out and "test contact was written and removed" in out
        assert hs.contacts == {}                       # the test contact was removed again
    finally:
        hs.close()


def test_hubspot_wrong_token_is_explained(capsys):
    hs = FakeHubSpot()
    try:
        assert chk.check_hubspot(settings_for(hs, token="bad"), False, False) is False
        assert "token is wrong" in capsys.readouterr().out
    finally:
        hs.close()


def test_calendar_check_with_fake_google(tmp_path, capsys, monkeypatch):
    g = FakeGoogle()
    try:
        s = Settings()
        s.google_calendar_id, s.google_service_account_file, s.google_api_base = g.CAL, "key.json", g.url
        monkeypatch.setattr(chk, "service_account_token_provider", lambda path: (lambda: "gtoken"))
        assert chk.check_calendar(s, write=True) is True
        out = capsys.readouterr().out
        assert "calendar is readable" in out and "test event was created and deleted" in out
        assert g.events == {}
        g.freebusy_errors = [{"reason": "notFound"}]
        assert chk.check_calendar(s, write=False) is False
        assert "Share the calendar" in capsys.readouterr().out
    finally:
        g.close()
