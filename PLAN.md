# E2E Plan: Google Health (Fitbit Air) -> Intervals.icu HRV sync

**Handoff doc. Goal: a second chat / agent can execute this end to end without re-asking Kishan for context.**
**Owner:** Kishan Ramachandran (kishandanny@gmail.com, America/Vancouver, PCT UTC-7 year-round)
**Athlete:** Intervals.icu i87434, API athlete id `0` also works
**Date:** 2026-11-13
**Status:** Code written, not yet deployed. No keys exchanged. No repo created yet in this plan version. See §8 for GitHub push steps.

## 0. TLDR

Build a minimal Python bridge, run it on GitHub Actions twice daily, free. It reads overnight HRV (RMSSD), resting HR, sleep, SpO2, respiration, VO2 max, steps from the Google Health API and PUTs them to Intervals.icu wellness. Publish the Google OAuth app to Production (unverified, 1 user, no CASA) to avoid the 7-day refresh-token expiry. Do not reverse engineer Health Sync. Do not sync workouts. Wahoo stays authoritative.

## 1. Problem, verified

- Path today: Fitbit Air / Google Health app -> Apple Health -> Intervals Companion -> Intervals.icu. Verified working Aug 24, 2026.
- Live Apple Health check Nov 13, 2026: 35,158 daily-metric records, HR / resting HR / SpO2 / respiration / sleep stages / steps present. `heart_rate_variability_ms` observed false, zero rows. `vo2_max` observed false.
- Root cause: Google lists HRV and skin temperature as read-only, not written to Apple Health. HRV does write to Health Connect on Android, not to Apple Health. Companion would forward HRV if it were in HealthKit. It is not there.
- Direct Bluetooth bypass is blocked: Fitbit Air uses GoldenGate / GattLink, DTLS PSK, AES-128-EAX megadump, per-device key never crosses the wire. Do not attempt BLE.
- Google Health API v4 (successor to Fitbit Web API, killed Nov 2, 2026, HTTP 410) has the data: discovery doc revision 20261111 includes `DailyHeartRateVariability`, `HeartRateVariability`, `daily-heart-rate-variability`, `daily-resting-heart-rate`, `daily-oxygen-saturation`, `daily-respiratory-rate`, `daily-vo2-max`, `sleep`, `steps`.
- Intervals API write is proven: `PUT /api/v1/athlete/{id}/wellness/{date}`, only fields provided change. Weight-only PUT verified Jun 3 and Jun 9, 2026 in `~/workspace/icu_runbook.md`.
- Existing proof: `giabisson/fitbit-intervals-sync` (Jul 25, 2026, MIT) implements this exact mapping for a Fitbit Air. Use it as reference, do not copy its Telegram reauth path. This plan builds a cleaner minimal version.

## 2. Non-goals and guardrails

- No workouts, activities, routes, or laps. No duplicate rides. Companion workout sync stays disabled.
- No Apple Health writes. No HealthKit code. No iOS app.
- No reverse engineering Health Sync (appyhapps) or any other app. Their value is a verified OAuth client, not secret code. Stealing their client ID/secret would be revoked and breaks on updates.
- No keys in chat, in files, in git history, or in Muse memory. Muse builds with placeholders only. Kishan pastes keys into GitHub Secrets himself.
- No CASA assessment ($500-$4,500), no brand verification, no public release. Personal use only, 1 user.
- No purchase. Health Sync paid app is the fallback, not the plan.
- Intervals Basic auth (username literal `API_KEY`, password = key) does NOT fit Muse Secure Vault. Vault retest Nov 13, 2026 declined. Do not retry vault storage for this key. GitHub Secrets is the store.
- Keep HRV source single: Fitbit RMSSD only. Apple Health historically stores SDNN. Do not mix baselines. iOS 27 adds a separate RMSSD type, still do not mix.

## 3. Architecture

```
Fitbit Air (overnight)
  -> Google Health app (iPhone)
  -> Google Health API v4 (cloud, OAuth2 refresh token)
  -> sync.py on GitHub Actions (ubuntu-latest, 07:00 + 19:00 PCT)
  -> Intervals.icu API PUT /athlete/0/wellness/{date}
  -> Intervals.icu wellness: hrv, restingHR, sleepSecs, spO2, respiration, vo2max, steps
Wahoo -> Intervals.icu (unchanged, activities only)
Intervals Companion -> Intervals.icu (unchanged, Apple Health wellness it already has)
```

Schedule: cron `0 14,2 * * *` UTC = 14:00 UTC (07:00 PCT) and 02:00 UTC (19:00 PCT prior day). Vancouver is UTC-7 year-round, no fall back Nov 1, 2026.
Sync window: last 2 finalized days (yesterday + day before). Today is not final. A missed run backfills on the next run, up to 14 days if needed by manual dispatch with a date.
Logging: log dates + field names + success/failure only. Never log token values or full health values in Actions logs beyond the payload already shown in dry-run mode. Keep logs minimal in production.

## 4. Field mapping (do not change without checking PLAN §1)

| Google Health dataType | Intervals field | Unit / note |
|---|---|---|
| daily-resting-heart-rate | restingHR | bpm, int |
| daily-heart-rate-variability | hrv | RMSSD ms, float |
| daily-heart-rate-variability (2nd value if present) | avgSleepingHR | NREM restoration HR, only if API returns it. Confirmed Nov 13, 2026: Nov 12 returned 58, mapped in sync.py. |
| sleep (dataPoints) | sleepSecs | seconds, sum sessions ending on wake date. Date the record to wake-up date, not bedtime. |
| daily-oxygen-saturation | spO2 | % |
| daily-respiratory-rate | respiration | breaths/min |
| daily-vo2-max, fallback vo2-max | vo2max | ml/kg/min |
| steps (list, summed in code) | steps | count |
| weight, body-fat | weight, bodyFat | kg, %. Only if present. Do not add unless Kishan asks. |

What is NOT available: Fitbit Sleep Score is not in the Google Health API. Do not expect `sleepScore`. Do not invent it.

## 5. Files in this repo (already written, placeholders only)

```
google-health-intervals-sync/
  PLAN.md                  # this file
  README.md                # operator short version
  sync.py                  # bridge, argparse --date/--dry-run/--mock, DAYS_BACK=2
  auth.py                  # one-time local OAuth helper, prints refresh token
  requirements.txt         # requests only
  .env.example             # placeholder names only
  .gitignore               # ignores .env
  .github/workflows/sync.yml  # schedule + workflow_dispatch, secrets via env
```

`sync.py` behaviour to preserve:
- Refresh Google access token each run from `GOOGLE_REFRESH_TOKEN`.
- Daily types via GET list, NOT dailyRollUp. dailyRollUp is POST only and daily types support only list and reconcile. Filter `{snake_type}.date >= "YYYY-MM-DD" AND {snake_type}.date < "next day"`. Sleep via `sleep.interval.civil_end_time` in the same date range. Steps via `steps.interval.start_time` as RFC3339 UTC (Vancouver day = 07:00Z to next day 07:00Z), summed in code, pageSize 2000 with pageToken loop.
- Sleep via `dataPoints`, sum only sessions ending on wake date.
- If a day has no Google data, skip it. Do not PUT an empty payload and do not blank Intervals.
- Intervals PUT with `Authorization: Basic base64(API_KEY:<key>)`, athlete `0`.
- Exit 1 on Intervals write failure so Actions shows failed, but continue other days.
- `--mock --dry-run` must work with no credentials. Use it for syntax/mapping tests.

## 6. Setup steps, in order (Kishan does §6.1-6.3, agent does the rest)

### 6.1 Google Cloud (Kishan, ~20 min, browser)

1. console.cloud.google.com > create project, name e.g. `kishan-health-sync`. No billing account needed.
2. APIs and Services > Library > search `Google Health API` > Enable. If it is not listed, enable `Health` / `Fitbit` equivalent as shown in console and record the exact API name.
3. OAuth consent screen > User Type External > app name e.g. `Kishan Health Sync`, support email kishandanny@gmail.com, no logo, no homepage needed for personal use.
4. Scopes > Add: `googlehealth.health_metrics_and_measurements.readonly`, `googlehealth.sleep.readonly`, `googlehealth.activity_and_fitness.readonly`. Do not add write scopes.
5. Audience > Test users > Add kishandanny@gmail.com.
6. Credentials > Create > OAuth client ID > Application type Desktop app > Create. Copy Client ID + Client Secret into a local `.env` (from `.env.example`). Never commit `.env`.
7. Run `auth.py` locally once in Testing to prove the flow works: `python3 auth.py`, open URL, sign in, click Advanced > Go, Allow, paste code. Save the refresh token temporarily locally only.
8. **Publish to Production:** GCP > Audience > Publish app > Confirm. This is the fix for 7-day expiry. No verification needed for 1 user. You will still see the unverified warning on consent. That is expected.
9. Run `auth.py` once more AFTER publishing to mint a durable refresh token. The Testing token stays 7-day limited. Save this second token as the one to use.
10. Caveat to test: field reports say Production + unverified + restricted scopes can still expire in some cases. Test past day 8. If it expires, fallback is §9.

### 6.2 Intervals.icu (Kishan, 2 min)

1. intervals.icu > Settings > Developer > create API key. Copy it once.
2. Athlete id is i87434 on file, but code uses `0` (authenticated athlete). No change needed.
3. Do not put the key in chat, email, or any file. It goes straight to GitHub Secrets in §6.3.

### 6.3 GitHub (agent + Kishan)

1. GitHub login on file: `KishanRam` (id 21347815), connected via Muse GitHub connector Nov 13, 2026.
2. Create a **private** repo, name `google-health-intervals-sync` (or `kishan-health-sync` if that name is taken). Private, not public. Health sync code with no keys is still personal.
3. Push the files in §5 to `main`. Verify no `.env` is in the push. Check `git status` before push.
4. Repo > Settings > Secrets and variables > Actions > New repository secret, add all four:
   - `GOOGLE_CLIENT_ID`
   - `GOOGLE_CLIENT_SECRET`
   - `GOOGLE_REFRESH_TOKEN` (the post-Production token from §6.1 step 9)
   - `INTERVALS_API_KEY`
5. Actions > `Google Health to Intervals sync` > Run workflow > leave date blank, dry-run `false` first? No: first run with dry-run `true` if the workflow supports it, or run `sync.py --dry-run` locally with the token to inspect payload. Then run for real for yesterday only.
6. Verify in Intervals.icu: open wellness for yesterday, check `hrv`, `restingHR`, `sleepSecs`, `spO2`, `respiration`, `vo2max`, `steps` match Google Health app for that date within rounding. Check that no workout was created or duplicated. Check that Companion fields were not blanked.
7. Leave the schedule enabled. Monitor the first 3 runs in Actions logs.

## 7. Verification checklist (must all pass before calling it done)

- [ ] `python sync.py --mock --dry-run` exits 0 and prints a payload with hrv, restingHR, sleepSecs.
- [ ] `python -m py_compile sync.py auth.py` passes, workflow YAML parses.
- [ ] GitHub repo is private, no secrets in git history (`git log --all -p | grep -i` for key names returns nothing).
- [ ] Manual Actions run for yesterday writes Intervals wellness, values match Google Health app.
- [ ] No new activity/workout appears in Intervals from this sync.
- [ ] Second scheduled run is a no-op / idempotent overwrite with same values, no duplicates.
- [ ] Day 8 check: after 8 days in Production, run still succeeds with no re-consent. If it fails with 401/invalid_grant, go to §9 fallback.
- [ ] Sleep is dated to wake date, not bedtime. Spot check a night crossing midnight.

## 8. GitHub push instructions for the executing agent

If the repo does not exist yet, create it via the GitHub connector:
- `github status` must be connected, account `KishanRam`.
- Use `create_repository` with name `google-health-intervals-sync`, private true, description `Personal Google Health to Intervals.icu wellness sync for Fitbit Air, HRV only, no workouts`.
- Then `push_files` for each file in §5, branch `main`, commit message `Initial minimal Google Health to Intervals bridge, placeholders only`.
- If `push_files` is not available for this account, fall back to local `git init + remote add + push` using `gh auth` already present on the VM, and ask Kishan to confirm the repo URL. Do not invent a repo URL. Report the exact URL returned.
- After push, read back `README.md` and `sync.yml` from GitHub to confirm the push landed.

## 9. Failure modes and fallbacks

| Failure | Cause | Fix |
|---|---|---|
| Google 401 invalid_grant | Refresh token expired (Testing 7-day, or Production unverified expiry) | Re-run `auth.py`, replace `GOOGLE_REFRESH_TOKEN` secret, test past day 8 again. If it recurs weekly, stay in Testing with a Telegram/email reminder, or buy Health Sync. |
| Google 403 PERMISSION_DENIED | Wrong scope, or token has broad cloud-platform scope | Re-consent with only the 3 readonly scopes in §6.1. Do not use a cloud-platform token. |
| Google 404 on dataType | API version / dataType name drift | Check discovery doc `https://health.googleapis.com/$discovery/rest?version=v4`, update name in `sync.py`, test with `--dry-run`. |
| Intervals 401 | Wrong API key or Basic format | Verify `API_KEY:<key>` base64, regenerate key in Intervals, update secret. |
| Intervals 400 | Bad field value (e.g. hrv as SDNN, sleepSecs as minutes) | Check units in §4, fix mapping, re-run for that date only. |
| No data for a day | Fitbit not worn / not synced to Google Health yet | Skip, do not blank. Next run backfills last 2 days. |
| Actions minutes exceeded | Unlikely at ~60 min/mo | Check Actions usage, reduce to once daily if needed. |

Fallback if custom bridge proves too fragile: Health Sync by appyhapps, iOS, source Google Health, destination Intervals.icu, wellness only, workouts off, paid after 1-week trial. That app is verified and has no weekly reauth. Do not switch without Kishan saying so.

## 10. Costs

- Intervals.icu core $0 forever, Supporter $4/mo optional, not needed.
- Google Health API $0 at personal volume. Quotas: 86.4M req/day/project, 120k/min/project, 300/min/user. This uses ~4 req/day.
- GitHub Actions free tier: private repos 2,000 min/month. This uses ~60 min/month. No card needed for free tier.
- GCP project $0, no billing account needed for this use.
- Total: $0/month.

## 11. Security notes

- Secrets live only in GitHub Actions Secrets (encrypted, never in logs if you do not print them). `sync.py` never prints tokens. It prints payloads only.
- Intervals key is unscoped: it can read/write the whole athlete record. If leaked, revoke it in Intervals Settings immediately and rotate the Google refresh token in GCP.
- Google refresh token gives full health read. If leaked, revoke in Google Account > Third-party access.
- `.env` is gitignored. Do not add a real `.env` to the repo, do not paste keys into this PLAN.md, do not paste keys into chat.
- Repo must stay private.

## 12. Open items for the executing agent (do not block on these)

1. RESOLVED Nov 13, 2026: dailyRollUp is not used. Daily types support only list and reconcile, so sync.py uses GET list, see section 5.
2. RESOLVED Nov 13, 2026: DailyHeartRateVariability does return a second value for avg sleeping HR in Kishan's live data, Nov 12 returned 58, mapped to `avgSleepingHR` in sync.py.
3. RESOLVED Nov 13, 2026 for Nov 12: Google returned no daily-vo2-max for Fitbit Air, so `vo2max` is left absent, not faked. No other day checked yet.
4. If GitHub repo creation needs Kishan to select repositories in the Muse GitHub App, ask him to do that step and then continue. Do not disconnect/reconnect GitHub to fix a missing repo.

---
*Reference: giabisson/fitbit-intervals-sync, Intervals OpenAPI https://intervals.icu/api/v1/docs/, Google Health https://developers.google.com/health/about and /reference/rest, discovery revision 20261111.*
