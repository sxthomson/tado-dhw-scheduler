import json
import logging

from conftest import ROOM_LIVING, ROOM_OFFICE, ROOMS_AND_DEVICES
import telemetry


def test_project_rooms_with_devices():
    living, office = telemetry.project_rooms([ROOM_LIVING, ROOM_OFFICE], ROOMS_AND_DEVICES)
    assert living == {
        "id": 7, "n": "Living Room", "t": 20.58, "h": 63, "sp": 20.0, "pw": 0,
        "win": False, "man": False, "boost": False, "away": False,
        "dev": ["SU04", "VA04"], "dis": False,
    }
    assert office["sp"] is None          # power OFF
    assert office["man"] is True
    assert office["dev"] == ["VA04"]     # valve-only room
    assert office["dis"] is True         # its valve is disconnected


def test_project_rooms_without_devices_omits_dev():
    (living,) = telemetry.project_rooms([ROOM_LIVING], None)
    assert "dev" not in living
    assert living["dis"] is False


def test_summarise_handles_missing_payloads():
    assert telemetry.summarise(None, None) == {
        "outdoor_c": None, "outdoor_at": None, "solar_pct": None,
        "slider": None, "effective": None, "auto": None,
    }


def test_emit_writes_one_parseable_line(caplog):
    caplog.set_level(logging.INFO)
    telemetry.emit({"v": 1, "rooms": [{"n": "Kid’s Bedroom"}]})
    (msg,) = [r.getMessage() for r in caplog.records]
    assert msg.startswith(telemetry.MARKER + " ")
    assert json.loads(msg[len(telemetry.MARKER):]) == {"v": 1, "rooms": [{"n": "Kid’s Bedroom"}]}
