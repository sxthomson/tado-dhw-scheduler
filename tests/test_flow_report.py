from datetime import datetime, timedelta, timezone

import flow_report

T0 = datetime(2026, 12, 1, 6, 0, tzinfo=timezone.utc)


def rec(i, rooms, outdoor_c=0.5, slider=70, effective=66, flow=None):
    return {
        "v": 1, "ts": (T0 + timedelta(minutes=15 * i)).isoformat(),
        "outdoor_c": outdoor_c, "slider": slider, "effective": effective,
        "rooms": rooms, "flow": flow or {},
    }


def room(t, sp=20.0, pw=100, rid=1, name="Cold Room", dev=("VA04",), **flags):
    return {"id": rid, "n": name, "t": t, "sp": sp, "pw": pw, "dev": list(dev), "dis": False,
            "win": False, "man": False, "boost": False, "away": False, **flags}


def test_parse_message_extracts_record_from_lambda_log_line():
    line = '[INFO]\t2026-12-01T06:00:00.000Z\treq-id\tTADO_TELEMETRY_V1 {"v":1,"slider":70}\n'
    assert flow_report.parse_message(line) == {"v": 1, "slider": 70}
    assert flow_report.parse_message("Reconcile result: {}") is None


def test_band():
    assert flow_report.band(0.5) == 0
    assert flow_report.band(-0.5) == -2
    assert flow_report.band(9.8) == 8
    assert flow_report.band(None) is None


def test_curve_table_factor_and_counts():
    records = [
        rec(0, [], outdoor_c=9.8, slider=70, effective=59, flow={"action": "apply", "dry_run": False}),
        rec(1, [], outdoor_c=9.6, slider=70, effective=59, flow={"action": "apply", "dry_run": True}),
        rec(2, [], outdoor_c=9.1, slider=73, effective=None, flow={"action": "noop", "reason": "override-detected"}),
    ]
    (row,) = flow_report.curve_table(records)
    assert row["outdoor"] == "+8..+10" and row["samples"] == 3
    assert row["factor"] == 0.8
    assert (row["writes"], row["dry-run"], row["overrides"]) == (1, 1, 1)


def test_room_struggling_needs_a_sustained_run():
    # 5 consecutive starving samples -> samples 4 and 5 count as struggling (2/5 = 40%).
    records = [rec(i, [room(18.0 + 0.1 * i)]) for i in range(5)]
    (row,) = flow_report.room_table(records)
    assert row["struggling%"] == 40
    assert row["warm-up C/h"] == 0.4           # 0.1C per 15 min
    assert row["flags"] == "valve-only"
    assert row["verdict"] == "undersized?"


def test_room_run_broken_by_open_window_and_setpoint_change():
    temps = [18.0] * 6
    rooms = [room(t) for t in temps]
    rooms[2] = room(18.0, win=True)            # excluded sample resets the run
    rooms[5] = room(18.0, sp=21.0)             # setpoint change resets it again
    records = [rec(i, [r]) for i, r in enumerate(rooms)]
    (row,) = flow_report.room_table(records)
    assert row["struggling%"] == 0
    assert row["samples"] == 5


def test_room_holding_median_and_headroom_verdict():
    records = [rec(i, [room(20.1, pw=pw, dev=("SU04", "VA04"), name="Warm Room")]) for i, pw in enumerate([20, 30, 40])]
    (row,) = flow_report.room_table(records)
    assert row["holding valve%"] == 30
    assert row["struggling%"] == 0
    assert row["flags"] == ""
    assert row["verdict"] == "headroom"


def test_rooms_off_or_disconnected_are_excluded():
    records = [rec(0, [room(15.0, sp=None)]), rec(1, [room(15.0, dis=True)])]
    assert flow_report.room_table(records) == []
