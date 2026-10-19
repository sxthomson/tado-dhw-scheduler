import logging
import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

logger = logging.getLogger(__name__)

# (connect, read) seconds. Without this a hung call could eat the whole Lambda timeout.
DEFAULT_TIMEOUT = (5, 10)

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

    def _get_headers(self):
        return {
            "Authorization": f"Bearer {self.auth.get_valid_token()}",
            "Content-Type": "application/json"
        }

    def _handle_request(self, method, url, **kwargs):
        kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
        try:
            resp = requests.request(method, url, headers=self._get_headers(), **kwargs)
            resp.raise_for_status()
            return resp
        except requests.exceptions.HTTPError as e:
            logger.error(f"⚠️ API ERROR ({resp.status_code}) on {method} {url}")
            try:
                logger.error(f"⚠️ Tado Message: {resp.json()}")
            except:
                logger.error(f"⚠️ Raw Response: {resp.text}")
            raise e
            
    @retry(
        stop=stop_after_attempt(5), 
        wait=wait_exponential(multiplier=1, min=2, max=15),
        retry=retry_if_exception_type((requests.exceptions.ConnectionError, requests.exceptions.Timeout))
    )
    def discover_context(self):
        logger.info("🔍 Discovering Home ID...")
        resp = self._handle_request("GET", self.ME_URL)
        self.home_id = resp.json()['homes'][0]['id']
        logger.info(f"✅ Context Discovered: HomeID={self.home_id}")

    def get_dhw_state(self):
        url = f"{self.API_BASE}/homes/{self.home_id}/programmer/domesticHotWater/state"
        resp = self._handle_request("GET", url)
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
            logger.warning(f"⚠️ Target {target_temp}°C clamped to {safe_temp}°C")
            
        url = f"{self.API_BASE}/homes/{self.home_id}/programmer/domesticHotWater/manualControl?ngsw-bypass=true"
        payload = {"setpoint": safe_temp}
        
        logger.info(f"🔥 Setting DHW to {safe_temp}°C")
        self._handle_request("POST", url, json=payload)
        return safe_temp

    def get_flow_temperature_optimization(self):
        """Flow Temperature Optimization settings for the home (Tado X / hops).

        `maxFlowTemperature` is the app's slider -- the only settable value.
        `autoAdaptation.maxFlowTemperature` is the weather-adjusted cap Tado actually
        applies; it reads null for ~15-30s after the slider changes while Tado recomputes.
        """
        url = f"{self.API_BASE}/homes/{self.home_id}/settings/flowTemperatureOptimization"
        data = self._handle_request("GET", url).json()
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
            logger.warning(f"⚠️ Max flow target {target_temp}°C clamped to {safe_temp}°C")

        url = f"{self.API_BASE}/homes/{self.home_id}/settings/flowTemperatureOptimization"
        logger.info(f"🔥 Setting max flow temperature to {safe_temp}°C")
        self._handle_request("PATCH", url, json={"maxFlowTemperature": safe_temp})
        return safe_temp

    def get_weather(self):
        """Tado's current outdoor weather for the home (my.tado.com; hops has no weather endpoint)."""
        url = f"{self.MY_API}/homes/{self.home_id}/weather"
        return self._handle_request("GET", url).json()

    def get_rooms(self):
        url = f"{self.API_BASE}/homes/{self.home_id}/rooms"
        return self._handle_request("GET", url).json()

    def get_rooms_and_devices(self):
        url = f"{self.API_BASE}/homes/{self.home_id}/roomsAndDevices"
        return self._handle_request("GET", url).json()
