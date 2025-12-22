import time
import json
import logging
import requests
from pathlib import Path

logger = logging.getLogger(__name__)

class TadoAuthenticator:
    AUTH_URL = "https://login.tado.com/oauth2/device_authorize"
    TOKEN_URL = "https://login.tado.com/oauth2/token"
    CLIENT_ID = "1bb50063-6b0c-4d11-bd99-387f4a91cc46"
    SCOPE = "offline_access"

    def __init__(self, token_path: Path):
        self.token_path = token_path
        self.access_token = None
        self.refresh_token = None

    def load_tokens(self) -> bool:
        if not self.token_path.exists():
            return False
        try:
            with open(self.token_path, 'r') as f:
                data = json.load(f)
                self.refresh_token = data.get('refresh_token')
                return True
        except Exception as e:
            logger.error(f"Failed to load tokens: {e}")
            return False

    def save_tokens(self, data):
        self.access_token = data['access_token']
        self.refresh_token = data['refresh_token']
        with open(self.token_path, 'w') as f:
            json.dump({
                'access_token': self.access_token,
                'refresh_token': self.refresh_token,
                'scope': data.get('scope'),
                'expires_in': data.get('expires_in')
            }, f)
        logger.info("✅ Tokens saved to disk.")

    def get_valid_token(self):
        if not self.refresh_token:
            if not self.load_tokens():
                self.interactive_login()
        self._refresh_access_token()
        return self.access_token

    def _refresh_access_token(self):
        payload = {
            'client_id': self.CLIENT_ID,
            'grant_type': 'refresh_token',
            'refresh_token': self.refresh_token
        }
        try:
            resp = requests.post(self.TOKEN_URL, data=payload)
            resp.raise_for_status()
            self.save_tokens(resp.json())
        except requests.exceptions.HTTPError as e:
            logger.error(f"❌ Token Refresh Failed: {resp.text}")
            raise Exception("Critical: Unable to refresh token. Manual re-auth required.")

    def interactive_login(self):
        print("\n" + "="*60)
        print("🔐 TADO AUTHENTICATION REQUIRED")
        print("="*60)
        
        resp = requests.post(self.AUTH_URL, data={'client_id': self.CLIENT_ID, 'scope': self.SCOPE})
        resp.raise_for_status()
        data = resp.json()
        
        device_code = data['device_code']
        interval = data.get('interval', 5) 
        
        print(f"\n👉 ACTION REQUIRED: Visit this URL to approve access:\n")
        print(f"   {data['verification_uri_complete']}")
        print(f"\n   (Or visit {data['verification_uri']} and enter code: {data['user_code']})")
        print(f"\n⏳ Waiting for approval... (Polling every {interval}s)")
        
        while True:
            time.sleep(interval)
            token_resp = requests.post(self.TOKEN_URL, data={
                'grant_type': 'urn:ietf:params:oauth:grant-type:device_code',
                'client_id': self.CLIENT_ID,
                'device_code': device_code
            })
            
            if token_resp.status_code == 200:
                print("\n✅ Authentication Successful!")
                self.save_tokens(token_resp.json())
                print("="*60 + "\n")
                break
            elif token_resp.json().get('error') == 'authorization_pending':
                continue
            else:
                print(f"❌ Error during polling: {token_resp.text}")
                raise Exception("Authentication failed.")