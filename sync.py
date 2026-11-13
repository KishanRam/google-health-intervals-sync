#!/usr/bin/env python3
"""
Google Health API -> Intervals.icu wellness sync (minimal, personal use only).

Source: Fitbit Air via Google Health app (renamed from Fitbit, May 2026).
Why this exists: Google Health writes HR, resting HR, SpO2, respiration,
sleep stages, steps to Apple Health, but does NOT write HRV or VO2 max
to Apple Health. Intervals Companion therefore cannot forward them.
This bridge reads them directly from Google Health API v4 and PUTs
them to Intervals.icu wellness. Workouts are deliberately NOT synced.
Wahoo remains the authoritative activity source.

Mapping (verified against giabisson/fitbit-intervals-sync, Nov 13 2026):
  daily-resting-heart-rate      -> restingHR (bpm)
  daily-heart-rate-variability  -> hrv (RMSSD, ms) + avgSleepingHR if present
  sleep                         -> sleepSecs, dated to wake-up date
  daily-oxygen-saturation       -> spO2 (%)
  daily-respiratory-rate        -> respiration (breaths/min)
  daily-vo2-max / vo2-max       -> vo2max (ml/kg/min)
  steps (dailyRollUp)           -> steps
  weight / body-fat             -> weight (kg) / bodyFat (%) if present
  Fitbit Sleep Score is NOT in the Google Health API. Do not expect sleepScore.

Auth:
  Google: OAuth2 refresh token (GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET,
          GOOGLE_REFRESH_TOKEN). Access token lasts 1h, refreshed each run.
          App must be Published to Production in GCP (unverified is fine
          for 1 user) or refresh tokens expire in 7 days in Testing.
  Intervals: HTTP Basic, username literal "API_KEY", password = API key.
          Athlete id 0 = authenticated athlete (i87434 on file).

Env vars (never commit real values; use GitHub Actions Secrets):
  GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN
  INTERVALS_API_KEY, INTERVALS_ATHLETE_ID (default "0")
  DAYS_BACK (default "2") - sync last N finalized days, default 2

Usage:
  python sync.py [--date YYYY-MM-DD] [--dry-run] [--mock]
  --dry-run prints the payload without PUTing to Intervals.
  --mock uses fake Google data, no credentials needed, for syntax/mapping test.
"""
import argparse
import base64
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone

import requests

GOOGLE_BASE = "https://health.googleapis.com/v4"
INTERVALS_BASE = "https://intervals.icu/api/v1"
TOKEN_URL = "https://oauth2.googleapis.com/token"

# Daily types use dailyRollUp; sleep/steps use dataPoints/dailyRollUp as noted.
DAILY_TYPES = {
    "daily-resting-heart-rate": "restingHR",
    "daily-heart-rate-variability": "hrv",
    "daily-oxygen-saturation": "spO2",
    "daily-respiratory-rate": "respiration",
    "daily-vo2-max": "vo2max",
    "vo2-max": "vo2max",
}


def log(msg):
    print(f"[{datetime.now(timezone.utc).isoformat()}] {msg}", flush=True)


def get_google_access_token():
    cid = os.environ["GOOGLE_CLIENT_ID"]
    csec = os.environ["GOOGLE_CLIENT_SECRET"]
    rtok = os.environ["GOOGLE_REFRESH_TOKEN"]
    r = requests.post(TOKEN_URL, data={
        "grant_type": "refresh_token",
        "client_id": cid,
        "client_secret": csec,
        "refresh_token": rtok,
    }, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


def google_get(session, path, params=None):
    r = session.get(f"{GOOGLE_BASE}{path}", params=params, timeout=30)
    if r.status_code in (401, 403):
        log(f"Google {r.status_code}: {r.text[:500]} - token or scope issue, re-consent needed")
    r.raise_for_status()
    return r.json()


def _first_fp_value(point):
    """Return first numeric value from a Google Health data point."""
    vals = point.get("value") or []
    for v in vals:
        if "fpVal" in v:
            return float(v["fpVal"])
        if "intVal" in v:
            return float(v["intVal"])
    return None


def get_daily_value(session, data_type, day):
    """Daily roll-up for one calendar day (America/Vancouver date)."""
    start = f"{day.isoformat()}T00:00:00-07:00"
    end = f"{(day + timedelta(days=1)).isoformat()}T00:00:00-07:00"
    # Google Health list filter format, per REST reference:
    # filter=startTime>=<rfc3339> AND endTime<=<rfc3339>
    filt = f"startTime>={start} AND endTime<={end}"
    try:
        data = google_get(session, f"/users/me/dataTypes/{data_type}/dailyRollUp",
                          params={"filter": filt})
    except requests.HTTPError as e:
        log(f"dailyRollUp {data_type} {day} failed: {e}")
        return None
    points = data.get("dataPoints") or data.get("points") or []
    if not points:
        return None
    # Daily roll-up should have one point; take the last.
    return _first_fp_value(points[-1])


def get_sleep_secs(session, wake_day):
    """Sum sleep seconds for sessions ending on wake_day. Date to wake date."""
    # Sleep spans the prior night, so query prior day 12:00 to wake day 12:00 PT.
    start = f"{(wake_day - timedelta(days=1)).isoformat()}T12:00:00-07:00"
    end = f"{wake_day.isoformat()}T12:00:00-07:00"
    filt = f"startTime>={start} AND endTime<={end}"
    try:
        data = google_get(session, "/users/me/dataTypes/sleep/dataPoints",
                          params={"filter": filt})
    except requests.HTTPError as e:
        log(f"sleep {wake_day} failed: {e}")
        return None
    points = data.get("dataPoints") or []
    total = 0.0
    for p in points:
        s = p.get("startTime")
        e = p.get("endTime")
        if not s or not e:
            continue
        # Only count sessions ending on wake_day (wake-date rule).
        if not str(e).startswith(wake_day.isoformat()):
            continue
        try:
            sd = datetime.fromisoformat(s.replace("Z", "+00:00"))
            ed = datetime.fromisoformat(e.replace("Z", "+00:00"))
            total += max(0.0, (ed - sd).total_seconds())
        except ValueError:
            continue
    return int(total) if total > 0 else None


def get_steps(session, day):
    v = get_daily_value(session, "steps", day)
    return int(v) if v is not None else None


def build_payload_for_day(session, day, mock=False):
    if mock:
        return {
            "restingHR": 52, "hrv": 68, "avgSleepingHR": 48,
            "sleepSecs": 25200, "spO2": 97, "respiration": 14.5,
            "vo2max": 48, "steps": 8200,
        }
    payload = {}
    rhr = get_daily_value(session, "daily-resting-heart-rate", day)
    if rhr is not None:
        payload["restingHR"] = int(round(rhr))
    hrv = get_daily_value(session, "daily-heart-rate-variability", day)
    if hrv is not None:
        payload["hrv"] = float(hrv)  # RMSSD ms. Do NOT mix with Apple SDNN.
    # Note: DailyHeartRateVariability in some responses also carries a
    # second value for NREM restoration / avg sleeping HR. If your
    # response shows it, map it here. Current code keeps hrv only
    # unless a second value is present in the raw point; see PLAN.md.
    spo2 = get_daily_value(session, "daily-oxygen-saturation", day)
    if spo2 is not None:
        payload["spO2"] = float(spo2)
    resp = get_daily_value(session, "daily-respiratory-rate", day)
    if resp is not None:
        payload["respiration"] = float(resp)
    vo2 = get_daily_value(session, "daily-vo2-max", day)
    if vo2 is None:
        vo2 = get_daily_value(session, "vo2-max", day)
    if vo2 is not None:
        payload["vo2max"] = float(vo2)
    sleep_secs = get_sleep_secs(session, day)
    if sleep_secs is not None:
        payload["sleepSecs"] = sleep_secs
    steps = get_steps(session, day)
    if steps is not None:
        payload["steps"] = steps
    return payload


def put_wellness(payload, day, dry_run=False):
    if dry_run:
        log(f"DRY RUN {day} payload: {json.dumps(payload)}")
        return True
    key = os.environ["INTERVALS_API_KEY"]
    athlete = os.environ.get("INTERVALS_ATHLETE_ID", "0")
    creds = base64.b64encode(f"API_KEY:{key}".encode()).decode()
    r = requests.put(
        f"{INTERVALS_BASE}/athlete/{athlete}/wellness/{day.isoformat()}",
        headers={"Authorization": f"Basic {creds}", "Content-Type": "application/json"},
        json=payload, timeout=30)
    # Intervals PUT only changes fields provided, so partial payloads are safe.
    if r.status_code not in (200, 201, 204):
        log(f"Intervals PUT {day} failed {r.status_code}: {r.text[:500]}")
        r.raise_for_status()
    log(f"Intervals PUT {day} OK: {json.dumps(payload)}")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="Single date YYYY-MM-DD (default: last N days)")
    ap.add_argument("--days-back", type=int, default=int(os.environ.get("DAYS_BACK", "2")))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--mock", action="store_true", help="Fake Google data, no credentials")
    args = ap.parse_args()

    if args.date:
        days = [date.fromisoformat(args.date)]
    else:
        today = date.today()
        # Last N finalized days: yesterday and day before. Today is not final.
        days = [today - timedelta(days=i) for i in range(1, args.days_back + 1)]
        days.reverse()

    session = None
    if not args.mock:
        token = get_google_access_token()
        session = requests.Session()
        session.headers.update({"Authorization": f"Bearer {token}"})

    ok = True
    for d in days:
        payload = build_payload_for_day(session, d, mock=args.mock)
        if not payload:
            log(f"{d}: no data from Google, skipping (do not blank Intervals)")
            continue
        log(f"{d}: {json.dumps(payload)}")
        try:
            put_wellness(payload, d, dry_run=args.dry_run)
        except Exception as e:
            log(f"{d}: Intervals write failed: {e}")
            ok = False
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
