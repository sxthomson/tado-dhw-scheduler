import logging
import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

logger = logging.getLogger(__name__)

class TadoClient:
    API_BASE = "https://hops.tado.com"
    ME_URL = "https://my.tado.com/api/v2/me"

    def __init__(self, authenticator):
        self.auth = authenticator
        self.home_id = None
        self.min_temp = 30
        self.max_temp = 65

    def _get_headers(self):
        return {
            "Authorization": f"Bearer {self.auth.get_valid_token()}",
            "Content-Type": "application/json"
        }

    def _handle_request(self, method, url, **kwargs):
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