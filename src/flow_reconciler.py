import math
import time
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import NamedTuple, Optional

logger = logging.getLogger(__name__)

# Tado's weather updates roughly hourly; anything older than this means the feed has
# stalled and we shouldn't steer the boiler off it.
MAX_WEATHER_AGE_MINUTES = 180

# Seconds to wait before reading the slider back after a write (mirrors DHW apply()).
VERIFY_DELAY_S = 2


@dataclass(frozen=True)
class FlowConfig:
    """Parsed `flow_temperature:` block from /tado/config.

    `curve` maps outdoor temperature to the app's Maximum Flow Temperature *slider*
    value -- not the flow temperature the boiler ends up at. Tado scales the slider
    down by its own weather curve (probed 2026-10-19 at 9.8C outdoor: effective cap =
    0.8 * slider + 3), so these numbers are deliberately higher than the target flow.
    """
    dry_run: bool
    curve: tuple  # ((outdoor_c, slider_c), ...) sorted by outdoor_c ascending
    min_change_c: float = 2
    override_grace_minutes: float = 720


class Decision(NamedTuple):
    action: str               # "apply" or "noop"
    reason: str
    new_state: Optional[dict]  # what to persist to the tracker (after the PATCH, for "apply"); None = leave it


def parse_flow_config(raw):
    """Return a FlowConfig, or None when the block is absent or `enabled` is not true.

    Raises ValueError on a malformed curve so a bad edit fails loudly (in the flow
    reconciler only -- DHW is isolated from this in main.handler).
    """
    if not raw or not raw.get("enabled", False):
        return None

    curve = []
    for point in raw.get("curve") or []:
        try:
            curve.append((float(point["outdoor_c"]), float(point["slider_c"])))
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"Invalid flow_temperature.curve point: {point!r}")
    if not curve:
        raise ValueError("flow_temperature.curve needs at least one {outdoor_c, slider_c} point.")
    curve.sort()
    outdoor_temps = [c[0] for c in curve]
    if len(set(outdoor_temps)) != len(outdoor_temps):
        raise ValueError(f"flow_temperature.curve has duplicate outdoor_c values: {outdoor_temps}")

    return FlowConfig(
        dry_run=bool(raw.get("dry_run", False)),
        curve=tuple(curve),
        min_change_c=float(raw.get("min_change_c", 2)),
        override_grace_minutes=float(raw.get("override_grace_minutes", 720)),
    )


def interpolate(curve, outdoor_c):
    """Slider value for `outdoor_c`: linear between points, flat beyond either end, rounded half-up."""
    if outdoor_c <= curve[0][0]:
        value = curve[0][1]
    elif outdoor_c >= curve[-1][0]:
        value = curve[-1][1]
    else:
        for (x0, y0), (x1, y1) in zip(curve, curve[1:]):
            if x0 <= outdoor_c <= x1:
                value = y0 + (outdoor_c - x0) * (y1 - y0) / (x1 - x0)
                break
    # Not round(): Python rounds halves to even, so 66.5 would become 66.
    return int(math.floor(value + 0.5))


def parse_ts(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def outdoor_from_weather(weather, now):
    """Outdoor temperature from Tado's weather payload, or None if missing or stale."""
    outside = (weather or {}).get("outsideTemperature") or {}
    celsius = outside.get("celsius")
    timestamp = outside.get("timestamp")
    if celsius is None or timestamp is None:
        return None
    age_min = (now - parse_ts(timestamp)).total_seconds() / 60
    if age_min > MAX_WEATHER_AGE_MINUTES:
        return None
    return float(celsius)


def decide(current, target, state, now, cfg):
    """Pure decision step, mirroring the DHW override logic in main.reconcile().

    Slider values are whole degrees, so comparisons are exact. Order matters: rule 1
    runs first so that "the live value is already close enough" always wins, which also
    makes a crash between our PATCH and the tracker save harmless (we just adopt it).
    """
    last_applied = state.get("last_applied_temp")
    override_since = state.get("override_since")
    override_temp = state.get("override_temp")
    applied = {"last_applied_temp": target, "last_applied_at": now.isoformat()}

    # 1. Within the deadband of the curve -- treat the live value as ours, no write.
    if abs(current - target) < cfg.min_change_c:
        if last_applied == current and override_since is None:
            return Decision("noop", "within-deadband", None)
        return Decision("noop", "adopted", {"last_applied_temp": current, "last_applied_at": now.isoformat()})

    # 2. No memory of a previous write (first run, or tracker reset) -- take control.
    if last_applied is None:
        return Decision("apply", "bootstrap", applied)

    # 3. Still what we last wrote -- the weather moved, follow the curve.
    if current == last_applied:
        return Decision("apply", "curve", applied)

    # 4. Someone else moved the slider (the Tado app). Respect it for a grace window,
    #    restarting the clock if they move it again.
    if override_since is None or override_temp != current:
        return Decision("noop", "override-detected", {
            "last_applied_temp": last_applied,
            "last_applied_at": state.get("last_applied_at"),
            "override_since": now.isoformat(),
            "override_temp": current,
        })

    elapsed_min = (now - parse_ts(override_since)).total_seconds() / 60
    if elapsed_min < cfg.override_grace_minutes:
        return Decision("noop", "override-in-grace", None)

    return Decision("apply", "override-expired", applied)


def reconcile_flow(client, tracker, cfg, flow, weather, now):
    """One idempotent flow-ceiling pass, using already-fetched `flow` and `weather` payloads.

    Returns a decision dict (embedded in the telemetry record). Raises on anything that
    should show up as a Lambda error: unknown constraints, a write that didn't land, or
    auto-adaptation switching off under us.
    """
    auto = flow.get("autoAdaptation") or {}
    current = flow.get("maxFlowTemperature")
    result = {"dry_run": cfg.dry_run, "current": current}

    # With auto-adaptation off, the slider IS the boiler cap and the curve's values would
    # run far too hot in mild weather. Never re-enable it ourselves -- that's the user's call.
    if not auto.get("enabled"):
        logger.warning("Flow: Tado auto-adaptation is OFF; leaving the max flow slider alone.")
        return {**result, "action": "skip", "reason": "auto-adaptation-off"}
    if current is None:
        raise RuntimeError("Tado did not report a current maxFlowTemperature.")

    outdoor_c = outdoor_from_weather(weather, now)
    if outdoor_c is None:
        logger.warning("Flow: outdoor temperature missing or older than %s min; skipping.", MAX_WEATHER_AGE_MINUTES)
        return {**result, "action": "skip", "reason": "weather-stale"}

    if client.flow_min is None or client.flow_max is None:
        raise RuntimeError("Flow temperature constraints unknown; refusing to compute a target.")
    target = max(client.flow_min, min(interpolate(cfg.curve, outdoor_c), client.flow_max))

    state = tracker.load()
    decision = decide(int(current), target, state, now, cfg)
    result.update(outdoor_c=outdoor_c, target=target, action=decision.action, reason=decision.reason)
    if decision.reason.startswith("override"):
        result["override_since"] = (decision.new_state or state).get("override_since")

    if cfg.dry_run:
        logger.info("Flow [dry run]: outdoor %.1fC -> slider target %s (live %s): would %s (%s).",
                    outdoor_c, target, current, decision.action, decision.reason)
        return result

    if decision.action == "noop":
        if decision.new_state is not None:
            tracker.save(decision.new_state)
        logger.info("Flow: outdoor %.1fC -> slider target %s (live %s). No change (%s).",
                    outdoor_c, target, current, decision.reason)
        return result

    logger.info("Flow: outdoor %.1fC -> setting slider %s -> %s (%s).", outdoor_c, current, target, decision.reason)
    client.set_max_flow_temperature(target)
    # Record the write before verifying, so a timeout below can't make our own write
    # look like a manual override next run.
    tracker.save(decision.new_state)

    time.sleep(VERIFY_DELAY_S)
    after = client.get_flow_temperature_optimization()
    verify = after.get("maxFlowTemperature")
    if verify != target:
        tracker.save(state)  # the write didn't take; don't claim it as ours
        raise RuntimeError(f"Flow verification failed: wanted slider {target}, got {verify}")
    if not (after.get("autoAdaptation") or {}).get("enabled"):
        raise RuntimeError("Tado auto-adaptation is OFF after our max flow write.")

    logger.info("Flow: slider set to %s successfully.", target)
    return result
