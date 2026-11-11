import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def client(settings):
    settings.admin_token = "secret"
    return TestClient(create_app(settings))


def chat(client, text, sid="abc"):
    return client.post("/chat", json={"session_id": sid, "message": text})


def test_health_and_index(client):
    assert client.get("/health").json()["status"] == "ok"
    assert "Bright Smile Dental" in client.get("/").text


def test_chat_returns_reply(client):
    r = chat(client, "How much is teeth whitening?")
    assert r.status_code == 200 and "220 EUR" in r.json()["reply"]


def test_validation_errors(client):
    assert client.post("/chat", json={"session_id": "bad id!", "message": "hi"}).status_code == 422
    assert client.post("/chat", json={"session_id": "ok", "message": ""}).status_code == 422
    assert client.post("/chat", json={"session_id": "ok", "message": "x" * 1001}).status_code == 422


def test_rate_limit(client):
    codes = [chat(client, "hi", sid="spam").status_code for _ in range(22)]
    assert codes.count(200) == 20 and codes[-1] == 429


def test_admin_endpoints_need_the_token(client):
    assert client.get("/admin/leads").status_code == 401
    assert client.get("/admin/leads", headers={"X-Admin-Token": "wrong"}).status_code == 401
    chat(client, "My name is Anna Novak")
    chat(client, "anna@example.com")
    leads = client.get("/admin/leads", headers={"X-Admin-Token": "secret"}).json()
    assert leads[0]["email"] == "anna@example.com"
    assert client.post("/admin/reindex", headers={"X-Admin-Token": "secret"}).json()["chunks"] > 0


def test_admin_disabled_without_token(settings):
    settings.admin_token = ""
    assert TestClient(create_app(settings)).get("/admin/leads").status_code == 404


def test_new_session_ids_do_not_bypass_the_ip_limit(settings):
    settings.rate_limit_ip, settings.daily_message_cap = 5, 0
    client = TestClient(create_app(settings))
    codes = [chat(client, "opening hours", sid=f"s{i}").status_code for i in range(8)]
    assert codes[:5] == [200] * 5 and codes[5:] == [429] * 3


def test_other_ip_addresses_have_their_own_limit(settings):
    settings.rate_limit_ip, settings.daily_message_cap, settings.trust_proxy = 2, 0, True
    client = TestClient(create_app(settings))
    send = lambda ip, n: client.post("/chat", json={"session_id": f"s{n}", "message": "hours"},
                                     headers={"X-Forwarded-For": ip}).status_code
    assert [send("1.1.1.1", 1), send("1.1.1.1", 2), send("1.1.1.1", 3)] == [200, 200, 429]
    assert send("2.2.2.2", 4) == 200


def test_forwarded_header_is_ignored_unless_the_proxy_is_trusted(settings):
    settings.rate_limit_ip, settings.daily_message_cap, settings.trust_proxy = 2, 0, False
    client = TestClient(create_app(settings))
    codes = [client.post("/chat", json={"session_id": f"s{n}", "message": "hours"},
                         headers={"X-Forwarded-For": f"9.9.9.{n}"}).status_code for n in range(3)]
    assert codes == [200, 200, 429]      # a visitor cannot dodge the limit by faking the header


def test_daily_cap_stops_the_chat_for_everyone(settings):
    settings.rate_limit_ip, settings.daily_message_cap = 0, 3
    client = TestClient(create_app(settings))
    codes = [chat(client, "hours", sid=f"s{i}").status_code for i in range(5)]
    assert codes == [200, 200, 200, 429, 429]
    assert client.get("/health").status_code == 200      # only the chat is stopped
