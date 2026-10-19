import json
import logging
from datetime import datetime, timedelta, timezone

import pytest

from conftest import CURVE_RAW, FakeFlowClient, FakeTracker
import main
import telemetry
from config_manager import ConfigManager

SLOW_EVENT = {"scheduled_time": "2026-10-19T21:00:00Z"}   # first tick of a 15-min slot
FAST_EVENT = {"scheduled_time": "2026-10-19T21:05:00Z"}


class FakeContext:
    def __init__(self, remaining_ms=100_000):
        self.remaining_ms = remaining_ms

    def get_remaining_time_in_millis(self):
        return self.remaining_ms


class FakeSSM:
    """Just enough of boto3's SSM client for ConfigManager."""

    def __init__(self, value):
        self.value = value

    def get_parameter(self, Name):
        return {"Parameter": {"Value": self.value}}


def config_mgr(config):
    import yaml
    mgr = ConfigManager(config_param_name="/test/config", ssm_client=FakeSSM(yaml.safe_dump(config)))
    return mgr


# A schedule with one midnight event every day, so the ruling DHW target is always 50.
DHW_ONLY = {
    "preferences": {"timezone": "Europe/London", "override_grace_minutes": 60},
    "schedule": [{"name": "always", "days": ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"],
                  "time": "00:00", "temperature": 50}],
}


class FakeClient(FakeFlowClient):
    """Fake covering both the DHW and the flow/telemetry endpoints."""

    def __init__(self, dhw_setpoint=50, **kw):
        super().__init__(**kw)
        self.dhw_setpoint = dhw_setpoint
        self.dhw_writes = []

    def get_dhw_state(self):
        return {"setpoint": self.dhw_setpoint}

    def set_dhw_temperature(self, temp):
        self.dhw_writes.append(temp)
        self.dhw_setpoint = temp
        return temp


@pytest.fixture
def wired(monkeypatch, no_sleep):
    """Patch main._bootstrap to hand back fakes; returns a builder."""
    def build(config, client=None, dhw_tracker=None, flow_tracker=None):
        client = client or FakeClient()
        dhw_tracker = dhw_tracker or FakeTracker()
        flow_tracker = flow_tracker or FakeTracker()
        mgr = config_mgr(config)
        monkeypatch.setattr(main, "_bootstrap", lambda: (client, mgr, dhw_tracker, flow_tracker))
        return client, dhw_tracker, flow_tracker
    return build


def telemetry_records(caplog):
    out = []
    for r in caplog.records:
        msg = r.getMessage()
        if msg.startswith(telemetry.MARKER):
            out.append(json.loads(msg[len(telemetry.MARKER):]))
    return out


# --- gating -----------------------------------------------------------------

def test_is_slow_tick_exactly_once_per_slot_at_any_offset():
    for offset_s in (0, 1, 133, 299, 300, 451, 899):
        start = datetime(2026, 10, 19, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=offset_s)
        ticks = [start + timedelta(minutes=5 * i) for i in range(288)]
        slow = [t for t in ticks if main.is_slow_tick(t)]
        assert len(slow) == 96, offset_s
        slots = {(t.hour, t.minute // 15) for t in slow}
        assert len(slots) == 96, offset_s


def test_slow_tick_time_parses_scheduler_input_and_falls_back():
    assert main.slow_tick_time(SLOW_EVENT) == datetime(2026, 10, 19, 21, 0, tzinfo=timezone.utc)
    for event in (None, {}, {"scheduled_time": "garbage"}):
        assert main.slow_tick_time(event).tzinfo is not None


# --- handler fault isolation -------------------------------------------------

def test_missing_blocks_means_dhw_only(wired):
    client, _, _ = wired(DHW_ONLY)
    result = main.handler(SLOW_EVENT, FakeContext())
    assert result["dhw"]["status"] == "no-op"
    assert result["slow"]["status"] == "skipped"
    assert client.events == []  # no flow/weather calls at all


def test_fast_tick_skips_slow_loop(wired, caplog):
    caplog.set_level(logging.INFO)
    client, _, _ = wired({**DHW_ONLY, "telemetry": {"enabled": True}, "flow_temperature": CURVE_RAW})
    result = main.handler(FAST_EVENT, FakeContext())
    assert "slow" not in result
    assert telemetry_records(caplog) == []


def test_slow_tick_runs_flow_and_emits_telemetry(wired, caplog):
    caplog.set_level(logging.INFO)
    client, _, flow_tracker = wired({**DHW_ONLY, "telemetry": {"enabled": True}, "flow_temperature": CURVE_RAW})
    result = main.handler(SLOW_EVENT, FakeContext())

    assert result["slow"]["flow"]["action"] == "apply"
    assert client.writes == [64]
    assert flow_tracker.state["last_applied_temp"] == 64
    (rec,) = telemetry_records(caplog)
    assert rec["slider"] == 70 and rec["effective"] == 59 and rec["auto"] is True
    assert rec["outdoor_c"] == pytest.approx(9.8)
    assert [r["n"] for r in rec["rooms"]] == ["Living Room", "Office"]
    assert rec["flow"]["target"] == 64
    assert rec["errors"] == {}


def test_dhw_failure_still_runs_slow_loop_then_raises(wired, monkeypatch):
    client, _, _ = wired({**DHW_ONLY, "flow_temperature": CURVE_RAW})
    monkeypatch.setattr(main, "reconcile", lambda *a: (_ for _ in ()).throw(RuntimeError("dhw boom")))
    with pytest.raises(RuntimeError, match="dhw: dhw boom"):
        main.handler(SLOW_EVENT, FakeContext())
    assert client.writes == [64]  # flow still reconciled


def test_flow_failure_keeps_dhw_result_and_raises(wired, caplog):
    caplog.set_level(logging.INFO)
    client = FakeClient(dhw_setpoint=35)
    client.fail.add("weather")
    client, dhw_tracker, _ = wired({**DHW_ONLY, "flow_temperature": CURVE_RAW}, client=client)
    with pytest.raises(RuntimeError, match="flow: Flow inputs unavailable"):
        main.handler(SLOW_EVENT, FakeContext())
    assert client.dhw_writes == [50]                  # DHW applied regardless
    assert dhw_tracker.state["last_applied_temp"] == 50
    (rec,) = telemetry_records(caplog)                # record still emitted, with the error
    assert rec["flow"]["action"] == "error" and "weather" in rec["errors"]


def test_bad_flow_config_raises_but_dhw_and_telemetry_run(wired, caplog):
    caplog.set_level(logging.INFO)
    bad = {"enabled": True, "curve": []}
    client, _, _ = wired({**DHW_ONLY, "telemetry": {"enabled": True}, "flow_temperature": bad})
    with pytest.raises(RuntimeError, match="flow: flow_temperature.curve"):
        main.handler(SLOW_EVENT, FakeContext())
    (rec,) = telemetry_records(caplog)
    assert "flow_config" in rec["errors"] and len(rec["rooms"]) == 2
    assert client.writes == []


def test_rooms_failure_is_logged_not_raised(wired, caplog):
    caplog.set_level(logging.INFO)
    client = FakeClient()
    client.fail.add("rooms")
    wired({**DHW_ONLY, "telemetry": {"enabled": True}}, client=client)
    result = main.handler(SLOW_EVENT, FakeContext())
    assert result["slow"]["errors"] == ["rooms"]
    (rec,) = telemetry_records(caplog)
    assert "rooms" not in rec and rec["slider"] == 70


def test_low_remaining_time_skips_slow_loop(wired):
    client, _, _ = wired({**DHW_ONLY, "flow_temperature": CURVE_RAW})
    result = main.handler(SLOW_EVENT, FakeContext(remaining_ms=10_000))
    assert result["slow"]["reason"] == "low remaining time"
    assert client.writes == []


# --- DHW regression (config load moved into the handler) ----------------------

def test_dhw_bootstrap_applies(wired):
    client, tracker, _ = wired(DHW_ONLY, client=FakeClient(dhw_setpoint=35))
    result = main.handler(FAST_EVENT, FakeContext())
    assert result["dhw"]["status"] == "applied" and client.dhw_writes == [50]
    assert tracker.state["last_applied_temp"] == 50


def test_dhw_respects_manual_boost(wired):
    client, tracker, _ = wired(DHW_ONLY, client=FakeClient(dhw_setpoint=60),
                               dhw_tracker=FakeTracker({"last_applied_temp": 50}))
    result = main.handler(FAST_EVENT, FakeContext())
    assert result["dhw"]["reason"] == "override-detected"
    assert client.dhw_writes == []
    assert tracker.state["override_temp"] == 60
