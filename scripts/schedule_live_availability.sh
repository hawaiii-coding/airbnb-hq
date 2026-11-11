#!/usr/bin/env bash
# Daily at 07:30 HCMC, plus 13:30 and 19:30. Created PAUSED; resume after the first clean run.
set -euo pipefail
: "${PROJECT:?}"; : "${REGION:?}"; : "${SA:?}"
gcloud scheduler jobs create http live-availability-3x-daily --project "$PROJECT" --location "$REGION" \
  --schedule "30 7,13,19 * * *" --time-zone "Asia/Ho_Chi_Minh" \
  --uri "https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT}/jobs/live-availability:run" \
  --http-method POST --oauth-service-account-email "$SA"
gcloud scheduler jobs pause live-availability-3x-daily --project "$PROJECT" --location "$REGION"
