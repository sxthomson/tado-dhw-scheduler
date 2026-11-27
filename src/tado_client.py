import re
import logging
from collections import Counter

import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

logger = logging.getLogger(__name__)

# (connect, read) seconds. Without this a hung call could eat the whole Lambda timeout.
DEFAULT_TIMEOUT = (5, 10)

# `RateLimit: "perday";r=19950;t=3600` (IETF draft style). Only `RateLimit-Policy:
# "perday";q=20000` has been seen on this account so far, so parse leniently.
_REMAINING_RE = re.compile(r"(?:^|[;,\s])r=(\d+)")


def parse_rate_limit_remaining(ratelimit=None, legacy_remaining=None):
    """Requests left in Tado's quota window, from the response headers, or None if not reported."""
    if ratelimit:
        m = _REMAINING_RE.search(ratelimit)
        if m:
            return int(m.group(1))
    if legacy_remaining is not None:
        try:
            return int(str(legacy_remaining).strip())
        except ValueError:
            pass
    return None


class TadoClient:
    API_BASE = "https://hops.tado.com"
    MY_API = "https://my.tado.com/api/v2"
    ME_URL = f"{MY_API}/me"

    def __init__(self, authenticator):
        self.auth = authenticator
        self.home_id = None
        self.min_temp = 30
        self.max_temp = 65
        # Learned from maxFlowTemperatureConstraints. Deliberately separate from the DHW
        # limits above, and None until read -- set_max_flow_temperature refuses to guess.
        self.flow_min = None
        self.flow_max = None
        # Per-invocation API accounting (the client is cached across warm invocations,
        # so the handler calls reset_usage() at the start of each one).
        self.usage = Counter()
        self.rate_limit_remaining = None
        self.rate_limit_policy = None

    def reset_usage(self):
        self.usage = Counter()
        self.rate_limit_remaining = None
        self.rate_limit_policy = None
        self.auth.refreshes = 0

    def usage_summary(self):
        """Calls made since reset_usage(), for the TADO_API_USAGE log line."""
        return {
            "calls": sum(self.usage.values()),
            "endpoints": dict(self.usage),
            "auth_refreshes": self.auth.refreshes,
            "remaining": self.rate_limit_remaining,
            "policy": self.rate_limit_policy,
        }

    def ensure_home_id(self, configured=None):
        """Use `preferences.home_id` from the config if set (no API call), else discover once and cache."""
        if configured is not None:
            home_id = str(configured).strip()
            if home_id.isdigit():
                self.home_id = int(home_id)
                return self.home_id
            logger.warning("Config: ignoring preferences.home_id=%r (not a number); discovering it instead.", configured)
        if self.home_id is None:
            self.discover_context()
        return self.home_id

    def _note_rate_limit(self, headers):
        policy = headers.get("RateLimit-Policy")
        if policy:
            self.rate_limit_policy = policy
        remaining = parse_rate_limit_remaining(headers.get("RateLimit"), headers.get("RateLimit-Remaining"))
        if remaining is not None:
            self.rate_limit_remaining = remaining

    def _get_headers(self):
        return {
            "Authorization": f"Bearer {self.auth.get_valid_token()}",
            "Content-Type": "application/json"
        }

    def _handle_request(self, method, url, *, endpoint, **kwargs):
        kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
        headers = self._get_headers()  # may refresh the token (login.tado.com, counted separately)
        self.usage[endpoint] += 1      # count attempts: a request that errors may still have been metered
        try:
            resp = requests.request(method, url, headers=headers, **kwargs)
            self._note_rate_limit(resp.headers)
            resp.raise_for_status()
            return resp
        except requests.exceptions.HTTPError as e:
            try:
                body = resp.json()
            except ValueError:
                body = resp.text
            if resp.status_code == 429:
                logger.error("Tado API: rate limited (429) on %s %s (remaining=%s, policy=%s). Response: %s",
                             method, url, self.rate_limit_remaining, self.rate_limit_policy, body)
            else:
                logger.error("Tado API: HTTP %s on %s %s. Response: %s", resp.status_code, method, url, body)
            raise e

    @retry(
        stop=stop_after_attempt(5), 
        wait=wait_exponential(multiplier=1, min=2, max=15),
        retry=retry_if_exception_type((requests.exceptions.ConnectionError, requests.exceptions.Timeout))
    )
    def discover_context(self):
        logger.info("Tado API: discovering home id.")
        resp = self._handle_request("GET", self.ME_URL, endpoint="me")
        self.home_id = resp.json()['homes'][0]['id']
        logger.info("Tado API: discovered home id %s.", self.home_id)

    def get_dhw_state(self):
        url = f"{self.API_BASE}/homes/{self.home_id}/programmer/domesticHotWater/state"
        resp = self._handle_request("GET", url, endpoint="dhw_state")
        data = resp.json()
        if 'setpointConstraints' in data:
            self.min_temp = data['setpointConstraints'].get('min', 30)
            self.max_temp = data['setpointConstraints'].get('max', 65)
        return data

    @retry(
        stop=stop_after_attempt(3), 
        wait=wait_exponential(multiplier=1, min=4, max=10),
        retry=retry_if_exception_type(requests.exceptions.ConnectionError)
    )
    def set_dhw_temperature(self, target_temp):
        safe_temp = max(self.min_temp, min(target_temp, self.max_temp))
        if safe_temp != target_temp:
            logger.warning("DHW: target %s°C clamped to %s°C (Tado limits %s–%s°C).", target_temp, safe_temp, self.min_temp, self.max_temp)
            
        url = f"{self.API_BASE}/homes/{self.home_id}/programmer/domesticHotWater/manualControl?ngsw-bypass=true"
        payload = {"setpoint": safe_temp}
        
        self._handle_request("POST", url, endpoint="dhw_set", json=payload)
        return safe_temp

    def get_flow_temperature_optimization(self):
        """Flow Temperature Optimization settings for the home (Tado X / hops).

        `maxFlowTemperature` is the app's slider -- the only settable value.
        `autoAdaptation.maxFlowTemperature` is the weather-adjusted cap Tado actually
        applies; it reads null for ~15-30s after the slider changes while Tado recomputes.
        """
        url = f"{self.API_BASE}/homes/{self.home_id}/settings/flowTemperatureOptimization"
        data = self._handle_request("GET", url, endpoint="flow").json()
        constraints = data.get("maxFlowTemperatureConstraints") or {}
        if "min" in constraints and "max" in constraints:
            self.flow_min = constraints["min"]
            self.flow_max = constraints["max"]
        return data

    @retry(
        stop=stop_after_attempt(2),
        wait=wait_exponential(multiplier=1, min=2, max=4),
        retry=retry_if_exception_type(requests.exceptions.ConnectionError)
    )
    def set_max_flow_temperature(self, target_temp):
        if self.flow_min is None or self.flow_max is None:
            raise RuntimeError("Flow temperature constraints unknown; call get_flow_temperature_optimization() first.")
        safe_temp = int(max(self.flow_min, min(target_temp, self.flow_max)))
        if safe_temp != target_temp:
            logger.warning("Flow: slider target %s°C clamped to %s°C (Tado limits %s–%s°C).", target_temp, safe_temp, self.flow_min, self.flow_max)

        url = f"{self.API_BASE}/homes/{self.home_id}/settings/flowTemperatureOptimization"
        self._handle_request("PATCH", url, endpoint="flow_set", json={"maxFlowTemperature": safe_temp})
        return safe_temp

    def get_weather(self):
        """Tado's current outdoor weather for the home (my.tado.com; hops has no weather endpoint)."""
        url = f"{self.MY_API}/homes/{self.home_id}/weather"
        return self._handle_request("GET", url, endpoint="weather").json()

    def get_rooms(self):
        url = f"{self.API_BASE}/homes/{self.home_id}/rooms"
        return self._handle_request("GET", url, endpoint="rooms").json()

    def get_rooms_and_devices(self):
        url = f"{self.API_BASE}/homes/{self.home_id}/roomsAndDevices"
        return self._handle_request("GET", url, endpoint="rooms_and_devices").json()
