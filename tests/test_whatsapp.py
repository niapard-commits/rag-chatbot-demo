import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app import whatsapp
from app.main import create_app


@pytest.fixture
def wa_client(settings, monkeypatch):
    settings.whatsapp_verify_token = "verify-me"
    settings.whatsapp_access_token = "token"
    settings.whatsapp_phone_number_id = "1234"
    settings.whatsapp_app_secret = "appsecret"
    app = create_app(settings)
    sent = []
    monkeypatch.setattr(app.state.whatsapp, "send", lambda to, text: sent.append((to, text)))
    return TestClient(app), sent


def payload(text="How much is teeth whitening?", msg_id="wamid.1", sender="421900111222", kind="text"):
    msg = {"id": msg_id, "from": sender, "type": kind}
    if kind == "text":
        msg["text"] = {"body": text}
    return {"object": "whatsapp_business_account",
            "entry": [{"changes": [{"value": {"messages": [msg]}}]}]}


def post(client, body, secret="appsecret", signed=True):
    raw = json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if signed:
        headers["X-Hub-Signature-256"] = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return client.post("/whatsapp/webhook", content=raw, headers=headers)


def test_the_channel_is_off_without_settings(settings):
    client = TestClient(create_app(settings))
    assert client.get("/whatsapp/webhook").status_code == 404


def test_meta_verification_handshake(wa_client):
    client, _ = wa_client
    ok = client.get("/whatsapp/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me",
                                                 "hub.challenge": "98765"})
    assert ok.status_code == 200 and ok.text == "98765"
    bad = client.get("/whatsapp/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "wrong",
                                                  "hub.challenge": "1"})
    assert bad.status_code == 403


def test_a_text_message_is_answered_from_the_knowledge_base(wa_client):
    client, sent = wa_client
    assert post(client, payload()).status_code == 200
    assert len(sent) == 1 and sent[0][0] == "421900111222" and "220 EUR" in sent[0][1]


def test_unsigned_or_wrongly_signed_calls_are_refused(wa_client):
    client, sent = wa_client
    assert post(client, payload(), signed=False).status_code == 401
    assert post(client, payload(), secret="other").status_code == 401
    assert sent == []


def test_a_repeated_delivery_is_answered_once(wa_client):
    client, sent = wa_client
    post(client, payload(msg_id="wamid.same"))
    post(client, payload(msg_id="wamid.same"))
    assert len(sent) == 1


def test_non_text_messages_get_a_polite_answer(wa_client):
    client, sent = wa_client
    post(client, payload(kind="image", msg_id="wamid.img"))
    assert sent == [("421900111222", whatsapp.TEXT_ONLY)]


def test_status_updates_are_ignored(wa_client):
    client, sent = wa_client
    body = {"entry": [{"changes": [{"value": {"statuses": [{"id": "x", "status": "delivered"}]}}]}]}
    assert post(client, body).status_code == 200 and sent == []


def test_session_id_is_valid_and_per_number():
    assert whatsapp.session_id_for("+421 900-111") == "wa421900111"


def test_send_uses_the_graph_api(monkeypatch):
    seen = {}

    class R:
        status_code = 200
        text = ""

    def fake_post(url, headers, json, timeout):
        seen.update(url=url, headers=headers, json=json)
        return R()

    monkeypatch.setattr(whatsapp.requests, "post", fake_post)
    whatsapp.WhatsAppChannel("tok", "555", "https://graph.example/v1").send("4219", "Hi")
    assert seen["url"] == "https://graph.example/v1/555/messages"
    assert seen["headers"]["Authorization"] == "Bearer tok"
    assert seen["json"]["to"] == "4219" and seen["json"]["text"] == {"body": "Hi"}


def test_failed_delivery_status_is_reported():
    payload = {"entry": [{"changes": [{"value": {"statuses": [
        {"id": "wamid.1", "status": "delivered"},
        {"id": "wamid.2", "status": "failed", "errors": [{"code": 131047, "title": "Re-engagement message"}]}]}}]}]}
    assert whatsapp.failed_statuses(payload) == [
        {"id": "wamid.2", "status": "failed", "code": 131047, "title": "Re-engagement message"}]
