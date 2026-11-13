#!/usr/bin/env python3
"""
One-time Google OAuth helper. Run locally on your Mac, never in GitHub Actions.
Prints a refresh token. Paste it into GitHub Secrets as GOOGLE_REFRESH_TOKEN.
Never commit the output, never paste it into chat.

Prereqs (see PLAN.md §3):
  1. GCP project created, Google Health API enabled.
  2. OAuth consent screen: External, app name + support email, test user = you.
  3. OAuth client: Desktop app. Copy its Client ID + Secret.
  4. Scopes requested below are read-only health metrics + sleep + activity.

Usage:
  cp .env.example .env   # fill GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET
  python3 auth.py
  # Open the printed URL, sign in as kishandanny@gmail.com, click
  # Advanced > Go (unverified app warning is expected for personal use),
  # Allow, paste the code back. The script prints GOOGLE_REFRESH_TOKEN.
  # Then flip the app to Production in GCP > Audience, and run auth.py
  # once more to mint a durable token. The old Testing token stays 7-day.
"""
import os
import urllib.parse

import requests

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"

SCOPES = [
    "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly",
    "https://www.googleapis.com/auth/googlehealth.sleep.readonly",
    "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly",
]

def load_env():
    # Minimal .env loader, no dependency. .env is gitignored.
    if os.path.exists(".env"):
        for line in open(".env"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

def main():
    load_env()
    cid = os.environ.get("GOOGLE_CLIENT_ID")
    csec = os.environ.get("GOOGLE_CLIENT_SECRET")
    if not cid or not csec or cid.startswith("PASTE"):
        print("Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env first. See PLAN.md §3.")
        raise SystemExit(1)
    params = {
        "client_id": cid,
        "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
    }
    url = AUTH_URL + "?" + urllib.parse.urlencode(params)
    print("1. Open this URL in your browser:\n")
    print(url)
    print("\n2. Sign in, click Advanced > Go, Allow, copy the code.")
    code = input("\n3. Paste the code here: ").strip()
    r = requests.post(TOKEN_URL, data={
        "code": code,
        "client_id": cid,
        "client_secret": csec,
        "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
        "grant_type": "authorization_code",
    }, timeout=30)
    r.raise_for_status()
    data = r.json()
    print("\n--- SAVE THIS IN GITHUB SECRETS, DO NOT COMMIT OR SHARE ---")
    print(f"GOOGLE_REFRESH_TOKEN={data['refresh_token']}")
    print("---")
    print("Access token (1h, for testing only):", data.get("access_token", "")[:20] + "...")

if __name__ == "__main__":
    main()
