#!/usr/bin/env python3
"""
One-time Google OAuth helper. Run locally on your Mac, never in GitHub Actions.
Prints a refresh token. Paste it into GitHub Secrets as GOOGLE_REFRESH_TOKEN.
Never commit the output, never paste it into chat.

Prereqs (see PLAN.md section 3):
  1. GCP project created, Google Health API enabled.
  2. OAuth consent screen: External, app name + support email, test user = you.
     Publish the app to Production first, otherwise the token expires in 7 days.
  3. OAuth client: Desktop app. Copy its Client ID + Secret.
  4. Scopes requested below are read-only health metrics + sleep + activity.

Usage:
  cp .env.example .env   # fill GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET
  /Library/Developer/CommandLineTools/usr/bin/python3 auth.py
  # A browser window opens. Sign in as kishandanny@gmail.com, click
  # Advanced > Go (unverified app warning is expected for personal use),
  # Allow. Google redirects to http://127.0.0.1:8080/ on your own Mac and
  # the script prints GOOGLE_REFRESH_TOKEN. No code to paste by hand.
  # Then paste that token into GitHub Secrets as GOOGLE_REFRESH_TOKEN.

Why loopback, not OOB: Google blocked the out-of-band flow
(redirect_uri=urn:ietf:wg:oauth:2.0:oob, Error 400 invalid_request).
Desktop apps must use a loopback redirect instead. Loopback
redirects to 127.0.0.1 do not need to be pre-registered in GCP;
if you ever see redirect_uri_mismatch, add
http://127.0.0.1:8080/ under Authorized redirect URIs for the client.
"""
import http.server
import os
import urllib.parse
import webbrowser

import requests

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
PORT = 8080

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
        print("Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env first. See PLAN.md section 3.")
        raise SystemExit(1)

    redirect_uri = f"http://127.0.0.1:{PORT}/"
    code_holder = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            query = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(query)
            if "code" in params:
                code_holder["code"] = params["code"][0]
                body = b"<h1>Done. You can close this tab and return to the terminal.</h1>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                error = params.get("error", ["unknown"])[0]
                body = f"<h1>Sign-in failed: {error}. Close this tab and try again.</h1>".encode()
                self.send_response(400)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
    params = {
        "client_id": cid,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
    }
    url = AUTH_URL + "?" + urllib.parse.urlencode(params)
    print("Opening this URL in your browser:\n")
    print(url)
    print("\nSign in, click Advanced > Go, Allow. Then come back here.")
    webbrowser.open(url)
    server.handle_request()

    code = code_holder.get("code")
    if not code:
        print("No code received. Try again, and make sure you Allow the consent screen.")
        raise SystemExit(1)
    server.server_close()

    r = requests.post(TOKEN_URL, data={
        "code": code,
        "client_id": cid,
        "client_secret": csec,
        "redirect_uri": redirect_uri,
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
