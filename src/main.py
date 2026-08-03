import time
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from tado_auth import TadoAuthenticator
from tado_client import TadoClient
from config_manager import ConfigManager

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Reused across warm Lambda invocations to avoid re-discovering the home id
# and re-creating boto3 clients on every 5-minute tick.
_client = None
_config_mgr = None


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
    """Lazily build (and cache across warm invocations) the client and config manager."""
    global _client, _config_mgr
    if _client is None:
        auth = TadoAuthenticator()
        client = TadoClient(auth)
        client.discover_context()
        _client = client
    if _config_mgr is None:
        _config_mgr = ConfigManager()
    return _client, _config_mgr


def reconcile(client, config_mgr):
    """One idempotent reconciliation pass.

    Unlike the old 24/7 loop, this keeps NO cross-invocation state. Each run it
    computes the ruling target, reads the current setpoint, and only writes when
    they diverge (>0.5 C). That naturally avoids spamming the Tado API and also
    corrects any manual/external override on the next cycle.
    """
    config_mgr.load_config()

    tz_name = config_mgr.config.get("preferences", {}).get("timezone", "Europe/London")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")
    now = datetime.now(tz)

    target_dt, target_temp = get_ruling_event(config_mgr.schedule_map, now)
    if target_dt is None:
        logger.info("No active schedule event found. Nothing to do.")
        return {"status": "no-op", "reason": "no ruling event"}

    state = client.get_dhw_state()
    current = state.get("setpoint")

    if current is not None and abs(current - target_temp) <= 0.5:
        logger.info("Already at target %s C (current %s C). No change.", target_temp, current)
        return {"status": "no-op", "target": target_temp, "current": current}

    logger.info("Divergence detected: target %s C, current %s C. Applying...", target_temp, current)
    client.set_dhw_temperature(target_temp)

    # Verify the change actually landed.
    time.sleep(2)
    verify = client.get_dhw_state().get("setpoint")
    if verify is None or abs(verify - target_temp) > 0.5:
        raise RuntimeError(f"Verification failed: wanted {target_temp}, got {verify}")

    logger.info("Applied %s C successfully.", target_temp)
    return {"status": "applied", "target": target_temp, "previous": current}


def handler(event, context):
    """Lambda entry point. Invoked on a schedule by EventBridge Scheduler."""
    logger.info("Tado DHW reconcile invocation start.")
    client, config_mgr = _bootstrap()
    result = reconcile(client, config_mgr)
    logger.info("Reconcile result: %s", result)
    return result
