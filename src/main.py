import time
import logging
import sys
import pytz
from datetime import datetime, timedelta
from pathlib import Path

from tado_auth import TadoAuthenticator
from tado_client import TadoClient
from config_manager import ConfigManager

# --- LOGGING ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(module)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# CONFIGURATION
CONFIG_PATH = Path("/app/config/config.yaml")

# STATE
# Stores the datetime of the specific schedule event we last successfully applied
last_successful_event_dt = None

def get_ruling_event(schedule_map, now):
    """
    Determines which schedule event should be active right now.
    Returns: (event_datetime, temperature) or (None, None)
    """
    # 1. Check Today's Schedule
    day_str = now.strftime("%a").upper() # "MON"
    today_events = schedule_map.get(day_str, [])
    
    # Filter for events that have already passed today
    past_events = [e for e in today_events if e['time'] <= now.time()]
    
    if past_events:
        # The latest passed event is the ruling one
        target = past_events[-1]
        # Combine with today's date to make a full datetime
        target_dt = datetime.combine(now.date(), target['time']).replace(tzinfo=now.tzinfo)
        return target_dt, target['temp']
    
    # 2. If no events yet today (e.g. 02:00 AM), check Yesterday's last event
    yesterday = now - timedelta(days=1)
    day_str_prev = yesterday.strftime("%a").upper()
    yesterday_events = schedule_map.get(day_str_prev, [])
    
    if yesterday_events:
        target = yesterday_events[-1]
        target_dt = datetime.combine(yesterday.date(), target['time']).replace(tzinfo=now.tzinfo)
        return target_dt, target['temp']

    return None, None

def main_loop_step(client, config_mgr):
    global last_successful_event_dt
    
    # Get configuration and timezone
    tz_name = config_mgr.config.get('preferences', {}).get('timezone', 'Europe/London')
    try:
        tz = pytz.timezone(tz_name)
    except:
        tz = pytz.UTC
        
    now = datetime.now(tz)
    
    # Determine what SHOULD be happening
    target_dt, target_temp = get_ruling_event(config_mgr.schedule_map, now)
    
    if not target_dt:
        logger.debug("No active schedule found (Sparse schedule?).")
        return

    # LOGIC: If the target event is newer than our last success, EXECUTE.
    if last_successful_event_dt is None or target_dt > last_successful_event_dt:
        logger.info(f"👉 Target Change Detected: {target_temp}°C (Event from {target_dt.strftime('%H:%M')})")
        
        try:
            # Attempt to set
            client.set_dhw_temperature(target_temp)
            
            # Verify
            time.sleep(2)
            state = client.get_dhw_state()
            current = state.get('setpoint')
            
            if current is None or abs(current - target_temp) > 0.5:
                 raise Exception(f"Mismatch! Wanted {target_temp}, got {current}")
            
            logger.info(f"✅ SUCCESS: Applied {target_temp}°C")
            
            # Update State: We successfully applied this specific event instance
            last_successful_event_dt = target_dt
            
        except Exception as e:
            logger.error(f"❌ Failed to apply target ({e}). Will retry next loop.")
            # We do NOT update last_successful_event_dt, so the next loop will retry.

if __name__ == "__main__":
    logger.info("🚀 Tado DHW Scheduler (State Reconciliation Mode) Starting...")
    
    # --- UPDATED AUTH FLOW ---
    # Initialize using the strict directory paths expected by the persistent volume
    auth = TadoAuthenticator(token_dir="/app/storage", token_file="token_store.json")
    
    try:
        # This one call replaces load_tokens() and interactive_login().
        # It handles missing tokens, expired tokens, and the OAuth printout automatically.
        auth.get_authenticated_session()
    except Exception as e:
        logger.critical(f"Login failed: {e}")
        sys.exit(1)
    # -------------------------

    # Client
    client = TadoClient(auth)
    try:
        client.discover_context()
    except Exception as e:
        logger.critical(f"💀 Fatal startup error: {e}")
        sys.exit(1)

    # Config
    def on_reload():
        logger.info("Config reloaded.")
        
    config_mgr = ConfigManager(CONFIG_PATH, on_reload)

    logger.info("⏳ Loop Started. Checking state every 60 seconds...")
    
    while True:
        try:
            main_loop_step(client, config_mgr)
        except Exception as e:
            logger.error(f"💥 Unexpected error in main loop: {e}")
            
        # Wait 60 seconds before next check
        time.sleep(60)