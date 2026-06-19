import os
import json
import time
import requests

class TadoAuth:
    def __init__(self, token_dir="/app/storage", token_file="tado_tokens.json"):
        # The public Tado client ID used by their web/mobile apps
        self.CLIENT_ID = "1bb50063-6b0c-4d11-bd99-387f4a91cc46"
        self.TOKEN_PATH = os.path.join(token_dir, token_file)
        self.AUTH_SERVER = "https://login.tado.com"
        
        # Ensure persistent storage directory exists
        os.makedirs(token_dir, exist_ok=True)

    def load_tokens(self):
        """Loads cached tokens from the persistent storage volume."""
        if os.path.exists(self.TOKEN_PATH):
            try:
                with open(self.TOKEN_PATH, "r") as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                print("[WARNING] Token file corrupted or unreadable. Forcing re-authentication.", flush=True)
        return None

    def save_tokens(self, tokens):
        """Saves active tokens to the persistent storage volume."""
        try:
            with open(self.TOKEN_PATH, "w") as f:
                json.dump(tokens, f, indent=4)
            # Set absolute timestamp for token expiration tracking
            tokens["expires_at"] = time.time() + tokens.get("expires_in", 3600)
            with open(self.TOKEN_PATH, "w") as f:
                json.dump(tokens, f, indent=4)
            print("[INFO] Tokens successfully cached to storage.", flush=True)
        except IOError as e:
            print(f"[ERROR] Failed to save tokens to disk: {e}", flush=True)

    def refresh_access_token(self, refresh_token):
        """Uses a refresh token to obtain a new access token without re-authenticating."""
        payload = {
            "client_id": self.CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token
        }
        try:
            resp = requests.post(f"{self.AUTH_SERVER}/oauth2/token", data=payload, timeout=10)
            if resp.status_code == 200:
                tokens = resp.json()
                self.save_tokens(tokens)
                return tokens
            else:
                print(f"[ERROR] Token refresh failed: {resp.status_code} - {resp.text}", flush=True)
                return None
        except requests.RequestException as e:
            print(f"[ERROR] Network error during token refresh: {e}", flush=True)
            return None

    def get_authenticated_session(self):
        """
        Main entry point for handling auth state. Returns an authenticated HTTP session 
        or executes the interactive OAuth Device Flow if no valid tokens exist.
        """
        tokens = self.load_tokens()

        if tokens:
            # Check if the current token is still valid or needs a refresh
            expires_at = tokens.get("expires_at", 0)
            if time.time() < (expires_at - 60):  # 1-minute buffer
                return self._create_session(tokens["access_token"])
            
            print("[INFO] Access token expired. Attempting refresh...", flush=True)
            if "refresh_token" in tokens:
                new_tokens = self.refresh_access_token(tokens["refresh_token"])
                if new_tokens:
                    return self._create_session(new_tokens["access_token"])

        # Fall back to interactive Device Flow login if loading and refreshing fail
        print("[WARNING] No active authorization available. Launching Device Flow...", flush=True)
        tokens = self._run_device_flow()
        if tokens:
            return self._create_session(tokens["access_token"])
        
        raise RuntimeError("Authentication pipeline failed completely.")

    def _create_session(self, access_token):
        """Helper to build a pre-authorized requests session."""
        session = requests.Session()
        session.headers.update({
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        })
        return session

    def _run_device_flow(self):
        """Handles the multi-step interactive OAuth device authentication process."""
        payload = {
            "client_id": self.CLIENT_ID,
            "scope": "openid email profile"
        }
        
        try:
            resp = requests.post(f"{self.AUTH_SERVER}/oauth2/device_authorize", data=payload, timeout=10)
            data = resp.json()
        except requests.RequestException as e:
            print(f"[CRITICAL] Failed to connect to Tado Auth backend: {e}", flush=True)
            time.sleep(60)
            return None

        # --- 1. CATCH TADO API ERRORS & RATE LIMITS ---
        if 'error' in data:
            print(f"\n🚨 TADO API ERROR: {data.get('error')} - {data.get('error_description', 'No details available')}", flush=True)
            print("⏳ Cool-down active. Script will sleep for 5 minutes to prevent spamming...", flush=True)
            time.sleep(300)
            return None

        # --- 2. EXTRACT DEVICE CODES AND LINKS SAFELY ---
        device_code = data.get('device_code')
        user_code = data.get('user_code')
        interval = data.get('interval', 5)
        auth_url = data.get('verification_uri_complete', '')

        # --- 3. FIX MISSING CLIENT_ID DESYNC ---
        if auth_url:
            if "client_id=" not in auth_url:
                auth_url += f"&client_id={self.CLIENT_ID}"

            print("\n============================================================", flush=True)
            print("🔐 TADO AUTHENTICATION REQUIRED", flush=True)
            print("============================================================", flush=True)
            print(f"👉 STEP 1: Visit this authorization URL in your browser:\n", flush=True)
            print(f"   {auth_url}\n", flush=True)
            print(f"👉 STEP 2: Verify the displayed code matches: {user_code}\n", flush=True)
            print("============================================================\n", flush=True)
        else:
            print(f"[CRITICAL] Tado response format unexpected. Raw payload: {data}", flush=True)
            return None

        # --- 4. POLL FOR USER APPROVAL ---
        poll_payload = {
            "client_id": self.CLIENT_ID,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": device_code
        }

        print("[INFO] Waiting for authorization...", flush=True)
        while True:
            time.sleep(interval)
            try:
                poll_resp = requests.post(f"{self.AUTH_SERVER}/oauth2/token", data=poll_payload, timeout=10)
                poll_data = poll_resp.json()
                
                if poll_resp.status_code == 200:
                    print("\n🎉 Access granted successfully!", flush=True)
                    self.save_tokens(poll_data)
                    return poll_data
                
                error = poll_data.get("error", "")
                if error == "authorization_pending":
                    continue  # User hasn't finished logging in yet, keep waiting
                elif error == "slow_down":
                    interval += 5  # Server requested a slower polling cadence
                elif error in ["expired_token", "access_denied"]:
                    print(f"\n[ERROR] Authorization session closed: {poll_data.get('error_description')}", flush=True)
                    return None
                else:
                    print(f"\n[ERROR] Unexpected polling response: {poll_data}", flush=True)
                    return None
                    
            except requests.RequestException as e:
                print(f"\n[WARNING] Network hiccup while polling auth status: {e}", flush=True)
                continue