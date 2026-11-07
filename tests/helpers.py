import datetime as dt
import json
import urllib.request

from spendbreaker.http import Client, Response

NOW = dt.datetime(2026, 11, 7, 15, 0, tzinfo=dt.timezone.utc)


def ts(day, hour=0):
    return int(dt.datetime(2026, 11, day, hour, tzinfo=dt.timezone.utc).timestamp())


class FakeTransport:
    """Routes (method, url-prefix) -> response body/callable. Records every call."""

    def __init__(self, routes=None):
        self.routes = routes or {}
        self.calls = []

    def __call__(self, method, url, headers=None, body=None):
        self.calls.append({"method": method, "url": url, "headers": headers or {}, "body": body})
        for (m, prefix), resp in self.routes.items():
            if m == method and url.startswith(prefix):
                if callable(resp):
                    resp = resp(method, url, headers, body)
                if isinstance(resp, Response):
                    return resp
                if isinstance(resp, (dict, list)):
                    return Response(200, json.dumps(resp))
                return Response(200, resp)
        return Response(404, f"no fake route for {method} {url}")


def fake_client(routes=None):
    t = FakeTransport(routes)
    return Client(t), t


def block_network():
    """Belt and braces: make any accidental real HTTP call fail loudly."""
    def _boom(*a, **k):
        raise AssertionError("real network access attempted in tests")
    urllib.request.urlopen = _boom
