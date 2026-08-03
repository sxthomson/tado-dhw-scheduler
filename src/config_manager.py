import os
import logging
from datetime import datetime

import yaml
import boto3

logger = logging.getLogger(__name__)

DAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]


class ConfigManager:
    """Loads the DHW schedule from an SSM String parameter on every call.

    An operator edits the parameter in the AWS console (Systems Manager >
    Parameter Store) and the next Lambda invocation picks up the change --
    no redeploy, no PR, no CI run.
    """

    def __init__(self, config_param_name=None, ssm_client=None):
        self.config_param_name = config_param_name or os.environ.get("CONFIG_PARAM_NAME", "/tado/config")
        self.ssm = ssm_client or boto3.client("ssm")
        self.config = {}
        self.schedule_map = {}

    def load_config(self):
        resp = self.ssm.get_parameter(Name=self.config_param_name)
        self.config = yaml.safe_load(resp["Parameter"]["Value"]) or {}

        # Flatten the schedule list into a per-day map, sorted by time.
        # Result: {'MON': [{'time': time(6,30), 'temp': 55}, ...], ...}
        new_map = {day: [] for day in DAYS}
        for block in self.config.get("schedule", []):
            t_obj = datetime.strptime(block["time"], "%H:%M").time()
            temp = block["temperature"]
            for day in block["days"]:
                key = day.upper()[:3]
                if key in new_map:
                    new_map[key].append({"time": t_obj, "temp": temp})

        for day in new_map:
            new_map[day].sort(key=lambda x: x["time"])  # ordering is load-bearing for get_ruling_event

        self.schedule_map = new_map
        logger.info("Configuration loaded from %s and indexed.", self.config_param_name)
        return self.config
