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
