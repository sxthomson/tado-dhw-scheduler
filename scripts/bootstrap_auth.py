#!/usr/bin/env python3
"""One-time (or re-auth) bootstrap for the Tado DHW Scheduler.

Runs the Tado OAuth 2.0 Device Authorization flow locally, then writes the
resulting tokens into the SSM SecureString parameter the Lambda reads. This is
the only step that needs a human (to approve access in a browser); every
scheduled run afterwards is autonomous via the stored refresh token.

Usage:
    pip install boto3 requests
    python scripts/bootstrap_auth.py [--param /tado/token] [--region eu-west-2]

Requires AWS credentials in your environment (e.g. `aws sso login`, or AWS_*
env vars) with ssm:PutParameter permission on the token parameter.
"""
import argparse
import json
import sys
import time

import boto3
import requests

CLIENT_ID = "1bb50063-6b0c-4d11-bd99-387f4a91cc46"
AUTH_SERVER = "https://login.tado.com"
SCOPE = "openid email profile offline_access"


def run_device_flow():
    resp = requests.post(
        f"{AUTH_SERVER}/oauth2/device_authorize",
        data={"client_id": CLIENT_ID, "scope": SCOPE},
        timeout=10,
    )
    data = resp.json()
    if "error" in data:
        print(f"Tado error: {data.get('error')} - {data.get('error_description', 'no detail')}")
        sys.exit(1)

    device_code = data["device_code"]
    interval = data.get("interval", 5)
    auth_url = data.get("verification_uri_complete", "")
    if auth_url and "client_id=" not in auth_url:
        auth_url += f"&client_id={CLIENT_ID}"

    print("\n" + "=" * 60)
    print("TADO AUTHENTICATION REQUIRED")
    print("=" * 60)
    print(f"\n1. Open this URL in your browser:\n\n   {auth_url}\n")
    print(f"2. Confirm the code shown matches: {data.get('user_code')}\n")
    print("=" * 60 + "\n")
    print("Waiting for approval...")

    poll = {
        "client_id": CLIENT_ID,
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "device_code": device_code,
    }
    while True:
        time.sleep(interval)
        r = requests.post(f"{AUTH_SERVER}/oauth2/token", data=poll, timeout=10)
        body = r.json()
        if r.status_code == 200:
            print("\nAccess granted.")
            body["expires_at"] = time.time() + body.get("expires_in", 3600)
            return body
        err = body.get("error", "")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 5
            continue
        print(f"\nAuthorization failed: {body}")
        sys.exit(1)


def main():
    ap = argparse.ArgumentParser(description="Seed the Tado OAuth token into SSM.")
    ap.add_argument("--param", default="/tado/token", help="SSM parameter name for the token")
    ap.add_argument("--region", default=None, help="AWS region (defaults to your AWS config)")
    args = ap.parse_args()

    tokens = run_device_flow()

    ssm = boto3.client("ssm", region_name=args.region) if args.region else boto3.client("ssm")
    ssm.put_parameter(
        Name=args.param,
        Value=json.dumps(tokens),
        Type="SecureString",
        Overwrite=True,
    )
    print(f"Token written to SSM SecureString parameter: {args.param}")


if __name__ == "__main__":
    main()
