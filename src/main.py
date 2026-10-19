import os
import time
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import telemetry
from tado_auth import TadoAuthenticator
from tado_client import TadoClient
from config_manager import ConfigManager
from override_tracker import OverrideTracker
from flow_reconciler import parse_flow_config, reconcile_flow

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Tolerance for "is the setpoint at X" comparisons, in degrees C.
TOLERANCE_C = 0.5

# Flow temperature + room telemetry (the "slow loop") run once per SLOW_LOOP_MINUTES
# slot, on whichever 5-minute tick lands first in it. Outdoor temperature only
# updates ~hourly, so there's no point spending API calls every tick.
SLOW_LOOP_MINUTES = 15
TICK_SECONDS = 300  # must match the template's rate(5 minutes) ScheduleExpression
# Skip the slow loop rather than risk the Lambda being killed mid-write.
MIN_REMAINING_MS_FOR_SLOW_LOOP = 30_000

# Reused across warm Lambda invocations to avoid re-discovering the home id
# and re-creating boto3 clients on every 5-minute tick.
_client = None
_config_mgr = None
_tracker = None
_flow_tracker = None


def get_ruling_event(schedule_map, now):
    """Return (event_datetime, temperature) that should be active at `now`.

    Returns (None, None) if the schedule is empty. Looks back to yesterday's
    last event to cover early-morning hours before any of today's events.
    """
    day_str = now.strftime("%a").upper()  # "MON"
    today_events = schedule_map.get(day_str, [])
    past_events = [e for e in today_events if e["time"] <= now.time()]
    if past_events:
        target = past_events[-1]  # latest event that has already passed today
        target_dt = datetime.combine(now.date(), target["time"]).replace(tzinfo=now.tzinfo)
        return target_dt, target["temp"]

    # Before today's first event -> yesterday's last event still rules.
    yesterday = now - timedelta(days=1)
    prev_events = schedule_map.get(yesterday.strftime("%a").upper(), [])
    if prev_events:
        target = prev_events[-1]
        target_dt = datetime.combine(yesterday.date(), target["time"]).replace(tzinfo=now.tzinfo)
        return target_dt, target["temp"]

    return None, None


def _bootstrap():
    """Lazily build (and cache across warm invocations) the client, config manager and trackers."""
    global _client, _config_mgr, _tracker, _flow_tracker
    if _client is None:
        auth = TadoAuthenticator()
        client = TadoClient(auth)
        client.discover_context()
        _client = client
    if _config_mgr is None:
        _config_mgr = ConfigManager()
    if _tracker is None:
        _tracker = OverrideTracker()
    if _flow_tracker is None:
        # Always pass an explicit name: OverrideTracker(None) falls back to the DHW
        # parameter, and the two reconcilers would overwrite each other's state.
        _flow_tracker = OverrideTracker(
            param_name=os.environ.get("FLOW_LAST_APPLIED_PARAM_NAME", "/tado/flow_last_applied")
        )
    return _client, _config_mgr, _tracker, _flow_tracker


def reconcile(client, config_mgr, tracker):
    """One idempotent reconciliation pass.

    Like the old design, this keeps NO in-memory state across invocations --
    but it does keep one small durable fact in SSM via `tracker`: the setpoint
    WE last wrote. That's what lets this tell apart two reasons the live
    setpoint might not match today's schedule target:

      1. It still holds whatever WE set it to last time, and the schedule has
         simply moved on to a new block -- business as usual, apply the new
         target.
      2. It holds something else entirely -- a manual boost via the Tado app
         (Tado's API gives no "who/why" signal for this; see main.py in git
         history / CLAUDE.md for the investigation). Respect it for up to
         `override_grace_minutes` (default 60) before reasserting control, so
         a boost from the app just works without a code change or a second
         "boost" UI. Tune the grace window via the `override_grace_minutes`
         key in the schedule's `preferences:` block (same SSM param as the
         schedule -- no redeploy needed).

    Expects `config_mgr.load_config()` to have been called already (the handler loads
    the config once per invocation for both reconcilers).
    """
    prefs = config_mgr.config.get("preferences", {})

    tz_name = prefs.get("timezone", "Europe/London")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")
    now = datetime.now(tz)
    grace_minutes = prefs.get("override_grace_minutes", 60)

    target_dt, target_temp = get_ruling_event(config_mgr.schedule_map, now)
    if target_dt is None:
        logger.info("No active schedule event found. Nothing to do.")
        return {"status": "no-op", "reason": "no ruling event"}

    state = client.get_dhw_state()
    current = state.get("setpoint")
    if current is None:
        raise RuntimeError("Tado did not report a current DHW setpoint (get_dhw_state returned no 'setpoint').")

    tracked = tracker.load()
    last_applied_temp = tracked.get("last_applied_temp")
    override_since = tracked.get("override_since")
    override_temp = tracked.get("override_temp")

    def apply(temp, reason):
        logger.info("Applying %s C (%s). Previous: %s C.", temp, reason, current)
        client.set_dhw_temperature(temp)

        # Verify the change actually landed.
        time.sleep(2)
        verify = client.get_dhw_state().get("setpoint")
        if verify is None or abs(verify - temp) > TOLERANCE_C:
            raise RuntimeError(f"Verification failed: wanted {temp}, got {verify}")

        # A fresh apply always starts a clean slate -- any override being
        # tracked is, by definition, over now that we've taken control back.
        tracker.save({"last_applied_temp": temp, "last_applied_at": now.isoformat()})
        logger.info("Applied %s C successfully.", temp)
        return {"status": "applied", "target": temp, "previous": current, "reason": reason}

    # 1. Already at the scheduled target -- in sync, nothing to do.
    if abs(current - target_temp) <= TOLERANCE_C:
        if override_since is not None or last_applied_temp is None or abs(last_applied_temp - current) > TOLERANCE_C:
            tracker.save({"last_applied_temp": current, "last_applied_at": now.isoformat()})
        logger.info("Already at target %s C (current %s C). No change.", target_temp, current)
        return {"status": "no-op", "target": target_temp, "current": current}

    # 2. First run ever (or tracker param wiped) -- no memory of what we last
    #    set, so we can't tell a boost from a stale setpoint. Bootstrap by
    #    taking control, same as the original always-enforce behaviour.
    if last_applied_temp is None:
        return apply(target_temp, "bootstrap")

    # 3. Current still matches what WE last wrote -- nothing external has
    #    touched it, the schedule has simply moved on to a new block.
    if abs(current - last_applied_temp) <= TOLERANCE_C:
        return apply(target_temp, "schedule")

    # 4. Current differs from BOTH the target and our last write -- something
    #    else (a manual boost) changed it. Respect it for a grace window.
    is_continuing_override = (
        override_since is not None
        and override_temp is not None
        and abs(current - override_temp) <= TOLERANCE_C
    )

    if not is_continuing_override:
        logger.info(
            "Manual override detected: %s C (target is %s C). Respecting it for up to %s min.",
            current, target_temp, grace_minutes,
        )
        tracker.save({
            "last_applied_temp": last_applied_temp,
            "last_applied_at": tracked.get("last_applied_at"),
            "override_since": now.isoformat(),
            "override_temp": current,
        })
        return {"status": "no-op", "reason": "override-detected", "current": current, "target": target_temp}

    elapsed_min = (now - datetime.fromisoformat(override_since)).total_seconds() / 60
    if elapsed_min < grace_minutes:
        logger.info(
            "Override still within grace window (%.0f/%s min). Leaving %s C alone.",
            elapsed_min, grace_minutes, current,
        )
        return {
            "status": "no-op", "reason": "override-in-grace",
            "current": current, "target": target_temp, "elapsed_min": round(elapsed_min, 1),
        }

    logger.info("Override grace window elapsed (%.0f min). Resuming schedule control.", elapsed_min)
    return apply(target_temp, "override-expired")


def slow_tick_time(event):
    """The tick's scheduled time (exact, from EventBridge Scheduler's Input), else the wall clock.

    Gating on the scheduled time rather than the wall clock means start-up jitter can't
    make two ticks (or none) land in the same slow-loop slot.
    """
    scheduled = event.get("scheduled_time") if isinstance(event, dict) else None
    if scheduled:
        try:
            return datetime.fromisoformat(scheduled.replace("Z", "+00:00"))
        except ValueError:
            logger.warning("Unparseable scheduled_time %r; falling back to the wall clock.", scheduled)
    return datetime.now(timezone.utc)


def is_slow_tick(t):
    """True for exactly one 5-minute tick per SLOW_LOOP_MINUTES slot."""
    return (t.minute % SLOW_LOOP_MINUTES) * 60 + t.second < TICK_SECONDS


def run_slow_loop(client, flow_tracker, config, now):
    """Fetch weather/flow (+ rooms) once, reconcile the max flow slider, log one telemetry record.

    Each fetch is isolated so a failing rooms endpoint can't stop flow control (and vice
    versa). Raises only for flow failures -- telemetry gaps are logged, not fatal.
    """
    telemetry_on = bool((config.get("telemetry") or {}).get("enabled", False))
    record = {"v": telemetry.SCHEMA_VERSION, "ts": now.isoformat(), "errors": {}}
    flow_error = None
    try:
        flow_cfg = parse_flow_config(config.get("flow_temperature"))
    except ValueError as e:
        flow_cfg, flow_error = None, e
        record["errors"]["flow_config"] = str(e)

    if not telemetry_on and flow_cfg is None and flow_error is None:
        return {"status": "skipped", "reason": "flow_temperature and telemetry disabled"}

    def fetch(name, fn):
        try:
            return fn()
        except Exception as e:
            logger.error("Slow loop: fetching %s failed: %s", name, e)
            record["errors"][name] = f"{type(e).__name__}: {e}"
            return None

    try:
        weather = fetch("weather", client.get_weather)
        flow = fetch("flow", client.get_flow_temperature_optimization)
        if telemetry_on:
            rooms = fetch("rooms", client.get_rooms)
            rooms_and_devices = fetch("rooms_and_devices", client.get_rooms_and_devices)
            if rooms is not None:
                record["rooms"] = telemetry.project_rooms(rooms, rooms_and_devices)
        record.update(telemetry.summarise(weather, flow))

        if flow_cfg is not None:
            if weather is None or flow is None:
                flow_error = RuntimeError("Flow inputs unavailable (weather or flow settings fetch failed).")
                record["flow"] = {"action": "error", "reason": "inputs-unavailable"}
            else:
                try:
                    record["flow"] = reconcile_flow(client, flow_tracker, flow_cfg, flow, weather, now)
                except Exception as e:
                    flow_error = e
                    record["flow"] = {"action": "error", "reason": f"{type(e).__name__}: {e}"}
    finally:
        telemetry.emit(record)

    if flow_error is not None:
        raise flow_error
    return {"status": "ok", "flow": record.get("flow"), "errors": sorted(record["errors"])}


def handler(event, context):
    """Lambda entry point. Invoked on a schedule by EventBridge Scheduler.

    DHW runs every tick; the slow loop (flow ceiling + telemetry) on one tick per
    SLOW_LOOP_MINUTES. They're isolated from each other: either failing still lets the
    other run, and the invocation raises at the end so the failure shows up as a Lambda
    error. (The template sets MaximumRetryAttempts: 0 -- the next tick is the retry.)
    """
    logger.info("Tado reconcile invocation start.")
    client, config_mgr, tracker, flow_tracker = _bootstrap()
    config_mgr.load_config()

    results, failures = {}, []
    try:
        results["dhw"] = reconcile(client, config_mgr, tracker)
    except Exception as e:
        logger.exception("DHW reconcile failed.")
        failures.append(f"dhw: {e}")

    if is_slow_tick(slow_tick_time(event)):
        remaining_ms = context.get_remaining_time_in_millis() if context is not None else None
        if remaining_ms is not None and remaining_ms < MIN_REMAINING_MS_FOR_SLOW_LOOP:
            logger.warning("Only %s ms left; skipping the slow loop this tick.", remaining_ms)
            results["slow"] = {"status": "skipped", "reason": "low remaining time"}
        else:
            try:
                results["slow"] = run_slow_loop(client, flow_tracker, config_mgr.config, datetime.now(timezone.utc))
            except Exception as e:
                logger.exception("Flow temperature reconcile failed.")
                failures.append(f"flow: {e}")

    logger.info("Reconcile result: %s", results)
    if failures:
        raise RuntimeError("Reconcile failed: " + "; ".join(failures))
    return results
