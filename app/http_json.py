"""Small JSON-over-HTTP helper with retries, shared by the HubSpot and Google Calendar adapters.
Only the standard library is used. A request is retried on network errors, HTTP 429 and HTTP 5xx; any other HTTP error
means our request is wrong, so retrying would not help and the error is raised at once."""
import json
import logging
import time
import urllib.error
import urllib.request

log = logging.getLogger("chatbot.http")


class HttpError(Exception):
    def __init__(self, status: int, body: str, url: str = ""):
        super().__init__(f"HTTP {status} from {url}: {body[:300]}")
        self.status, self.body, self.url = status, body, url


def request_json(method: str, url: str, headers: dict, payload: dict | None = None, timeout: float = 8.0,
                 retry_delays: tuple = (1.0, 3.0)):
    """Returns the decoded JSON answer (None for an empty body). Raises HttpError or the last network error."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    all_headers = {"Accept": "application/json", **headers}
    if data is not None:
        all_headers["Content-Type"] = "application/json"
    last: Exception | None = None
    for attempt in range(len(retry_delays) + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=all_headers, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            if 400 <= exc.code < 500 and exc.code != 429:
                raise HttpError(exc.code, body, url) from None
            last = HttpError(exc.code, body, url)
        except Exception as exc:    # timeouts, connection refused, DNS problems
            last = exc
        if attempt < len(retry_delays):
            time.sleep(retry_delays[attempt])
    raise last
