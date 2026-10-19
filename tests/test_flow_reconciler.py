from datetime import timedelta

import pytest

from conftest import CURVE_RAW, NOW, FakeFlowClient, FakeTracker
from flow_reconciler import (
    FlowConfig, decide, interpolate, outdoor_from_weather, parse_flow_config, reconcile_flow,
)

CFG = parse_flow_config(CURVE_RAW)


# --- config -----------------------------------------------------------------

@pytest.mark.parametrize("raw", [None, {}, {"enabled": False, "curve": CURVE_RAW["curve"]}])
def test_parse_returns_none_when_absent_or_disabled(raw):
    assert parse_flow_config(raw) is None


def test_parse_sorts_curve_and_applies_defaults():
    cfg = parse_flow_config(CURVE_RAW)
    assert cfg.curve == ((-3.0, 75.0), (0.0, 70.0), (5.0, 68.0), (10.0, 64.0))
    assert cfg.dry_run is False
    assert cfg.min_change_c == 2
    assert cfg.override_grace_minutes == 720


@pytest.mark.parametrize("curve", [
    [],
    [{"outdoor_c": 5}],
    [{"outdoor_c": "warm", "slider_c": 60}],
    [{"outdoor_c": 5, "slider_c": 60}, {"outdoor_c": 5, "slider_c": 65}],
])
def test_parse_rejects_bad_curves(curve):
    with pytest.raises(ValueError):
        parse_flow_config({"enabled": True, "curve": curve})


# --- interpolation ----------------------------------------------------------

@pytest.mark.parametrize("outdoor, expected", [
    (-10, 75),   # flat beyond the cold end
    (-3, 75),
    (-1.5, 73),  # 72.5 rounds half-up, not to even
    (0, 70),
    (2.5, 69),
    (5, 68),
    (7.5, 66),
    (9.8, 64),   # 64.16
    (10, 64),
    (18, 64),    # flat beyond the warm end
])
def test_interpolate(outdoor, expected):
    assert interpolate(CFG.curve, outdoor) == expected


def test_interpolate_single_point_is_flat():
    assert interpolate(((0.0, 70.0),), -5) == 70
    assert interpolate(((0.0, 70.0),), 15) == 70


# --- weather ----------------------------------------------------------------

def test_outdoor_from_weather_fresh_stale_missing():
    fresh = {"outsideTemperature": {"celsius": 9.79, "timestamp": "2026-10-19T19:56:16.851Z"}}
    assert outdoor_from_weather(fresh, NOW) == pytest.approx(9.79)
    stale = {"outsideTemperature": {"celsius": 9.79, "timestamp": "2026-10-19T17:00:00Z"}}
    assert outdoor_from_weather(stale, NOW) is None
    assert outdoor_from_weather({}, NOW) is None
    assert outdoor_from_weather(None, NOW) is None


# --- decide -----------------------------------------------------------------

def test_decide_within_deadband_no_state_change():
    d = decide(64, 65, {"last_applied_temp": 64}, NOW, CFG)
    assert (d.action, d.reason, d.new_state) == ("noop", "within-deadband", None)


def test_decide_within_deadband_adopts_unknown_value():
    # Bootstrap where the slider already sits near the curve: adopt, don't write.
    d = decide(65, 64, {}, NOW, CFG)
    assert (d.action, d.reason) == ("noop", "adopted")
    assert d.new_state["last_applied_temp"] == 65


def test_decide_adopt_after_crash_between_patch_and_save():
    # We PATCHed 64 but died before saving; tracker still says 70.
    d = decide(64, 64, {"last_applied_temp": 70}, NOW, CFG)
    assert (d.action, d.reason) == ("noop", "adopted")
    assert d.new_state == {"last_applied_temp": 64, "last_applied_at": NOW.isoformat()}


def test_decide_adopt_clears_override():
    state = {"last_applied_temp": 64, "override_since": NOW.isoformat(), "override_temp": 69}
    d = decide(69, 68, state, NOW, CFG)
    assert d.reason == "adopted"
    assert "override_since" not in d.new_state


def test_decide_bootstrap_applies():
    d = decide(70, 64, {}, NOW, CFG)
    assert (d.action, d.reason) == ("apply", "bootstrap")
    assert d.new_state["last_applied_temp"] == 64


def test_decide_follows_curve_when_slider_is_ours():
    d = decide(64, 68, {"last_applied_temp": 64}, NOW, CFG)
    assert (d.action, d.reason) == ("apply", "curve")


def test_decide_detects_override():
    d = decide(73, 64, {"last_applied_temp": 64, "last_applied_at": "x"}, NOW, CFG)
    assert (d.action, d.reason) == ("noop", "override-detected")
    assert d.new_state == {"last_applied_temp": 64, "last_applied_at": "x",
                           "override_since": NOW.isoformat(), "override_temp": 73}


def test_decide_override_in_grace_then_expired():
    since = NOW - timedelta(minutes=100)
    state = {"last_applied_temp": 64, "override_since": since.isoformat(), "override_temp": 73}
    d = decide(73, 64, state, NOW, CFG)
    assert (d.action, d.reason, d.new_state) == ("noop", "override-in-grace", None)

    later = since + timedelta(minutes=720)
    d = decide(73, 64, state, later, CFG)
    assert (d.action, d.reason) == ("apply", "override-expired")
    assert d.new_state == {"last_applied_temp": 64, "last_applied_at": later.isoformat()}


def test_decide_override_value_changes_restarts_clock():
    since = NOW - timedelta(minutes=700)
    state = {"last_applied_temp": 64, "override_since": since.isoformat(), "override_temp": 73}
    d = decide(75, 64, state, NOW, CFG)
    assert d.reason == "override-detected"
    assert d.new_state["override_since"] == NOW.isoformat()
    assert d.new_state["override_temp"] == 75


# --- reconcile_flow ---------------------------------------------------------

def _run(client, tracker, cfg=CFG):
    flow = client.get_flow_temperature_optimization()
    weather = client.get_weather()
    return reconcile_flow(client, tracker, cfg, flow, weather, NOW)


def test_reconcile_applies_saves_before_verify(no_sleep):
    events = []
    client = FakeFlowClient(slider=70, events=events)
    tracker = FakeTracker(events=events)
    result = _run(client, tracker)

    assert result["action"] == "apply" and result["reason"] == "bootstrap"
    assert result["target"] == 64 and result["current"] == 70
    assert client.writes == [64]
    kinds = [e[0] for e in events]
    # get (initial), patch, save, get (verify) -- the save must precede the verify read.
    assert kinds == ["get_flow", "patch", "save", "get_flow"]
    assert tracker.state["last_applied_temp"] == 64


def test_reconcile_dry_run_reads_but_never_writes(no_sleep):
    client = FakeFlowClient(slider=70)
    tracker = FakeTracker()
    cfg = FlowConfig(dry_run=True, curve=CFG.curve)
    result = _run(client, tracker, cfg)

    assert result["dry_run"] is True and result["action"] == "apply"
    assert client.writes == []
    assert tracker.saves == []
    assert tracker.loads == 1


def test_reconcile_verify_failure_restores_state_and_raises(no_sleep):
    client = FakeFlowClient(slider=70, ignore_writes=True)
    tracker = FakeTracker(state={"last_applied_temp": 70})
    with pytest.raises(RuntimeError, match="verification failed"):
        _run(client, tracker)
    assert tracker.state == {"last_applied_temp": 70}


def test_reconcile_raises_if_auto_adaptation_turns_off(no_sleep):
    client = FakeFlowClient(slider=70, disable_auto_on_write=True)
    with pytest.raises(RuntimeError, match="auto-adaptation"):
        _run(client, FakeTracker())


def test_reconcile_skips_when_auto_adaptation_off(no_sleep):
    client = FakeFlowClient(slider=70, auto=False)
    tracker = FakeTracker()
    result = _run(client, tracker)
    assert (result["action"], result["reason"]) == ("skip", "auto-adaptation-off")
    assert client.writes == [] and tracker.loads == 0


def test_reconcile_skips_on_stale_weather(no_sleep):
    client = FakeFlowClient(slider=70, weather_age_min=240)
    result = _run(client, FakeTracker())
    assert (result["action"], result["reason"]) == ("skip", "weather-stale")
    assert client.writes == []


def test_reconcile_refuses_without_constraints(no_sleep):
    client = FakeFlowClient(slider=70)
    flow = client.get_flow_temperature_optimization()
    client.flow_min = client.flow_max = None
    with pytest.raises(RuntimeError, match="constraints unknown"):
        reconcile_flow(client, FakeTracker(), CFG, flow, client.get_weather(), NOW)


def test_reconcile_noop_persists_override_detection(no_sleep):
    client = FakeFlowClient(slider=73)
    tracker = FakeTracker(state={"last_applied_temp": 64})
    result = _run(client, tracker)
    assert result["reason"] == "override-detected"
    assert result["override_since"] == NOW.isoformat()
    assert tracker.state["override_temp"] == 73
    assert client.writes == []


def test_reconcile_within_deadband_writes_nothing(no_sleep):
    client = FakeFlowClient(slider=64)
    tracker = FakeTracker(state={"last_applied_temp": 64})
    result = _run(client, tracker)
    assert result["reason"] == "within-deadband"
    assert client.writes == [] and tracker.saves == []


def test_reconcile_clamps_target_to_constraints(no_sleep):
    client = FakeFlowClient(slider=70, outdoor_c=-20)
    cfg = FlowConfig(dry_run=False, curve=((-10.0, 90.0), (10.0, 64.0)))
    result = _run(client, FakeTracker(state={"last_applied_temp": 70}), cfg)
    assert result["target"] == 82
    assert client.writes == [82]
