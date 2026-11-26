import os
import json
import time
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import telemetry
from tado_auth import TadoAuthenticator
from tado_client import TadoClient
from config_manager import ConfigManager
from override_tracker import OverrideTracker
from flow_reconciler import can_skip_read, interpolate, outdoor_from_weather, parse_flow_config, reconcile_flow

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

# Lean mode (`lean_mode:` in /tado/config): stay inside Tado's free-tier API quota
# (~100 requests/day) by only calling Tado when a tick actually needs to. DHW first,
# then the flow curve; no room telemetry. Ticks stay every 5 minutes -- Lambda
# invocations are free, Tado calls are what's rationed.
EVENT_RETRY_MINUTES = 15   # keep retrying a schedule event this long if it hasn't landed
USAGE_MARKER = "TADO_API_USAGE"  # one line per invocation that called Tado

# Reused across warm Lambda invocations to avoid re-discovering the home id
# and re-creating boto3 clients on every 5-minute tick.
_client = None
_config_mgr = None
_tracker = None
_flow_tracker = None


@dataclass(frozen=True)
class LeanConfig:
    """Parsed `lean_mode:` block. None (from parse_lean_config) means lean mode is off."""
    dhw_check_minutes: int = 60
    flow_check_minutes: int = 60
    flow_reserve: int = 25


_LEAN_DEFAULTS = LeanConfig()


def _slot_minutes(raw, key):
    default = getattr(_LEAN_DEFAULTS, key)
    value = raw.get(key, default)
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        minutes = None
    if minutes is None or minutes <= 0 or minutes % 5 or 1440 % minutes:
        logger.warning("lean_mode.%s=%r must be a multiple of 5 that divides 1440; using %s.", key, value, default)
        return default
    return minutes


def parse_lean_config(raw):
    """`lean_mode: true` or `{enabled: true, ...}` -> LeanConfig; absent/false -> None.

    Never raises: a bad tuning value falls back to its default with a warning, so a typo
    can't take DHW control down with it.
    """
    if raw is None or raw is False or (isinstance(raw, dict) and raw.get("enabled") is False):
        return None
    if raw is True:
        raw = {"enabled": True}
    if not isinstance(raw, dict) or raw.get("enabled") is not True:
        # Silently staying off would mean ~700 Tado calls/day -- far over the free tier.
        logger.warning("lean_mode=%r is not `true` or `{enabled: true}`; lean mode is OFF.", raw)
        return None
    reserve = raw.get("flow_reserve", _LEAN_DEFAULTS.flow_reserve)
    try:
        reserve = int(reserve)
        if reserve < 0:
            raise ValueError
    except (TypeError, ValueError):
        logger.warning("lean_mode.flow_reserve=%r must be a whole number >= 0; using %s.",
                       raw.get("flow_reserve"), _LEAN_DEFAULTS.flow_reserve)
        reserve = _LEAN_DEFAULTS.flow_reserve
    return LeanConfig(
        dhw_check_minutes=_slot_minutes(raw, "dhw_check_minutes"),
        flow_check_minutes=_slot_minutes(raw, "flow_check_minutes"),
        flow_reserve=reserve,
    )


def schedule_tz(prefs):
    try:
        return ZoneInfo(prefs.get("timezone", "Europe/London"))
    except Exception:
        return ZoneInfo("UTC")


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
        # Home id is resolved lazily (handler -> ensure_home_id) so lean mode can skip
        # the /me lookup when preferences.home_id is set, and so a tick that makes no
        # Tado calls doesn't make one just to start up.
        _client = TadoClient(TadoAuthenticator())
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
    prefs = config_mgr.config.get("preferences") or {}
    now = datetime.now(schedule_tz(prefs))
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


def is_slot_tick(t, slot_minutes):
    """True for exactly one 5-minute tick per `slot_minutes` slot, counted from midnight.

    `slot_minutes` must be a multiple of 5 that divides 1440 (parse_lean_config enforces
    this), so slots line up across midnight whatever the schedule's start offset.
    """
    return ((t.hour * 60 + t.minute) % slot_minutes) * 60 + t.second < TICK_SECONDS


def _parse_dt(value):
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def dhw_due(schedule_map, tick, tracked, grace_minutes, check_minutes):
    """Lean mode: should this tick spend Tado calls on DHW? Pure -> (due, reason).

    `tick` is the tick's scheduled time in the schedule's timezone. `tracked` is the DHW
    OverrideTracker state (SSM, so reading it costs no Tado calls). When due, the normal
    reconcile() runs unchanged; when not, the tick makes no DHW calls at all.
    """
    if tracked.get("last_applied_temp") is None:
        return True, "bootstrap"

    since = _parse_dt(tracked.get("override_since"))
    expiry = since + timedelta(minutes=grace_minutes) if since is not None else None

    event_dt, _ = get_ruling_event(schedule_map, tick)
    if event_dt is not None:
        age_s = (tick - event_dt).total_seconds()
        if 0 <= age_s < TICK_SECONDS:
            return True, "event"
        if 0 <= age_s < EVENT_RETRY_MINUTES * 60:
            # Handled = the tracker was written since the event, or a boost is still in grace
            # (reconcile would leave it alone; grace-expiry picks it up). A no-op that writes
            # nothing, e.g. already at target, costs at most 2 extra reads here.
            last = _parse_dt(tracked.get("last_applied_at"))
            handled = (last is not None and last >= event_dt) or (since is not None and (since >= event_dt or tick < expiry))
            if not handled:
                return True, "event-retry"

    if expiry is not None and 0 <= (tick - expiry).total_seconds() < TICK_SECONDS:
        return True, "grace-expiry"

    if is_slot_tick(tick, check_minutes):
        return True, "check"
    return False, "not-due"


def run_slow_loop(client, flow_tracker, config, now, lean=None):
    """Fetch weather/flow (+ rooms) once, reconcile the max flow slider, log one telemetry record.

    Each fetch is isolated so a failing rooms endpoint can't stop flow control (and vice
    versa). Raises only for flow failures -- telemetry gaps are logged, not fatal.
    With `lean` (a LeanConfig): no rooms, no calls without a usable flow config, and the
    flow settings are read only when the curve needs them (_lean_flow_shortcut).
    """
    telemetry_on = lean is None and bool((config.get("telemetry") or {}).get("enabled", False))
    record = {"v": telemetry.SCHEMA_VERSION, "ts": now.isoformat(), "errors": {}}
    if lean is not None:
        record["lean"] = True
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
        weather = flow = shortcut = None
        if lean is None:
            weather = fetch("weather", client.get_weather)
            flow = fetch("flow", client.get_flow_temperature_optimization)
        elif flow_cfg is not None:  # a bad flow config gets no Tado calls
            shortcut = _lean_budget_skip(client, flow_cfg, lean)
            if shortcut is None:
                weather = fetch("weather", client.get_weather)
                if weather is not None:
                    shortcut = _lean_flow_shortcut(client, flow_tracker, flow_cfg, weather, now, lean)
                    if shortcut is None:
                        flow = fetch("flow", client.get_flow_temperature_optimization)
        if telemetry_on:
            rooms = fetch("rooms", client.get_rooms)
            rooms_and_devices = fetch("rooms_and_devices", client.get_rooms_and_devices)
            if rooms is not None:
                record["rooms"] = telemetry.project_rooms(rooms, rooms_and_devices)
        record.update(telemetry.summarise(weather, flow))

        if shortcut is not None:
            record["flow"] = shortcut
        elif flow_cfg is not None:
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


def _lean_budget_skip(client, flow_cfg, lean):
    """Lean mode: skip flow once Tado reports fewer than `flow_reserve` requests left, keeping them for DHW.

    Only works once Tado's responses carry a remaining count (see TADO_API_USAGE `remaining`).
    """
    remaining = client.rate_limit_remaining
    if remaining is None or remaining >= lean.flow_reserve:
        return None
    logger.warning("Flow [lean]: only %s Tado requests left (reserve %s); leaving them for DHW.",
                   remaining, lean.flow_reserve)
    return {"dry_run": flow_cfg.dry_run, "action": "skip", "reason": "budget-reserve", "remaining": remaining}


def _lean_flow_shortcut(client, flow_tracker, flow_cfg, weather, now, lean):
    """Lean mode: a flow decision that needs no settings read, or None to read and reconcile as usual."""
    skip = _lean_budget_skip(client, flow_cfg, lean)  # the weather response may have just reported it
    if skip is not None:
        return skip
    outdoor_c = outdoor_from_weather(weather, now)
    if outdoor_c is None:
        logger.warning("Flow [lean]: outdoor temperature missing or stale; skipping.")
        return {"dry_run": flow_cfg.dry_run, "action": "skip", "reason": "weather-stale"}
    state = flow_tracker.load()
    if not can_skip_read(flow_cfg, outdoor_c, state):
        return None
    target = interpolate(flow_cfg.curve, outdoor_c)
    logger.info("Flow [lean]: outdoor %.1fC -> slider target %s (last set %s). No change; settings read skipped.",
                outdoor_c, target, state.get("last_applied_temp"))
    return {"dry_run": flow_cfg.dry_run, "current": None, "outdoor_c": outdoor_c, "target": target,
            "last_applied": state.get("last_applied_temp"), "action": "noop", "reason": "lean-unchanged"}


def _slow_loop_calls_tado(config, lean):
    flow_on = bool((config.get("flow_temperature") or {}).get("enabled"))
    rooms_on = lean is None and bool((config.get("telemetry") or {}).get("enabled"))
    return flow_on or rooms_on


def _ensure_home(client, config, lean):
    configured = (config.get("preferences") or {}).get("home_id")
    if lean is not None and configured is None and client.home_id is None:
        logger.warning("Lean mode: set preferences.home_id in /tado/config to skip the /me lookup on cold starts.")
    client.ensure_home_id(configured)


def _log_usage(client, lean):
    summary = client.usage_summary()
    if summary["calls"] or summary["auth_refreshes"]:
        summary["lean"] = lean is not None
        logger.info("%s %s", USAGE_MARKER, json.dumps(summary, separators=(",", ":"), default=str))


def handler(event, context):
    """Lambda entry point. Invoked on a schedule by EventBridge Scheduler.

    DHW runs every tick; the slow loop (flow ceiling + telemetry) on one tick per
    SLOW_LOOP_MINUTES. They're isolated from each other: either failing still lets the
    other run, and the invocation raises at the end so the failure shows up as a Lambda
    error. (The template sets MaximumRetryAttempts: 0 -- the next tick is the retry.)

    In lean mode DHW runs only on ticks dhw_due() picks and the slow loop once per
    `flow_check_minutes`; every other tick makes no Tado calls at all.
    """
    logger.info("Tado reconcile invocation start.")
    client, config_mgr, tracker, flow_tracker = _bootstrap()
    config_mgr.load_config()
    config = config_mgr.config
    lean = parse_lean_config(config.get("lean_mode"))
    tick = slow_tick_time(event)
    client.reset_usage()

    results, failures = {}, []
    try:
        slow = is_slot_tick(tick, lean.flow_check_minutes if lean is not None else SLOW_LOOP_MINUTES)
        due, reason = True, None
        if lean is not None:
            prefs = config.get("preferences") or {}
            try:
                due, reason = dhw_due(config_mgr.schedule_map, tick.astimezone(schedule_tz(prefs)), tracker.load(),
                                      prefs.get("override_grace_minutes", 60), lean.dhw_check_minutes)
            except Exception:
                logger.exception("Lean DHW gate failed; running DHW anyway.")
                due, reason = True, "gate-error"
            if not due:
                results["dhw"] = {"status": "skipped", "reason": "lean-not-due"}

        # Resolve the home id once, and only on ticks that call Tado: a failing /me lookup
        # (with its retries) must not run a second time for the slow loop.
        if due or (slow and _slow_loop_calls_tado(config, lean)):
            try:
                _ensure_home(client, config, lean)
            except Exception as e:
                logger.exception("Tado home id lookup failed; skipping this tick.")
                failures.append(f"home: {e}")
                due = slow = False

        if due:
            try:
                results["dhw"] = reconcile(client, config_mgr, tracker)
                if reason:
                    results["dhw"]["lean_reason"] = reason
            except Exception as e:
                logger.exception("DHW reconcile failed.")
                failures.append(f"dhw: {e}")

        if slow:
            remaining_ms = context.get_remaining_time_in_millis() if context is not None else None
            if remaining_ms is not None and remaining_ms < MIN_REMAINING_MS_FOR_SLOW_LOOP:
                logger.warning("Only %s ms left; skipping the slow loop this tick.", remaining_ms)
                results["slow"] = {"status": "skipped", "reason": "low remaining time"}
            else:
                try:
                    results["slow"] = run_slow_loop(client, flow_tracker, config, datetime.now(timezone.utc), lean=lean)
                except Exception as e:
                    logger.exception("Flow temperature reconcile failed.")
                    failures.append(f"flow: {e}")
    finally:
        _log_usage(client, lean)

    logger.info("Reconcile result: %s", results)
    if failures:
        raise RuntimeError("Reconcile failed: " + "; ".join(failures))
    return results
