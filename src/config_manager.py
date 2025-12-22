import yaml
import logging
from pathlib import Path
from datetime import datetime
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

logger = logging.getLogger(__name__)

class ConfigManager(FileSystemEventHandler):
    def __init__(self, config_path, on_reload_callback):
        self.config_path = Path(config_path)
        self.callback = on_reload_callback
        self.config = {}
        self.schedule_map = {}
        
        self.load_config()
        
        self.observer = Observer()
        self.observer.schedule(self, path=str(self.config_path.parent), recursive=False)
        self.observer.start()

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