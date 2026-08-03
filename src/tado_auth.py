import os
import json
import time
import logging

import requests
import boto3

logger = logging.getLogger(__name__)

# The public Tado client ID used by their web/mobile apps.
CLIENT_ID = "1bb50063-6b0c-4d11-bd99-387f4a91cc46"
AUTH_SERVER = "https://login.tado.com"


class TadoAuthenticator:
    """Manages Tado OAuth tokens backed by an SSM SecureString parameter.

    In the serverless runtime this class NEVER runs the interactive device
    flow. If no token exists (or a refresh fails) it raises a clear error and
    you must run scripts/bootstrap_auth.py once to seed the token. The device
    flow itself lives in that bootstrap script, not here.
    """

    def __init__(self, token_param_name=None, ssm_client=None):
        self.token_param_name = token_param_name or os.environ.get("TOKEN_PARAM_NAME", "/tado/token")
        self.ssm = ssm_client or boto3.client("ssm")

    # --- storage backed by SSM SecureString ------------------------------
    def load_tokens(self):
        try:
            resp = self.ssm.get_parameter(Name=self.token_param_name, WithDecryption=True)
            return json.loads(resp["Parameter"]["Value"])
        except self.ssm.exceptions.ParameterNotFound:
            return None
        except (json.JSONDecodeError, KeyError):
            logger.warning("Token parameter %s present but unreadable/corrupt.", self.token_param_name)
            return None

    def save_tokens(self, tokens):
        # Stamp an absolute expiry so we can check validity without another API call.
        if "expires_at" not in tokens:
            tokens["expires_at"] = time.time() + tokens.get("expires_in", 3600)
        self.ssm.put_parameter(
            Name=self.token_param_name,
            Value=json.dumps(tokens),
            Type="SecureString",
            Overwrite=True,
        )
        logger.info("Tokens persisted to SSM parameter %s", self.token_param_name)

    # --- refresh ----------------------------------------------------------
    def refresh_access_token(self, refresh_token):
        payload = {
            "client_id": CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
        resp = requests.post(f"{AUTH_SERVER}/oauth2/token", data=payload, timeout=10)
        if resp.status_code != 200:
            logger.error("Token refresh failed: %s - %s", resp.status_code, resp.text)
            return None

        tokens = resp.json()
        # Preserve the refresh token if Tado doesn't rotate it (prevents token loss).
        if "refresh_token" not in tokens:
            tokens["refresh_token"] = refresh_token
        self.save_tokens(tokens)
        return tokens

    # --- public API used by TadoClient ------------------------------------
    def get_valid_token(self):
        """Return a currently-valid access token, refreshing if needed."""
        tokens = self.load_tokens()
        if not tokens:
            raise RuntimeError(
                f"No Tado token found in SSM ({self.token_param_name}). "
                "Run scripts/bootstrap_auth.py once to seed it."
            )

        # Still valid? (60s safety buffer)
        if time.time() < (tokens.get("expires_at", 0) - 60):
            return tokens["access_token"]

        logger.info("Access token expired; refreshing.")
        refresh_token = tokens.get("refresh_token")
        if not refresh_token:
            raise RuntimeError(
                "Access token expired and no refresh_token stored. "
                "Re-run scripts/bootstrap_auth.py."
            )

        new_tokens = self.refresh_access_token(refresh_token)
        if not new_tokens:
            raise RuntimeError(
                "Token refresh failed (see logs). You may need to re-run scripts/bootstrap_auth.py."
            )
        return new_tokens["access_token"]
