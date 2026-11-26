import json
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

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
        # main.handler reads the real clock, so the fake weather must be fresh relative
        # to it; the fixed conftest NOW makes the reconciler skip with "weather-stale".
        kw.setdefault("now", datetime.now(timezone.utc))
        super().__init__(**kw)
        self.dhw_setpoint = dhw_setpoint
        self.dhw_writes = []

    def get_dhw_state(self):
        self._call("dhw_state")
        self._maybe_fail("dhw_state")
        return {"setpoint": self.dhw_setpoint}

    def set_dhw_temperature(self, temp):
        self._call("dhw_set")
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


# --- lean mode: pure pieces ----------------------------------------------------

LON = ZoneInfo("Europe/London")

# The DHW schedule from config/config.yaml: pre-load, off, evening top-up, off.
DHW_REAL = {
    "preferences": {"timezone": "Europe/London", "override_grace_minutes": 60},
    "schedule": [
        {"name": "Pre-load ON", "days": ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"], "time": "04:45", "temperature": 60},
        {"name": "Pre-load OFF", "days": ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"], "time": "05:30", "temperature": 30},
        {"name": "Top-up ON", "days": ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"], "time": "16:00", "temperature": 50},
        {"name": "Top-up OFF", "days": ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"], "time": "16:30", "temperature": 30},
    ],
}


def schedule_map(config=DHW_REAL):
    mgr = config_mgr(config)
    mgr.load_config()
    return mgr.schedule_map


def local(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=LON)


# A tracker that applied yesterday evening's event and nothing since.
HANDLED_YESTERDAY = {"last_applied_temp": 30, "last_applied_at": local(2026, 11, 25, 16, 32, 24).isoformat()}


@pytest.mark.parametrize("slot", [15, 60, 120])
def test_is_slot_tick_exactly_once_per_slot_at_any_offset(slot):
    for offset_s in (0, 1, 133, 299, 300, 451, 899):
        start = datetime(2026, 10, 19, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=offset_s)
        ticks = [start + timedelta(minutes=5 * i) for i in range(288)]
        hits = [t for t in ticks if main.is_slot_tick(t, slot)]
        assert len(hits) == 1440 // slot, (slot, offset_s)
        assert len({(t.hour * 60 + t.minute) // slot for t in hits}) == 1440 // slot


@pytest.mark.parametrize("raw", [None, False, {"enabled": False}])
def test_parse_lean_config_off(raw, caplog):
    assert main.parse_lean_config(raw) is None
    assert caplog.text == ""


def test_parse_lean_config_defaults_and_overrides():
    assert main.parse_lean_config(True) == main.LeanConfig()
    assert main.parse_lean_config({"enabled": True}) == main.LeanConfig(60, 60, 25)
    cfg = main.parse_lean_config({"enabled": True, "dhw_check_minutes": "120", "flow_check_minutes": 30, "flow_reserve": 10})
    assert cfg == main.LeanConfig(dhw_check_minutes=120, flow_check_minutes=30, flow_reserve=10)


@pytest.mark.parametrize("key,value", [
    ("dhw_check_minutes", 7), ("dhw_check_minutes", 50), ("dhw_check_minutes", 0),
    ("flow_check_minutes", "hourly"), ("flow_reserve", -1), ("flow_reserve", "lots"),
])
def test_parse_lean_config_bad_values_fall_back_with_warning(key, value, caplog):
    cfg = main.parse_lean_config({"enabled": True, key: value})
    defaults = {"dhw_check_minutes": 60, "flow_check_minutes": 60, "flow_reserve": 25}
    assert cfg is not None and getattr(cfg, key) == defaults[key]
    assert f"lean_mode.{key}" in caplog.text


@pytest.mark.parametrize("raw", [{"enabled": "true"}, {"dhw_check_minutes": 30}, "yes"])
def test_parse_lean_config_warns_when_a_block_is_present_but_not_enabled(raw, caplog):
    assert main.parse_lean_config(raw) is None
    assert "lean mode is OFF" in caplog.text   # staying silently off would blow the free tier


def due(tick, tracked=HANDLED_YESTERDAY, grace=60, check=60, sched=None):
    return main.dhw_due(sched or schedule_map(), tick, tracked, grace, check)


def test_dhw_due_bootstrap_when_tracker_empty():
    assert due(local(2026, 11, 26, 13, 7), tracked={}) == (True, "bootstrap")


def test_dhw_due_on_the_event_tick():
    assert due(local(2026, 11, 26, 4, 47, 24)) == (True, "event")


def test_dhw_due_retries_an_unhandled_event_then_gives_up():
    assert due(local(2026, 11, 26, 4, 52)) == (True, "event-retry")
    assert due(local(2026, 11, 26, 4, 57)) == (True, "event-retry")
    assert due(local(2026, 11, 26, 5, 7)) == (False, "not-due")   # retry window over; next check at 06:00


def test_dhw_no_retries_while_a_boost_is_in_grace():
    # Boost at 16:10; the 16:30 event lands in its grace window, so reconcile would leave
    # it alone -- retrying would only spend reads. grace-expiry (17:10) takes over.
    boosted = {**HANDLED_YESTERDAY, "override_since": local(2026, 11, 26, 16, 10).isoformat(), "override_temp": 60}
    assert due(local(2026, 11, 26, 16, 32), tracked=boosted) == (True, "event")
    assert due(local(2026, 11, 26, 16, 37), tracked=boosted) == (False, "not-due")
    assert due(local(2026, 11, 26, 17, 12), tracked=boosted) == (True, "grace-expiry")


def test_dhw_not_due_once_the_event_is_handled():
    applied = {"last_applied_temp": 60, "last_applied_at": local(2026, 11, 26, 4, 47, 30).isoformat()}
    assert due(local(2026, 11, 26, 4, 52), tracked=applied) == (False, "not-due")
    boosted = {**HANDLED_YESTERDAY, "override_since": local(2026, 11, 26, 4, 47, 30).isoformat(), "override_temp": 65}
    assert due(local(2026, 11, 26, 4, 52), tracked=boosted) == (False, "not-due")


def test_dhw_due_when_override_grace_expires():
    since = local(2026, 11, 26, 10, 2, 24)
    boosted = {**HANDLED_YESTERDAY, "override_since": since.isoformat(), "override_temp": 60}
    assert due(local(2026, 11, 26, 10, 57), tracked=boosted) == (False, "not-due")
    assert due(local(2026, 11, 26, 11, 7), tracked=boosted) == (True, "grace-expiry")
    assert due(local(2026, 11, 26, 11, 12), tracked=boosted) == (False, "not-due")


def test_dhw_due_on_the_periodic_check_only():
    assert due(local(2026, 11, 26, 13, 2)) == (True, "check")
    assert due(local(2026, 11, 26, 13, 7)) == (False, "not-due")
    assert due(local(2026, 11, 26, 13, 2), check=120) == (False, "not-due")   # 13:00 isn't a 2-hour slot
    assert due(local(2026, 11, 26, 14, 2), check=120) == (True, "check")


def test_dhw_due_retry_window_crosses_midnight_via_yesterdays_event():
    late = {"preferences": {"timezone": "Europe/London"},
            "schedule": [{"name": "late", "days": ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"],
                          "time": "23:55", "temperature": 40}]}
    tracked = {"last_applied_temp": 40, "last_applied_at": local(2026, 11, 24, 23, 57).isoformat()}
    assert due(local(2026, 11, 26, 0, 7), tracked=tracked, sched=schedule_map(late)) == (True, "event-retry")


@pytest.mark.parametrize("utc_tick", [
    datetime(2026, 10, 25, 4, 47, tzinfo=timezone.utc),   # clocks went back at 02:00 BST: 04:45 local = 04:45 UTC
    datetime(2026, 3, 29, 3, 47, tzinfo=timezone.utc),    # clocks went forward at 01:00 GMT: 04:45 local = 03:45 UTC
])
def test_dhw_due_event_tick_across_dst_changes(utc_tick):
    tracked = {"last_applied_temp": 30, "last_applied_at": (utc_tick - timedelta(hours=12)).isoformat()}
    assert due(utc_tick.astimezone(LON), tracked=tracked) == (True, "event")


# --- lean mode: handler ---------------------------------------------------------

LEAN = {**DHW_ONLY, "preferences": {**DHW_ONLY["preferences"], "home_id": 123},
        "lean_mode": {"enabled": True}, "telemetry": {"enabled": True}, "flow_temperature": CURVE_RAW}
HOURLY_EVENT = SLOW_EVENT                                  # 21:00Z: first tick of an hourly slot
MID_HOUR_EVENT = {"scheduled_time": "2026-10-19T21:45:00Z"}   # a 15-min slot tick, but not hourly
DHW_DONE = {"last_applied_temp": 50, "last_applied_at": "2026-10-19T00:02:00+01:00"}


def usage_records(caplog):
    return [json.loads(r.getMessage()[len(main.USAGE_MARKER):]) for r in caplog.records
            if r.getMessage().startswith(main.USAGE_MARKER)]


def test_lean_mid_hour_tick_makes_no_api_calls(wired, caplog):
    caplog.set_level(logging.INFO)
    client, _, _ = wired(LEAN, dhw_tracker=FakeTracker(DHW_DONE))
    result = main.handler(MID_HOUR_EVENT, FakeContext())
    assert result == {"dhw": {"status": "skipped", "reason": "lean-not-due"}}
    assert client.calls == []
    assert usage_records(caplog) == [] and telemetry_records(caplog) == []


def test_lean_event_tick_applies_dhw_with_verify(wired):
    client = FakeClient(dhw_setpoint=30)
    client, tracker, _ = wired({**LEAN, "flow_temperature": {"enabled": False}}, client=client,
                               dhw_tracker=FakeTracker({"last_applied_temp": 30, "last_applied_at": "2026-10-18T16:32:00+01:00"}))
    result = main.handler({"scheduled_time": "2026-10-19T23:02:00Z"}, FakeContext())   # 00:02 BST, just after the 00:00 event
    assert result["dhw"]["status"] == "applied" and result["dhw"]["lean_reason"] == "event"
    assert client.calls == ["dhw_state", "dhw_set", "dhw_state"]
    assert tracker.state["last_applied_temp"] == 50
    assert result["slow"]["status"] == "skipped"            # flow off, and lean never fetches rooms


def test_lean_runs_dhw_anyway_if_the_gate_cannot_read_the_tracker(wired):
    class FlakyTracker(FakeTracker):
        def load(self):
            if not self.loads:                 # the gate's read fails; reconcile's own read works
                self.loads += 1
                raise RuntimeError("ssm boom")
            return super().load()

    client, _, _ = wired(LEAN, dhw_tracker=FlakyTracker(DHW_DONE))
    result = main.handler(MID_HOUR_EVENT, FakeContext())   # a tick the gate would normally skip
    assert result["dhw"]["lean_reason"] == "gate-error" and "dhw_state" in client.calls


def test_lean_hourly_tick_drops_rooms_and_still_logs_a_flow_record(wired, caplog):
    caplog.set_level(logging.INFO)
    client, _, flow_tracker = wired(LEAN, dhw_tracker=FakeTracker(DHW_DONE))
    result = main.handler(HOURLY_EVENT, FakeContext())
    assert result["dhw"]["lean_reason"] == "check"
    assert client.calls == ["dhw_state", "weather", "flow", "flow_set", "flow"]   # bootstrap write + verify; no rooms
    assert flow_tracker.state["last_applied_temp"] == 64
    (rec,) = telemetry_records(caplog)
    assert rec["lean"] is True and "rooms" not in rec and rec["flow"]["action"] == "apply"
    (usage,) = usage_records(caplog)
    assert usage["calls"] == 5 and usage["lean"] is True and "me" not in usage["endpoints"]


def test_lean_skips_the_flow_read_when_the_curve_has_not_moved(wired, caplog):
    caplog.set_level(logging.INFO)
    client, _, _ = wired(LEAN, dhw_tracker=FakeTracker(DHW_DONE), flow_tracker=FakeTracker({"last_applied_temp": 64}))
    result = main.handler(HOURLY_EVENT, FakeContext())
    assert client.calls == ["dhw_state", "weather"]
    assert result["slow"]["flow"]["reason"] == "lean-unchanged"
    (rec,) = telemetry_records(caplog)
    assert rec["slider"] is None and rec["flow"]["target"] == 64 and rec["outdoor_c"] == pytest.approx(9.8)


def test_lean_reads_and_writes_once_the_curve_moves(wired):
    client = FakeClient(slider=60)
    client, _, flow_tracker = wired(LEAN, client=client, dhw_tracker=FakeTracker(DHW_DONE),
                                    flow_tracker=FakeTracker({"last_applied_temp": 60}))
    result = main.handler(HOURLY_EVENT, FakeContext())
    assert result["slow"]["flow"]["reason"] == "curve" and client.writes == [64]
    assert flow_tracker.state["last_applied_temp"] == 64


def test_lean_always_reads_while_an_override_is_tracked(wired):
    override = {"last_applied_temp": 64, "override_since": datetime.now(timezone.utc).isoformat(), "override_temp": 62}
    client, _, _ = wired(LEAN, client=FakeClient(slider=62), dhw_tracker=FakeTracker(DHW_DONE),
                         flow_tracker=FakeTracker(override))
    result = main.handler(HOURLY_EVENT, FakeContext())
    assert "flow" in client.calls and client.writes == []
    assert result["slow"]["flow"]["reason"] == "override-in-grace"   # the read lets the grace clock run


def test_lean_leaves_the_last_requests_for_dhw(wired):
    client = FakeClient(quota_remaining=20)                  # below the default reserve of 25
    client, _, _ = wired(LEAN, client=client, dhw_tracker=FakeTracker(DHW_DONE))
    result = main.handler(HOURLY_EVENT, FakeContext())
    assert client.calls == ["dhw_state"]                     # DHW ran; its response showed the quota, so no weather call
    assert result["slow"]["flow"] == {"dry_run": False, "action": "skip", "reason": "budget-reserve", "remaining": 19}


def test_lean_reserve_also_applies_once_the_weather_response_reports_it(wired):
    client = FakeClient(quota_remaining=20)
    config = {**LEAN, "lean_mode": {"enabled": True, "dhw_check_minutes": 120}}
    client, _, _ = wired(config, client=client, dhw_tracker=FakeTracker(DHW_DONE))
    result = main.handler({"scheduled_time": "2026-10-19T22:00:00Z"}, FakeContext())   # 23:00 BST: no DHW check
    assert client.calls == ["weather"]
    assert result["slow"]["flow"]["reason"] == "budget-reserve"


def test_lean_weather_failure_raises_without_reading_flow(wired, caplog):
    caplog.set_level(logging.INFO)
    client = FakeClient()
    client.fail.add("weather")
    client, _, _ = wired(LEAN, client=client, dhw_tracker=FakeTracker(DHW_DONE))
    with pytest.raises(RuntimeError, match="flow: Flow inputs unavailable"):
        main.handler(HOURLY_EVENT, FakeContext())
    assert client.calls == ["dhw_state", "weather"]
    (rec,) = telemetry_records(caplog)
    assert "weather" in rec["errors"] and rec["flow"]["reason"] == "inputs-unavailable"


def test_lean_stale_weather_skips_without_reading_flow(wired):
    client, _, _ = wired(LEAN, client=FakeClient(weather_age_min=300), dhw_tracker=FakeTracker(DHW_DONE))
    result = main.handler(HOURLY_EVENT, FakeContext())
    assert client.calls == ["dhw_state", "weather"]
    assert result["slow"]["flow"]["reason"] == "weather-stale"


def test_lean_bad_flow_config_raises_without_any_flow_calls(wired, caplog):
    caplog.set_level(logging.INFO)
    client, _, _ = wired({**LEAN, "flow_temperature": {"enabled": True, "curve": []}}, dhw_tracker=FakeTracker(DHW_DONE))
    with pytest.raises(RuntimeError, match="flow: flow_temperature.curve"):
        main.handler(HOURLY_EVENT, FakeContext())
    assert client.calls == ["dhw_state"]
    (rec,) = telemetry_records(caplog)
    assert "flow_config" in rec["errors"]


def test_usage_is_logged_even_when_dhw_fails(wired, caplog):
    caplog.set_level(logging.INFO)
    client = FakeClient()
    client.fail.add("dhw_state")
    client, _, _ = wired(LEAN, client=client, dhw_tracker=FakeTracker(DHW_DONE))
    with pytest.raises(RuntimeError, match="dhw: dhw_state boom"):
        main.handler(HOURLY_EVENT, FakeContext())
    (usage,) = usage_records(caplog)
    assert usage["endpoints"]["dhw_state"] == 1


def test_failed_home_id_lookup_runs_once_and_skips_the_tick(wired):
    client = FakeClient()
    client.fail.add("me")
    no_id = {**LEAN, "preferences": DHW_ONLY["preferences"]}
    client, _, _ = wired(no_id, client=client, dhw_tracker=FakeTracker(DHW_DONE))
    with pytest.raises(RuntimeError, match="home: me boom") as err:
        main.handler(HOURLY_EVENT, FakeContext())
    assert client.calls == ["me"]                            # not retried for the slow loop
    assert "dhw:" not in str(err.value) and "flow:" not in str(err.value)


def test_lean_without_home_id_looks_it_up_and_warns(wired, caplog):
    caplog.set_level(logging.INFO)
    no_id = {**LEAN, "preferences": DHW_ONLY["preferences"]}
    client, _, _ = wired(no_id, dhw_tracker=FakeTracker(DHW_DONE), flow_tracker=FakeTracker({"last_applied_temp": 64}))
    main.handler(HOURLY_EVENT, FakeContext())
    assert client.calls == ["me", "dhw_state", "weather"]
    assert "preferences.home_id" in caplog.text


def test_lean_off_keeps_the_15_minute_slow_loop_with_rooms(wired, caplog):
    caplog.set_level(logging.INFO)
    client, _, _ = wired({**LEAN, "lean_mode": {"enabled": False}}, dhw_tracker=FakeTracker(DHW_DONE))
    main.handler(MID_HOUR_EVENT, FakeContext())
    assert client.calls == ["dhw_state", "weather", "flow", "rooms", "rooms_and_devices", "flow_set", "flow"]
    (usage,) = usage_records(caplog)
    assert usage["lean"] is False
