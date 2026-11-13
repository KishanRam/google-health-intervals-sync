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

Mapping (verified against live Google Health API v4, discovery
revision 20261113, and Pulse's live-tested notes, Nov 13 2026):
  daily-resting-heart-rate      -> restingHR (bpm)
  daily-heart-rate-variability  -> hrv (RMSSD, ms) + avgSleepingHR if present
  sleep                         -> sleepSecs, dated to wake-up date
  daily-oxygen-saturation       -> spO2 (%)
  daily-respiratory-rate        -> respiration (breaths/min)
  daily-vo2-max                 -> vo2max (ml/kg/min)
  steps (list, summed)          -> steps
  weight (Sample, weightGrams)  -> weight (kg, grams/1000, last sample of day)

Read method, corrected Nov 13 2026 after a 404 on every type:
  dailyRollUp is NOT a GET endpoint. It is POST
  /users/me/dataTypes/{type}/dataPoints:dailyRollUp, and daily
  types (daily-resting-heart-rate, daily-heart-rate-variability,
  daily-oxygen-saturation, daily-respiratory-rate, daily-vo2-max)
  do NOT support dailyRollUp at all. They support only list and
  reconcile. So daily types are read with GET
  /users/me/dataTypes/{type}/dataPoints with a date filter:
    {snake_type}.date >= "YYYY-MM-DD" AND {snake_type}.date < "YYYY-MM-DD"
  Sleep (Session) uses:
    sleep.interval.civil_end_time >= "YYYY-MM-DD" AND sleep.interval.civil_end_time < "YYYY-MM-DD"
  Steps (Interval) uses list with a physical-time filter and the
  minute buckets are summed in code. dailyRollUp is not used.
  Response values live in dataPoints[].{camelType}, e.g.
  dailyRestingHeartRate.beatsPerMinute (a string), not in a
  generic "value" array. Numeric-looking fields are often strings.

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
  Local real-data test needs env vars exported first, sync.py does
  not read .env on its own:
    set -a; source .env; set +a
    python3 sync.py --dry-run --date 2026-11-12
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

# Daily type -> (camelCase key in DataPoint, field holding the value)
DAILY_SPECS = {
    "daily-resting-heart-rate": ("dailyRestingHeartRate", "beatsPerMinute"),
    "daily-heart-rate-variability": ("dailyHeartRateVariability", "averageHeartRateVariabilityMilliseconds"),
    "daily-oxygen-saturation": ("dailyOxygenSaturation", "averagePercentage"),
    "daily-respiratory-rate": ("dailyRespiratoryRate", "breathsPerMinute"),
    "daily-vo2-max": ("dailyVo2Max", "vo2Max"),
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


def _num(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def get_daily_point(session, data_type, day):
    """GET list for one daily type on one civil date. Returns the nested
    typed object (e.g. dailyRestingHeartRate dict) or None."""
    camel, _field = DAILY_SPECS[data_type]
    snake = data_type.replace("-", "_")
    nxt = day + timedelta(days=1)
    filt = f'{snake}.date >= "{day.isoformat()}" AND {snake}.date < "{nxt.isoformat()}"'
    try:
        data = google_get(session, f"/users/me/dataTypes/{data_type}/dataPoints",
                          params={"filter": filt, "pageSize": 10})
    except requests.HTTPError as e:
        log(f"list {data_type} {day} failed: {e}")
        return None
    points = data.get("dataPoints") or []
    if not points:
        return None
    # Daily types should have one point per day; take the last.
    return points[-1].get(camel) or {}


def get_daily_value(session, data_type, day):
    """Daily list value for one calendar day (America/Vancouver date)."""
    obj = get_daily_point(session, data_type, day)
    if not obj:
        return None
    _camel, field = DAILY_SPECS[data_type]
    return _num(obj.get(field))


def get_sleep_secs(session, wake_day):
    """Sum sleep seconds for sessions ending on wake_day. Date to wake date."""
    nxt = wake_day + timedelta(days=1)
    filt = (f'sleep.interval.civil_end_time >= "{wake_day.isoformat()}" '
            f'AND sleep.interval.civil_end_time < "{nxt.isoformat()}"')
    try:
        data = google_get(session, "/users/me/dataTypes/sleep/dataPoints",
                          params={"filter": filt, "pageSize": 25})
    except requests.HTTPError as e:
        log(f"sleep {wake_day} failed: {e}")
        return None
    points = data.get("dataPoints") or []
    total = 0.0
    for p in points:
        interval = (p.get("sleep") or {}).get("interval") or {}
        s = interval.get("startTime")
        e = interval.get("endTime")
        if not s or not e:
            continue
        try:
            sd = datetime.fromisoformat(s.replace("Z", "+00:00"))
            ed = datetime.fromisoformat(e.replace("Z", "+00:00"))
            total += max(0.0, (ed - sd).total_seconds())
        except ValueError:
            continue
    return int(total) if total > 0 else None


def get_steps(session, day):
    """Sum minute-bucket step counts for one Vancouver day via list.
    Vancouver is UTC-7, so the day is 07:00Z to next day 07:00Z."""
    start = f"{day.isoformat()}T07:00:00Z"
    end = f"{(day + timedelta(days=1)).isoformat()}T07:00:00Z"
    filt = (f'steps.interval.start_time >= "{start}" '
            f'AND steps.interval.start_time < "{end}"')
    total = 0
    page_token = None
    try:
        while True:
            params = {"filter": filt, "pageSize": 2000}
            if page_token:
                params["pageToken"] = page_token
            data = google_get(session, "/users/me/dataTypes/steps/dataPoints",
                              params=params)
            for p in data.get("dataPoints") or []:
                v = _num((p.get("steps") or {}).get("count"))
                if v is not None:
                    total += int(v)
            page_token = data.get("nextPageToken")
            if not page_token:
                break
    except requests.HTTPError as e:
        log(f"steps {day} failed: {e}")
        return None
    return total if total > 0 else None


def get_weight_kg(session, day):
    """Last weight sample for one Vancouver day, in kg.
    Weight is a Sample type: filter on weight.sample_time.physical_time,
    Vancouver day = 07:00Z to next day 07:00Z. Take the last point."""
    start = f"{day.isoformat()}T07:00:00Z"
    end = f"{(day + timedelta(days=1)).isoformat()}T07:00:00Z"
    filt = (f'weight.sample_time.physical_time >= "{start}" '
            f'AND weight.sample_time.physical_time < "{end}"')
    try:
        data = google_get(session, "/users/me/dataTypes/weight/dataPoints",
                          params={"filter": filt, "pageSize": 10})
    except requests.HTTPError as e:
        log(f"weight {day} failed: {e}")
        return None
    points = data.get("dataPoints") or []
    if not points:
        return None
    grams = _num((points[-1].get("weight") or {}).get("weightGrams"))
    if grams is None:
        return None
    return round(grams / 1000.0, 3)


def build_payload_for_day(session, day, mock=False):
    if mock:
        return {
            "restingHR": 52, "hrv": 68, "avgSleepingHR": 48,
            "sleepSecs": 25200, "spO2": 97, "respiration": 14.5,
            "vo2max": 48, "steps": 8200, "weight": 73.6,
        }
    payload = {}
    rhr = get_daily_value(session, "daily-resting-heart-rate", day)
    if rhr is not None:
        payload["restingHR"] = int(round(rhr))
    hrv_obj = get_daily_point(session, "daily-heart-rate-variability", day)
    if hrv_obj:
        hrv = _num(hrv_obj.get("averageHeartRateVariabilityMilliseconds"))
        if hrv is not None:
            payload["hrv"] = float(hrv)  # RMSSD ms. Do NOT mix with Apple SDNN.
        # Second value in the same point, if Google returns it.
        non_rem = _num(hrv_obj.get("nonRemHeartRateBeatsPerMinute"))
        if non_rem is not None:
            payload["avgSleepingHR"] = int(round(non_rem))
    spo2 = get_daily_value(session, "daily-oxygen-saturation", day)
    if spo2 is not None:
        payload["spO2"] = float(spo2)
    resp = get_daily_value(session, "daily-respiratory-rate", day)
    if resp is not None:
        payload["respiration"] = float(resp)
    vo2 = get_daily_value(session, "daily-vo2-max", day)
    if vo2 is not None:
        payload["vo2max"] = float(vo2)
    sleep_secs = get_sleep_secs(session, day)
    if sleep_secs is not None:
        payload["sleepSecs"] = sleep_secs
    steps = get_steps(session, day)
    if steps is not None:
        payload["steps"] = steps
    weight = get_weight_kg(session, day)
    if weight is not None:
        payload["weight"] = weight
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
