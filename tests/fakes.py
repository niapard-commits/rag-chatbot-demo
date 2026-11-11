"""Tiny fake HubSpot and Google Calendar servers for the tests (they behave like the parts of the real APIs we use)."""
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


class _Server:
    def __init__(self):
        self.requests = []            # (method, path, headers, body)
        self.fail_next = []           # statuses to answer with before behaving normally
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _handle(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw_body = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw_body) if raw_body else None
                except ValueError:            # the token endpoint receives a form, not JSON
                    body = raw_body.decode()
                outer.requests.append((self.command, self.path, dict(self.headers), body))
                if outer.fail_next:
                    status = outer.fail_next.pop(0)
                    self._send(status, {"message": "forced failure"})
                    return
                status, payload = outer.route(self.command, self.path, body, dict(self.headers))
                self._send(status, payload)

            def _send(self, status, payload):
                raw = json.dumps(payload).encode() if payload is not None else b""
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_POST = do_PATCH = do_DELETE = _handle

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()

    def route(self, method, path, body, headers):
        raise NotImplementedError


class FakeHubSpot(_Server):
    def __init__(self, token="tok"):
        self.token, self.contacts, self.next_id = token, {}, 1000
        self.properties_exist = True       # False: custom properties are missing in this HubSpot account
        self.existing_properties = set()
        super().__init__()

    def _new(self, props):
        self.next_id += 1
        self.contacts[str(self.next_id)] = dict(props)
        return str(self.next_id)

    def _check_props(self, props):
        if not self.properties_exist and any(k.startswith("chatbot_") for k in props):
            return 400, {"status": "error", "category": "VALIDATION_ERROR",
                         "message": "Property values were not valid: PROPERTY_DOESNT_EXIST"}
        return None

    def route(self, method, path, body, headers):
        if headers.get("Authorization") != f"Bearer {self.token}":
            return 401, {"message": "bad token"}
        if method == "POST" and path == "/crm/v3/objects/contacts/batch/upsert":
            item = body["inputs"][0]
            err = self._check_props(item["properties"])
            if err:
                return err
            email = item["id"]
            for cid, c in self.contacts.items():
                if c.get("email") == email:
                    c.update(item["properties"])
                    return 200, {"results": [{"id": cid}]}
            cid = self._new({**item["properties"], "email": email})
            return 201, {"results": [{"id": cid}]}
        if method == "POST" and path == "/crm/v3/objects/contacts":
            err = self._check_props(body["properties"])
            return err or (201, {"id": self._new(body["properties"])})
        m = re.fullmatch(r"/crm/v3/objects/contacts/(\d+)", path)
        if m and method == "PATCH":
            if m.group(1) not in self.contacts:
                return 404, {"message": "not found"}
            err = self._check_props(body["properties"])
            if err:
                return err
            self.contacts[m.group(1)].update(body["properties"])
            return 200, {"id": m.group(1)}
        if m and method == "DELETE":
            self.contacts.pop(m.group(1), None)
            return 204, None
        if method == "GET" and path.startswith("/crm/v3/objects/contacts?"):
            return 200, {"results": []}
        m = re.fullmatch(r"/crm/v3/properties/contacts/(\w+)", path)
        if m and method == "GET":
            return (200, {"name": m.group(1)}) if m.group(1) in self.existing_properties else (404, {"message": "no"})
        if method == "POST" and path == "/crm/v3/properties/contacts":
            if body["name"] in self.existing_properties:
                return 409, {"message": "exists"}
            self.existing_properties.add(body["name"])
            return 201, {"name": body["name"]}
        return 404, {"message": f"unknown route {method} {path}"}


class FakeGoogle(_Server):
    CAL = "clinic@example.com"

    def __init__(self):
        self.busy, self.events, self.freebusy_errors = [], {}, None   # busy: list of (start_iso, end_iso)
        self.next_id = 0
        super().__init__()

    def route(self, method, path, body, headers):
        if method == "POST" and path == "/token":
            self.token_requests = getattr(self, "token_requests", 0) + 1
            return 200, {"access_token": "gtoken", "expires_in": 3600, "token_type": "Bearer"}
        if headers.get("Authorization") != "Bearer gtoken":
            return 401, {"error": "bad token"}
        if method == "POST" and path == "/freeBusy":
            cal = body["items"][0]["id"]
            info = {"errors": self.freebusy_errors} if self.freebusy_errors else {"busy": [
                {"start": s, "end": e} for s, e in self.busy]}
            return 200, {"calendars": {cal: info}}
        m = re.fullmatch(r"/calendars/([^/]+)/events", path)
        if method == "POST" and m:
            self.next_id += 1
            event_id = f"ev{self.next_id}"
            self.events[event_id] = body
            return 200, {"id": event_id}
        m = re.fullmatch(r"/calendars/([^/]+)/events/(\w+)", path)
        if method == "DELETE" and m:
            self.events.pop(m.group(2), None)
            return 204, None
        return 404, {"error": f"unknown route {method} {path}"}
