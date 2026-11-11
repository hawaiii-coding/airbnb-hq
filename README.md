# airbnb-hq

Automation for Cong's Airbnb HCMC business: calendar feeds, live availability, cleaning sync.
Secrets (iCal URLs, service-account keys) live in Google Secret Manager, never in this repo.

## Live Availability (Phase 1)
Rebuilds the tracker tab **Live Availability** from the 13 Airbnb iCal feeds: per-listing nights for 60 days,
apartment-level nights (1BR+2BR combined) and gaps (orphan nights, short gaps). A failed feed shows UNKNOWN,
never OPEN. Writes only that tab, only on allow-listed spreadsheets, with readback.

Local test: `pip install pytest && python -m pytest -q`
Dry run (no write): `python -m jobs.live_availability --dry-run`

Deploy (gcloud logged in):
1. Feed secrets already exist (ical-url-xxxx, from cleaning-sync); mapping in config/listings.json. Only for a NEW listing: add its secret and a config row.
2. `PROJECT=… REGION=… SA=… SPREADSHEET_ID=<scratch copy> scripts/deploy_live_availability.sh`
3. Run once, check the tab, then redeploy with the production SPREADSHEET_ID.
4. `scripts/schedule_live_availability.sh`, then resume the scheduler.

The worker service account needs Editor access on the target spreadsheet.
PHK listings were dropped (owner, 2026-11-11). 11 feeds remain.

## Auto-deploy (Cloud Build trigger)
A merge to `main` runs tests, builds the image and redeploys the `live-availability` job (cloudbuild.yaml).
Claude works on branches and opens pull requests; merging the PR is the approval to go live.
The spreadsheet the job writes to is the trigger substitution `_SPREADSHEET_ID` (scratch copy until switched).
