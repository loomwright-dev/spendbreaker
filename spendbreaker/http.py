"""Minimal HTTP transport (stdlib only). Injected everywhere so tests never touch the network."""
import json
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "spendbreaker/0.1 (+self-hosted; no telemetry)"


class HTTPError(Exception):
    def __init__(self, status, body, url):
        super().__init__(f"HTTP {status} from {redact_url(url)}: {body[:300]!r}")
        self.status = status
        self.body = body


def redact_url(url):
    # Telegram puts the bot token in the path; never echo it in errors/logs.
    if "api.telegram.org/bot" in url:
        return "https://api.telegram.org/bot<redacted>/..."
    return url


class Response:
    def __init__(self, status, body, headers=None):
        self.status = status
        self.body = body if isinstance(body, bytes) else body.encode()
        self.headers = headers or {}

    @property
    def text(self):
        return self.body.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.text)


def urllib_transport(method, url, headers=None, body=None, timeout=30):
    data = body.encode() if isinstance(body, str) else body
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("User-Agent", USER_AGENT)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return Response(r.status, r.read(), dict(r.headers))
    except urllib.error.HTTPError as e:
        return Response(e.code, e.read() or b"", dict(e.headers or {}))


class Client:
    """Thin wrapper: request() returns Response, raises HTTPError on non-2xx."""

    def __init__(self, transport=None):
        self.transport = transport or urllib_transport

    def request(self, method, url, params=None, headers=None, json_body=None, data=None):
        if params:
            url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params, doseq=True)
        headers = dict(headers or {})
        body = data
        if json_body is not None:
            body = json.dumps(json_body)
            headers.setdefault("Content-Type", "application/json")
        resp = self.transport(method, url, headers, body)
        if not 200 <= resp.status < 300:
            raise HTTPError(resp.status, resp.text, url)
        return resp

    def get_json(self, url, **kw):
        return self.request("GET", url, **kw).json()
