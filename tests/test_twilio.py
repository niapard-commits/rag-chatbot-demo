import base64
import hashlib
import hmac
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from app import twilio_whatsapp as tw
from app.main import create_app

URL = "https://demo.example/twilio/whatsapp"
TOKEN = "twilio-secret"
SENDER = "whatsapp:+421900111222"


def sign(params, url=URL, token=TOKEN):
    data = url + "".join(k + params[k] for k in sorted(params))
    return base64.b64encode(hmac.new(token.encode(), data.encode(), hashlib.sha1).digest()).decode()


@pytest.fixture
def tw_client(settings, monkeypatch):
    settings.twilio_account_sid = "ACtest"
    settings.twilio_auth_token = TOKEN
    settings.twilio_webhook_url = URL
    app = create_app(settings)
    sent = []
    monkeypatch.setattr(app.state.twilio, "send", lambda to, text: sent.append((to, text)))
    return TestClient(app), sent


def post(client, body="How much is teeth whitening?", sid="SM1", sender=SENDER, signature=None, **extra):
    params = {"From": sender, "To": "whatsapp:+14155238886", "Body": body, "MessageSid": sid, "NumMedia": "0", **extra}
    headers = {"Content-Type": "application/x-www-form-urlencoded",
               "X-Twilio-Signature": sign(params) if signature is None else signature}
    return client.post("/twilio/whatsapp", content=urlencode(params).encode(), headers=headers)


def test_signature_matches_the_example_in_twilio_docs():
    params = {"CallSid": "CA1234567890ABCDE", "Caller": "+14158675310", "Digits": "1234",
              "From": "+14158675310", "To": "+18005551212"}
    header = "GvWf1cFY/Q7PnoempGyD5oXAezc="
    assert tw.signature_ok("12345", "https://mycompany.com/myapp.php?foo=1&bar=2", params, header)
    assert not tw.signature_ok("12345", "https://mycompany.com/myapp.php?foo=1&bar=2", params, "x" + header[1:])


def test_the_channel_is_off_without_settings(settings):
    assert TestClient(create_app(settings)).post("/twilio/whatsapp").status_code == 404


def test_a_text_message_is_answered_from_the_knowledge_base(tw_client):
    client, sent = tw_client
    r = post(client)
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/xml")
    assert "<Message>" in r.text and "220 EUR" in r.text   # the answer travels back in the webhook response
    assert sent == []   # nothing goes through the Messages API (a trial account may not send free text there)


def test_unsigned_or_wrongly_signed_calls_are_refused(tw_client):
    client, sent = tw_client
    assert post(client, signature="").status_code == 401
    assert post(client, signature="AAAA").status_code == 401
    assert sent == []


def test_a_repeated_delivery_is_answered_once(tw_client):
    client, sent = tw_client
    first = post(client, sid="SMsame")
    second = post(client, sid="SMsame")
    assert "220 EUR" in first.text and "<Message>" not in second.text


def test_a_photo_without_text_gets_a_polite_answer(tw_client):
    client, sent = tw_client
    r = post(client, body="", sid="SMimg", NumMedia="1", MediaUrl0="https://api.twilio.com/media/1")
    assert tw.TEXT_ONLY in r.text and sent == []


def test_calls_that_are_not_whatsapp_are_ignored(tw_client):
    client, sent = tw_client
    r = post(client, sender="+421900111222", sid="SMsms")
    assert r.status_code == 200 and "<Message>" not in r.text and sent == []


def test_a_slow_answer_is_sent_afterwards(settings, monkeypatch):
    import time
    settings.twilio_account_sid, settings.twilio_auth_token, settings.twilio_webhook_url = "ACtest", TOKEN, URL
    settings.twilio_reply_wait = 0.05
    app = create_app(settings)
    sent = []
    monkeypatch.setattr(app.state.twilio, "send", lambda to, text: sent.append((to, text)))
    real = app.state.agent.handle

    def slow(sid, text):
        time.sleep(0.4)
        return real(sid, text)

    monkeypatch.setattr(app.state.agent, "handle", slow)
    with TestClient(app) as client:
        r = post(client)
        assert r.status_code == 200 and "<Message>" not in r.text   # acknowledged at once, empty response
        for _ in range(50):
            if sent:
                break
            time.sleep(0.1)
    assert len(sent) == 1 and sent[0][0] == SENDER and "220 EUR" in sent[0][1]


def test_the_reply_is_valid_xml():
    reply = tw.twiml_reply("Tom & Jerry <b>5 > 3</b>")
    assert "&amp;" in reply and "&lt;b&gt;" in reply and "<b>" not in reply


def test_session_id_is_valid_and_per_number():
    assert tw.session_id_for("whatsapp:+421 900-111") == "tw421900111"


def test_send_uses_the_messages_api(monkeypatch):
    seen = {}

    class R:
        status_code = 201
        text = ""

    def fake_post(url, auth, data, timeout):
        seen.update(url=url, auth=auth, data=data)
        return R()

    monkeypatch.setattr(tw.requests, "post", fake_post)
    tw.TwilioChannel("ACtest", "tok", "+14155238886").send(SENDER, "x" * 2000)
    assert seen["url"] == "https://api.twilio.com/2010-04-01/Accounts/ACtest/Messages.json"
    assert seen["auth"] == ("ACtest", "tok")
    assert seen["data"]["From"] == "whatsapp:+14155238886" and seen["data"]["To"] == SENDER
    assert len(seen["data"]["Body"]) == tw.MAX_CHARS
