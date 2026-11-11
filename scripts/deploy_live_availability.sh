#!/usr/bin/env bash
# Builds the image and deploys the Cloud Run job 'live-availability'.
# Start with SPREADSHEET_ID = a SCRATCH copy; switch to production only after a clean run.
set -euo pipefail
: "${PROJECT:?}"; : "${REGION:?}"; : "${SA:?}"; : "${SPREADSHEET_ID:?}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/airbnb-hq/live-availability:$(git rev-parse --short HEAD)"
gcloud builds submit --project "$PROJECT" --tag "$IMAGE" .
secrets=$(python3 -c "import json;print(','.join('ICAL_URL_%s=%s:latest'%(l['key'],l['secret']) for l in json.load(open('config/listings.json'))['listings']))")
gcloud run jobs deploy live-availability --project "$PROJECT" --region "$REGION" \
  --image "$IMAGE" --service-account "$SA" --max-retries 1 --task-timeout 300 \
  --set-env-vars "SPREADSHEET_ID=${SPREADSHEET_ID},WRITE_ALLOWED_IDS=${SPREADSHEET_ID},HORIZON_DAYS=60" \
  --set-secrets "$secrets"
echo "Run once:  gcloud run jobs execute live-availability --project $PROJECT --region $REGION --wait"
echo "Dry run:   gcloud run jobs execute live-availability --project $PROJECT --region $REGION --args=--dry-run --wait"
