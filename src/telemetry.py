import json
import logging

logger = logging.getLogger(__name__)

# One log line per slow-loop run: `TADO_TELEMETRY_V1 {json}`. scripts/flow_report.py
# filters CloudWatch on this marker, so bump the version if the shape changes.
MARKER = "TADO_TELEMETRY_V1"
SCHEMA_VERSION = 1


def _get(obj, *keys):
    for key in keys:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def summarise(weather, flow):
    """Home-level fields for the telemetry record. Missing payloads just yield nulls."""
    return {
        "outdoor_c": _get(weather, "outsideTemperature", "celsius"),
        "outdoor_at": _get(weather, "outsideTemperature", "timestamp"),
        "solar_pct": _get(weather, "solarIntensity", "percentage"),
        "slider": _get(flow, "maxFlowTemperature"),
        "effective": _get(flow, "autoAdaptation", "maxFlowTemperature"),
        "auto": _get(flow, "autoAdaptation", "enabled"),
    }


def project_rooms(rooms, rooms_and_devices=None):
    """Compact per-room view of hops /rooms (+ device types from /roomsAndDevices).

    Keys: t inside temp, h humidity, sp setpoint (None when the room is OFF), pw valve
    heatingPower %, win/man/boost/away flags (Tado object present), dev device types
    (SU04 = room sensor, VA04 = valve; valve-only rooms read temp next to the radiator),
    dis = room or any device not CONNECTED.
    """
    devices_by_room = {
        r.get("roomId"): r.get("devices") or []
        for r in _get(rooms_and_devices, "rooms") or []
    }
    out = []
    for room in rooms or []:
        setting = room.get("setting") or {}
        devices = devices_by_room.get(room.get("id"))
        entry = {
            "id": room.get("id"),
            "n": room.get("name"),
            "t": _get(room, "sensorDataPoints", "insideTemperature", "value"),
            "h": _get(room, "sensorDataPoints", "humidity", "percentage"),
            "sp": _get(setting, "temperature", "value") if setting.get("power") == "ON" else None,
            "pw": _get(room, "heatingPower", "percentage"),
            "win": room.get("openWindow") is not None,
            "man": room.get("manualControlTermination") is not None,
            "boost": room.get("boostMode") is not None,
            "away": room.get("awayMode") is not None or room.get("holidayMode") is not None,
        }
        disconnected = _get(room, "connection", "state") not in (None, "CONNECTED")
        if devices_by_room:
            entry["dev"] = sorted(d.get("type") or "?" for d in devices or [])
            disconnected = disconnected or any(_get(d, "connection", "state") != "CONNECTED" for d in devices or [])
        entry["dis"] = disconnected
        out.append(entry)
    return out


def emit(record):
    logger.info("%s %s", MARKER, json.dumps(record, separators=(",", ":"), ensure_ascii=False, default=str))
