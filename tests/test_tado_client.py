import logging

import pytest
import requests

import tado_client
from tado_client import TadoClient, parse_rate_limit_remaining


class FakeAuth:
    def __init__(self):
        self.refreshes = 0

    def get_valid_token(self):
        return "token"


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = requests.structures.CaseInsensitiveDict(headers or {})
        self.text = str(self._body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code}", response=self)


@pytest.fixture
def http(monkeypatch):
    """Patch requests.request; queue responses in `http.responses`, inspect `http.sent`."""
    class Http:
        responses = []
        sent = []

    def fake_request(method, url, headers=None, **kw):
        Http.sent.append((method, url))
        return Http.responses.pop(0) if Http.responses else FakeResponse()

    Http.responses, Http.sent = [], []
    monkeypatch.setattr(tado_client.requests, "request", fake_request)
    return Http


def client_with_home():
    c = TadoClient(FakeAuth())
    c.home_id = 42
    return c


@pytest.mark.parametrize("ratelimit,legacy,expected", [
    ('"perday";r=19950;t=3600', None, 19950),
    ("r=5", None, 5),
    ('"perday";q=20000', None, None),        # a policy, not a remaining count
    ("garbage", None, None),
    (None, "42", 42),
    (None, "lots", None),
    (None, None, None),
])
def test_parse_rate_limit_remaining(ratelimit, legacy, expected):
    assert parse_rate_limit_remaining(ratelimit, legacy) == expected


def test_counts_calls_per_endpoint_and_captures_rate_limit_headers(http):
    c = client_with_home()
    http.responses = [
        FakeResponse(body={"setpoint": 30}),
        FakeResponse(body={"setpoint": 30}, headers={"RateLimit": '"perday";r=99;t=600',
                                                      "RateLimit-Policy": '"perday";q=100'}),
        FakeResponse(body={"outsideTemperature": {}}),
    ]
    c.get_dhw_state()
    c.get_dhw_state()
    c.get_weather()
    summary = c.usage_summary()
    assert summary["calls"] == 3
    assert summary["endpoints"] == {"dhw_state": 2, "weather": 1}
    assert summary["remaining"] == 99 and summary["policy"] == '"perday";q=100'   # kept when a later response omits them


def test_reset_usage_clears_counts_and_auth_refreshes(http):
    c = client_with_home()
    c.get_dhw_state()
    c.auth.refreshes = 3
    c.rate_limit_remaining = 10
    c.reset_usage()
    assert c.usage_summary() == {"calls": 0, "endpoints": {}, "auth_refreshes": 0, "remaining": None, "policy": None}


def test_rate_limit_is_logged_clearly_and_raises(http, caplog):
    c = client_with_home()
    http.responses = [FakeResponse(status=429, body={"errors": [{"code": "rateLimited"}]},
                                   headers={"RateLimit": '"perday";r=0;t=3600'})]
    with pytest.raises(requests.exceptions.HTTPError):
        c.get_dhw_state()
    assert "rate limited (429)" in caplog.text and "API ERROR" not in caplog.text
    assert c.usage_summary()["calls"] == 1 and c.rate_limit_remaining == 0


def test_ensure_home_id_uses_config_without_calling_me(http):
    c = TadoClient(FakeAuth())
    assert c.ensure_home_id(123) == 123
    assert c.ensure_home_id("456") == 456
    assert http.sent == []


def test_ensure_home_id_discovers_once_and_caches(http, caplog):
    caplog.set_level(logging.WARNING)
    c = TadoClient(FakeAuth())
    http.responses = [FakeResponse(body={"homes": [{"id": 7}]})]
    assert c.ensure_home_id("not-a-number") == 7       # bad config value: warn and discover
    assert c.ensure_home_id(None) == 7                 # cached: no second /me
    assert [url for _, url in http.sent] == [TadoClient.ME_URL]
    assert c.usage_summary()["endpoints"] == {"me": 1}
    assert "preferences.home_id" in caplog.text
