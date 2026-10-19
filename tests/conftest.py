import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# src/ is the Lambda code root and imports are flat (`from tado_client import ...`);
# scripts/ holds flow_report.py. Mirror that here.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

NOW = datetime(2026, 10, 19, 21, 0, tzinfo=timezone.utc)


class FakeTracker:
    """In-memory OverrideTracker stand-in. `events` is shared with FakeFlowClient to check ordering."""

    def __init__(self, state=None, events=None):
        self.state = dict(state or {})
        self.saves = []
        self.loads = 0
        self.events = events if events is not None else []

    def load(self):
        self.loads += 1
        return dict(self.state)

    def save(self, state):
        self.state = dict(state)
        self.saves.append(dict(state))
        self.events.append(("save", dict(state)))


class FakeFlowClient:
    """Fake TadoClient for the flow/telemetry side, shaped like the probed hops/my.tado payloads."""

    def __init__(self, slider=70, auto=True, effective=59, outdoor_c=9.8, weather_age_min=10,
                 now=NOW, events=None, ignore_writes=False, disable_auto_on_write=False):
        self.slider = slider
        self.auto = auto
        self.effective = effective
        self.outdoor_c = outdoor_c
        self.weather_at = now - timedelta(minutes=weather_age_min)
        self.events = events if events is not None else []
        self.ignore_writes = ignore_writes
        self.disable_auto_on_write = disable_auto_on_write
        self.writes = []
        self.flow_min = None
        self.flow_max = None
        self.fail = set()  # names of fetches that should raise

    def _maybe_fail(self, name):
        if name in self.fail:
            raise RuntimeError(f"{name} boom")

    def get_flow_temperature_optimization(self):
        self._maybe_fail("flow")
        self.events.append(("get_flow", self.slider))
        self.flow_min, self.flow_max = 20, 82
        return {
            "maxFlowTemperature": self.slider,
            "maxFlowTemperatureConstraints": {"min": 20, "max": 82},
            "autoAdaptation": {"enabled": self.auto, "maxFlowTemperature": self.effective},
        }

    def set_max_flow_temperature(self, value):
        self.events.append(("patch", value))
        self.writes.append(value)
        if not self.ignore_writes:
            self.slider = value
            self.effective = None  # Tado recomputes asynchronously
        if self.disable_auto_on_write:
            self.auto = False
        return value

    def get_weather(self):
        self._maybe_fail("weather")
        return {
            "outsideTemperature": {"celsius": self.outdoor_c, "timestamp": self.weather_at.isoformat().replace("+00:00", "Z")},
            "solarIntensity": {"percentage": 0.0},
        }

    def get_rooms(self):
        self._maybe_fail("rooms")
        return [ROOM_LIVING, ROOM_OFFICE]

    def get_rooms_and_devices(self):
        self._maybe_fail("rooms_and_devices")
        return ROOMS_AND_DEVICES


ROOM_LIVING = {
    "id": 7, "name": "Living Room",
    "sensorDataPoints": {"insideTemperature": {"value": 20.58}, "humidity": {"percentage": 63}},
    "setting": {"power": "ON", "temperature": {"value": 20.0}},
    "manualControlTermination": None, "boostMode": None,
    "heatingPower": {"percentage": 0}, "connection": {"state": "CONNECTED"},
    "openWindow": None, "balanceControl": None, "awayMode": None, "holidayMode": None,
}
ROOM_OFFICE = {
    "id": 12, "name": "Office",
    "sensorDataPoints": {"insideTemperature": {"value": 18.1}, "humidity": {"percentage": 55}},
    "setting": {"power": "OFF", "temperature": None},
    "manualControlTermination": {"type": "MANUAL"}, "boostMode": None,
    "heatingPower": {"percentage": 100}, "connection": {"state": "CONNECTED"},
    "openWindow": None, "balanceControl": None, "awayMode": None, "holidayMode": None,
}
ROOMS_AND_DEVICES = {
    "rooms": [
        {"roomId": 7, "roomName": "Living Room", "devices": [
            {"type": "SU04", "connection": {"state": "CONNECTED"}},
            {"type": "VA04", "connection": {"state": "CONNECTED"}},
        ]},
        {"roomId": 12, "roomName": "Office", "devices": [
            {"type": "VA04", "connection": {"state": "DISCONNECTED"}},
        ]},
    ],
    "otherDevices": [],
}

CURVE_RAW = {
    "enabled": True,
    "curve": [
        {"outdoor_c": 10, "slider_c": 64},
        {"outdoor_c": 5, "slider_c": 68},
        {"outdoor_c": 0, "slider_c": 70},
        {"outdoor_c": -3, "slider_c": 75},
    ],
}


@pytest.fixture
def no_sleep(monkeypatch):
    import flow_reconciler
    import main
    monkeypatch.setattr(flow_reconciler.time, "sleep", lambda s: None)
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
