import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest


class Receiver:
    """A tiny local web server that records what the CRM webhook sends."""

    def __init__(self, status=200):
        self.requests, outer = [], self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                outer.requests.append((dict(self.headers), json.loads(body)))
                self.send_response(status)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/hook"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture
def receiver():
    r = Receiver()
    yield r
    r.close()


def test_lead_is_posted_with_secret_header(settings, receiver):
    from app.crm import CRM
    crm = CRM(settings.db_path, receiver.url, webhook_secret="s3cret")
    crm.save_lead("s1", "Anna", "a@b.co", None, "Check-up", "asked about parking")
    crm.flush()
    headers, payload = receiver.requests[0]
    assert headers["X-Webhook-Secret"] == "s3cret"
    assert payload["event"] == "lead" and payload["name"] == "Anna" and payload["note"] == "asked about parking"


def test_appointment_is_posted(settings, receiver):
    from app.crm import CRM
    crm = CRM(settings.db_path, receiver.url)
    crm.notify_booking("s1", "Anna", "a@b.co", "Check-up", "2030-01-08", "10:00")
    crm.flush()
    assert receiver.requests[0][1] == {"event": "appointment", "session_id": "s1", "name": "Anna",
                                       "contact": "a@b.co", "service": "Check-up", "day": "2030-01-08", "slot": "10:00"}


def test_webhook_down_does_not_break_the_chat(settings):
    from app.crm import CRM
    crm = CRM(settings.db_path, "http://127.0.0.1:9/unreachable", timeout=1)
    crm.retry_delays = (0.01, 0.01)
    assert crm.save_lead("s1", "Anna", "a@b.co", None, None) is True     # no exception
    crm.flush()
    assert crm.list_leads()[0]["name"] == "Anna"                        # still saved locally


def test_webhook_error_status_does_not_break_the_chat(settings):
    from app.crm import CRM
    r = Receiver(status=500)
    try:
        crm = CRM(settings.db_path, r.url)
        crm.retry_delays = (0.01, 0.01)
        assert crm.save_lead("s1", "Anna", "a@b.co", None, None) is True
        crm.flush()
        assert len(r.requests) == 3                  # a server error is retried: 3 attempts in total
    finally:
        r.close()


def test_rejected_request_is_not_retried(settings):
    from app.crm import CRM
    r = Receiver(status=401)                         # e.g. wrong secret: retrying cannot help
    try:
        crm = CRM(settings.db_path, r.url)
        crm.retry_delays = (0.01, 0.01)
        crm.save_lead("s1", "Anna", "a@b.co", None, None)
        crm.flush()
        assert len(r.requests) == 1
    finally:
        r.close()


def test_slow_webhook_does_not_delay_the_caller(settings):
    import time
    from app.crm import CRM
    gate = threading.Event()

    class Slow(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            gate.wait(5)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Slow)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        crm = CRM(settings.db_path, f"http://127.0.0.1:{server.server_port}/hook")
        started = time.monotonic()
        crm.save_lead("s1", "Anna", "a@b.co", None, None)
        assert time.monotonic() - started < 1.0      # returns at once, the sending happens in the background
    finally:
        gate.set()
        crm.flush()
        server.shutdown()
