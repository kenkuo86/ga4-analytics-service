#!/usr/bin/env bash

set -euo pipefail

PROJECT_ID="ga4-reports-dev"
REGION="asia-east1"
SERVICE="ga4-analytics-service"
SERVICE_ACCOUNT="ga4-analytics-service@ga4-reports-dev.iam.gserviceaccount.com"
SERVICE_URL="https://ga4-analytics-service-398991472921.asia-east1.run.app"
ALLOW_DIRTY=false

usage() {
  echo "Usage: scripts/deploy_poc.sh [--allow-dirty]"
  echo
  echo "Runs tests, deploys the PoC from source, routes 100% to LATEST,"
  echo "checks /health, and reports ERROR logs from the new revision."
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --allow-dirty)
      ALLOW_DIRTY=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ ! -x ".venv/bin/python" ]]; then
  echo "Missing .venv/bin/python. Create the project virtualenv first." >&2
  exit 1
fi

echo "Checking gcloud authentication..."
gcloud auth print-access-token >/dev/null

WORKTREE_STATUS="$(git status --porcelain)"
DIRTY_LABEL=""
if [[ -n "$WORKTREE_STATUS" ]]; then
  if [[ "$ALLOW_DIRTY" != true ]]; then
    echo "Refusing to deploy a dirty worktree:" >&2
    echo "$WORKTREE_STATUS" >&2
    echo "Commit the changes, or rerun with --allow-dirty after reviewing them." >&2
    exit 1
  fi
  DIRTY_LABEL="-dirty"
  echo "Warning: deploying a dirty worktree because --allow-dirty was provided." >&2
fi

echo "Running tests..."
.venv/bin/python -m unittest discover -s tests

PREVIOUS_REVISION="$(
  gcloud run services describe "$SERVICE" \
    --project="$PROJECT_ID" \
    --region="$REGION" \
    --format='value(status.latestReadyRevisionName)'
)"
COMMIT_SHA="$(git rev-parse --short HEAD)"
REVISION_SUFFIX="poc-$(date -u +%Y%m%d-%H%M%S)-${COMMIT_SHA}${DIRTY_LABEL}"
REVISION_NAME="${SERVICE}-${REVISION_SUFFIX}"

echo "Deploying revision ${REVISION_NAME}..."
gcloud run deploy "$SERVICE" \
  --source=. \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --service-account="$SERVICE_ACCOUNT" \
  --update-env-vars="BIGQUERY_BILLING_PROJECT=${PROJECT_ID}" \
  --revision-suffix="$REVISION_SUFFIX" \
  --quiet

# A previous rollback or no-traffic deployment pins traffic to a revision.
# Resetting to LATEST after a successful deployment keeps future PoC deploys simple.
echo "Routing 100% of traffic to LATEST..."
gcloud run services update-traffic "$SERVICE" \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --to-latest \
  --quiet

LATEST_READY_REVISION="$(
  gcloud run services describe "$SERVICE" \
    --project="$PROJECT_ID" \
    --region="$REGION" \
    --format='value(status.latestReadyRevisionName)'
)"
if [[ "$LATEST_READY_REVISION" != "$REVISION_NAME" ]]; then
  echo "Expected latest Ready revision ${REVISION_NAME}, got ${LATEST_READY_REVISION}." >&2
  echo "Rollback command:" >&2
  echo "gcloud run services update-traffic ${SERVICE} --project=${PROJECT_ID} --region=${REGION} --to-revisions=${PREVIOUS_REVISION}=100" >&2
  exit 1
fi

echo "Checking ${SERVICE_URL}/health..."
if ! curl \
  --fail \
  --silent \
  --show-error \
  --retry 8 \
  --retry-delay 3 \
  --retry-all-errors \
  --max-time 20 \
  "${SERVICE_URL}/health"; then
  echo >&2
  echo "Health check failed. Rollback command:" >&2
  echo "gcloud run services update-traffic ${SERVICE} --project=${PROJECT_ID} --region=${REGION} --to-revisions=${PREVIOUS_REVISION}=100" >&2
  exit 1
fi
echo

LOG_FILTER="resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"${SERVICE}\" AND resource.labels.revision_name=\"${REVISION_NAME}\" AND severity>=ERROR"
ERROR_LOGS="$(
  gcloud logging read "$LOG_FILTER" \
    --project="$PROJECT_ID" \
    --limit=20 \
    --order=desc \
    --format='value(timestamp,severity,textPayload,jsonPayload.message)'
)"
if [[ -n "$ERROR_LOGS" ]]; then
  echo "Deployment succeeded, but the new revision has ERROR logs:" >&2
  echo "$ERROR_LOGS" >&2
  echo "Rollback command:" >&2
  echo "gcloud run services update-traffic ${SERVICE} --project=${PROJECT_ID} --region=${REGION} --to-revisions=${PREVIOUS_REVISION}=100" >&2
  exit 1
fi

echo "Deployment verified: ${REVISION_NAME} is Ready and serving 100% of traffic."
echo "Note: this OAuth PoC keeps refresh tokens in process memory; users may need to reconnect after a deployment."
