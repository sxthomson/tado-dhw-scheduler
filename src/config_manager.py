import yaml
import logging
import shutil
from pathlib import Path
from datetime import datetime
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

logger = logging.getLogger(__name__)

class ConfigManager(FileSystemEventHandler):
    def __init__(self, config_path, on_reload_callback):
        self.config_path = Path(config_path)
        self.default_path = Path("/app/default_config.yaml") # Safe fallback location
        self.callback = on_reload_callback
        self.config = {}
        self.schedule_map = {}
        
        # --- THE FIX: Self-Healing Config ---
        self._ensure_config_exists()
        # ------------------------------------
        
        self.load_config()
        
        self.observer = Observer()
        self.observer.schedule(self, path=str(self.config_path.parent), recursive=False)
        self.observer.start()

    def _ensure_config_exists(self):
        """Restores the default config if the Docker bind mount is empty."""
        if not self.config_path.exists():
            logger.info(f"⚠️ No config found at {self.config_path}. Restoring default...")
            try:
                # Ensure the EC2 host mount directory exists
                self.config_path.parent.mkdir(parents=True, exist_ok=True)
                
                # Drop the default file into the mounted EC2 directory
                if self.default_path.exists():
                    shutil.copy(self.default_path, self.config_path)
                    logger.info("✅ Default config successfully restored to host directory.")
                else:
                    logger.critical(f"💀 Backup config not found at {self.default_path}! Check your Dockerfile.")
            except Exception as e:
                logger.error(f"❌ Failed to write default config: {e}")

    def load_config(self):
        try:
            with open(self.config_path, 'r') as f:
                self.config = yaml.safe_load(f)
            
            # --- PROCESS SCHEDULE INTO MAP ---
            # Result: {'MON': [{'time': time(6,30), 'temp': 55}, ...], ...}
            new_map = {day: [] for day in ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]}
            
            for block in self.config.get('schedule', []):
                time_str = block['time']
                temp = block['temperature']
                
                # Parse "06:30" into a time object
                t_obj = datetime.strptime(time_str, "%H:%M").time()
                
                for day in block['days']:
                    day_key = day.upper()[:3] # Ensure MON, TUE standard
                    if day_key in new_map:
                        new_map[day_key].append({'time': t_obj, 'temp': temp})

            # Sort each day's list by time (Crucial for the logic)
            for day in new_map:
                new_map[day].sort(key=lambda x: x['time'])

            self.schedule_map = new_map
            logger.info("✅ Configuration loaded and indexed.")
            
        except Exception as e:
            logger.error(f"❌ Failed to load config: {e}")

    def on_modified(self, event):
        if event.src_path.endswith(self.config_path.name):
            logger.info("🔄 Config change detected. Reloading...")
            self.load_config()
            if self.callback:
                self.callback()