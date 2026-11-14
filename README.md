# Google Health to Intervals.icu sync

Personal bridge for Kishan Ramachandran. Fitbit Air -> Google Health API -> Intervals.icu wellness.

Read `PLAN.md` first. It is the full handoff plan. This README is the short operator version.

## What it syncs (wellness only, no workouts)

| Google Health | Intervals |
|---|---|
| daily-resting-heart-rate | restingHR |
| daily-heart-rate-variability | hrv (RMSSD ms) |
| sleep | sleepSecs, dated to wake-up date |
| daily-oxygen-saturation | spO2 |
| daily-respiratory-rate | respiration |
| daily-vo2-max | vo2max |
| steps | steps |
| weight | weight (kg, last sample of the day) |

Wahoo stays the authoritative activity source. Intervals Companion is no longer needed for wellness, the bridge now covers weight too. This bridge only writes the fields above. Intervals PUT only changes fields sent, so it will not blank other wellness fields.

## Quick start

1. Follow PLAN.md §3 (GCP project, enable Google Health API, Desktop OAuth client, add yourself as test user, publish to Production, run `auth.py` once more after publishing).
2. Intervals.icu > Settings > Developer > create API key.
3. Create a private GitHub repo, push this folder, add Secrets: `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN`, `INTERVALS_API_KEY`.
4. Run the Action manually once with dry-run, check logs, then run for real for yesterday. Verify in Intervals.icu wellness for that date.
5. Leave the schedule on. It runs 10:00 and 19:00 Vancouver time, syncing today plus the last 2 days.

## Local test (no keys)

```bash
pip install -r requirements.txt
python sync.py --mock --dry-run
python sync.py --mock --dry-run --date 2026-11-12
```

## Costs

Free. Intervals core $0, Google Health API $0 at this volume, GitHub Actions free tier: private repos 2,000 min/month, this uses ~60 min/month. No CASA, no verification needed for 1 user. Stay unverified, click Advanced > Go on consent.
