#!/usr/bin/env python3
"""Summarise the Lambda's telemetry to tune the flow curve and spot undersized radiators.

Reads the `TADO_TELEMETRY_V1 {json}` lines (one per 15-minute slow-loop run) from
CloudWatch Logs and prints two tables:

  1. Per outdoor-temperature band: the slider we set, the effective cap Tado applied,
     Tado's scaling factor, and how often we wrote / you overrode. Use it to see how
     Tado's curve behaves as it gets colder.
  2. Per room per band: valve opening needed to *hold* target, how often the room
     struggles (below target with its valve wide open), and warm-up rate. Rooms that
     need ~85-100% valve just to hold target in cold weather are radiator-upgrade
     candidates -- and they're the ones that set the cold end of the curve.

Tuning: edit `flow_temperature.curve` in /tado/config (no redeploy). Raise a band's
slider_c if several rooms struggle there; lower it if nothing ever needs >~70% valve.
If it's always the same single room, look at its radiator/lockshield instead.

Usage (personal AWS account only; needs logs:FilterLogEvents on the log group):
    pip install boto3
    python scripts/flow_report.py --region eu-west-2 --days 14 [--profile <name>]
"""
import argparse
import json
import math
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone

MARKER = "TADO_TELEMETRY_V1"  # keep in sync with src/telemetry.py

BAND_C = 2                 # outdoor temperature band width
HOLDING_TOL_C = 0.3        # |inside - setpoint| within this = holding target
STRUGGLE_BELOW_C = 0.5     # this far below setpoint ...
STRUGGLE_VALVE_PCT = 90    # ... with the valve at least this open ...
STRUGGLE_MIN_SAMPLES = 4   # ... for this many consecutive samples (~45+ min at 15-min sampling)
MAX_GAP = timedelta(minutes=20)  # samples further apart than this don't count as consecutive
# factor = (effective - INTERCEPT) / (slider - INTERCEPT). Fitted from one outdoor
# temperature (9.8C on 2026-10-19: effective = 0.8 * slider + 3); treat it as indicative.
TADO_INTERCEPT_C = 15
UNDERSIZED_HOLDING_PCT = 85
UNDERSIZED_STRUGGLING_PCT = 20
HEADROOM_HOLDING_PCT = 40


def parse_message(message):
    idx = message.find(MARKER + " ")
    if idx < 0:
        return None
    try:
        return json.loads(message[idx + len(MARKER):])
    except ValueError:
        return None


def fetch(logs, log_group, start):
    paginator = logs.get_paginator("filter_log_events")
    records = []
    for page in paginator.paginate(
        logGroupName=log_group,
        startTime=int(start.timestamp() * 1000),
        filterPattern=f'"{MARKER}"',
    ):
        for event in page.get("events", []):
            rec = parse_message(event["message"])
            if rec is not None:
                records.append(rec)
    return records


def _ts(rec):
    return datetime.fromisoformat(rec["ts"].replace("Z", "+00:00"))


def band(outdoor_c):
    if outdoor_c is None:
        return None
    return int(math.floor(outdoor_c / BAND_C) * BAND_C)


def band_label(lo):
    return f"{lo:+d}..{lo + BAND_C:+d}"


def _median(values, digits=0):
    if not values:
        return None
    m = statistics.median(values)
    return round(m, digits) if digits else int(round(m))


def curve_table(records):
    bands = defaultdict(lambda: {"n": 0, "slider": [], "eff": [], "factor": [], "writes": 0, "would": 0, "overrides": 0})
    for rec in records:
        b = band(rec.get("outdoor_c"))
        if b is None:
            continue
        s = bands[b]
        s["n"] += 1
        slider, eff = rec.get("slider"), rec.get("effective")
        if slider is not None:
            s["slider"].append(slider)
        if eff is not None:
            s["eff"].append(eff)
        if slider is not None and eff is not None and slider != TADO_INTERCEPT_C:
            s["factor"].append((eff - TADO_INTERCEPT_C) / (slider - TADO_INTERCEPT_C))
        flow = rec.get("flow") or {}
        if flow.get("action") == "apply":
            s["would" if flow.get("dry_run") else "writes"] += 1
        if flow.get("reason") == "override-detected":
            s["overrides"] += 1
    return [
        {
            "outdoor": band_label(b), "samples": s["n"],
            "slider": _median(s["slider"]), "effective": _median(s["eff"]),
            "factor": _median(s["factor"], 2), "writes": s["writes"],
            "dry-run": s["would"], "overrides": s["overrides"],
        }
        for b, s in sorted(bands.items())
    ]


def _usable(room):
    """A sample we can judge the radiator on: heating requested, sensor trustworthy, no manual/window/away noise."""
    if room.get("sp") is None or room.get("t") is None or room.get("pw") is None:
        return False
    return not any(room.get(k) for k in ("win", "man", "boost", "away", "dis"))


def room_table(records):
    names, valve_only, disconnected = {}, set(), set()
    per_room = defaultdict(list)
    for rec in sorted(records, key=_ts):
        b = band(rec.get("outdoor_c"))
        for room in rec.get("rooms") or []:
            rid = room.get("id")
            names[rid] = room.get("n")
            if room.get("dev") is not None and "SU04" not in room["dev"]:
                valve_only.add(rid)
            if room.get("dis"):
                disconnected.add(rid)
            per_room[rid].append((_ts(rec), b, room))

    stats = defaultdict(lambda: {"usable": 0, "holding": [], "struggling": 0, "rates": []})
    for rid, samples in per_room.items():
        prev, run = None, 0
        for ts, b, room in samples:
            if b is None or not _usable(room):
                prev, run = None, 0
                continue
            s = stats[(rid, b)]
            s["usable"] += 1
            t, sp, pw = room["t"], room["sp"], room["pw"]
            consecutive = prev is not None and ts - prev[0] <= MAX_GAP and prev[1]["sp"] == sp

            if abs(t - sp) <= HOLDING_TOL_C:
                s["holding"].append(pw)

            starving = t < sp - STRUGGLE_BELOW_C and pw >= STRUGGLE_VALVE_PCT
            run = (run + 1 if consecutive else 1) if starving else 0
            if run >= STRUGGLE_MIN_SAMPLES:
                s["struggling"] += 1

            # Warm-up rate: valve wide open across both samples while still below target.
            if consecutive and pw >= STRUGGLE_VALVE_PCT and prev[1]["pw"] >= STRUGGLE_VALVE_PCT and prev[1]["t"] < sp:
                hours = (ts - prev[0]).total_seconds() / 3600
                s["rates"].append((t - prev[1]["t"]) / hours)
            prev = (ts, room)

    rows = []
    for (rid, b), s in stats.items():
        holding = _median(s["holding"])
        struggling = round(100 * s["struggling"] / s["usable"]) if s["usable"] else None
        flags = ",".join(f for f, on in (("valve-only", rid in valve_only), ("disconnected", rid in disconnected)) if on)
        rows.append({
            "room": names.get(rid) or str(rid), "outdoor": band_label(b), "samples": s["usable"],
            "holding valve%": holding, "struggling%": struggling,
            "warm-up C/h": _median(s["rates"], 1), "flags": flags,
            "verdict": verdict(holding, struggling), "_band": b,
        })
    rows.sort(key=lambda r: (r["_band"], -(r["struggling%"] or 0), -(r["holding valve%"] or 0)))
    for r in rows:
        del r["_band"]
    return rows


def verdict(holding_pct, struggling_pct):
    """Heuristic; only meaningful in the colder bands, where radiators are actually stretched."""
    if (struggling_pct or 0) >= UNDERSIZED_STRUGGLING_PCT or (holding_pct or 0) >= UNDERSIZED_HOLDING_PCT:
        return "undersized?"
    if holding_pct is not None and holding_pct <= HEADROOM_HOLDING_PCT and not struggling_pct:
        return "headroom"
    return "ok"


def print_table(title, rows):
    print(f"\n{title}")
    if not rows:
        print("  (no data)")
        return
    cols = list(rows[0])
    cell = lambda v: "-" if v is None or v == "" else str(v)
    widths = {c: max(len(c), *(len(cell(r[c])) for r in rows)) for c in cols}
    print("  " + "  ".join(c.ljust(widths[c]) for c in cols))
    for r in rows:
        print("  " + "  ".join(cell(r[c]).ljust(widths[c]) for c in cols))


def main():
    import boto3

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--region", default=None)
    parser.add_argument("--profile", default=None)
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--log-group", default="/aws/lambda/tado-dhw-scheduler")
    args = parser.parse_args()

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    start = datetime.now(timezone.utc) - timedelta(days=args.days)
    records = fetch(session.client("logs"), args.log_group, start)
    print(f"{len(records)} telemetry records over the last {args.days} days.")

    print_table("Flow ceiling by outdoor temperature", curve_table(records))
    print_table("Rooms by outdoor temperature (coldest first)", room_table(records))
    print(
        "\nholding valve% = median valve opening while within "
        f"{HOLDING_TOL_C}C of target; struggling% = share of samples more than {STRUGGLE_BELOW_C}C "
        f"below target with valve >= {STRUGGLE_VALVE_PCT}% for {STRUGGLE_MIN_SAMPLES}+ samples in a row.\n"
        "valve-only rooms measure next to the radiator, so they look better than they are. "
        "A stuck lockshield looks the same as an undersized radiator."
    )


if __name__ == "__main__":
    main()
