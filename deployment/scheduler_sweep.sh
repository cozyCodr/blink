#!/usr/bin/env bash
# The Cloud Scheduler job behind Blink's push notifications (P15-10, Gap 3).
#
# Cloud Run has no cron. This job is the clock: every five minutes it pokes
# POST /internal/sweep, and the server decides — per workspace, in the USER'S
# local day — whether a nudge, a morning brief or an evening check-in is both
# due and true. The server owns every rule (the three-a-day budget, the fifteen
# minute gap, the once-per-day ledger); this job owns nothing but the tick.
#
# NOT RUN BY THE DEPLOY. Run it once, by hand, after `deploy.sh` has put the
# secrets in place. Re-running is safe: the `update` fallback at the bottom
# handles a job that already exists.
#
# AUTHENTICATION. The endpoint is protected by a shared secret from Secret
# Manager, presented in the X-Blink-Sweep-Secret header and compared in
# constant time. It fails CLOSED: with BLINK_SWEEP_SECRET unset on the service,
# no request can match, so a half-configured deployment has a sweep nobody can
# fire rather than one anybody can. The service itself stays
# --allow-unauthenticated for the web app, which is why the endpoint carries
# its own credential rather than relying on Cloud Run's IAM.
set -euo pipefail

PROJECT="focus-agent-506601"
REGION="us-central1"
JOB="blink-push-sweep"
URL="https://blink.oapps.dev/internal/sweep"

# Read the SAME secret the service reads, so the two can never drift apart.
# It never lands in a file, a log, or this repo.
SWEEP_SECRET="$(gcloud secrets versions access latest \
  --secret blink-sweep-secret --project "${PROJECT}")"

gcloud services enable cloudscheduler.googleapis.com --project "${PROJECT}"

COMMON=(
  --project "${PROJECT}"
  --location "${REGION}"
  --schedule "*/5 * * * *"
  # The windows this drives ("before 10am", "after 5pm") are per-user and
  # resolved server-side from UserProfile.timezone, so the job's own zone is
  # arbitrary. UTC, so a reader is never tempted to think it means something.
  --time-zone "Etc/UTC"
  --uri "${URL}"
  --http-method POST
  --headers "X-Blink-Sweep-Secret=${SWEEP_SECRET},Content-Type=application/json"
  --message-body '{}'
  --attempt-deadline 60s
  # A missed tick is not an emergency: the next sweep five minutes later finds
  # exactly the same signals still due. So retry twice and move on rather than
  # hammering a service that is having a bad minute.
  --max-retry-attempts 2
  --max-backoff 60s
  --description "Blink: every 5 minutes, send the push signals that are due (P15-10)."
)

gcloud scheduler jobs create http "${JOB}" "${COMMON[@]}" \
  || gcloud scheduler jobs update http "${JOB}" "${COMMON[@]}"

echo
echo "Job installed. Fire one tick by hand with:"
echo "  gcloud scheduler jobs run ${JOB} --project ${PROJECT} --location ${REGION}"
echo "Then read the decision line:"
echo "  gcloud run services logs read focus-agent --project ${PROJECT} --region ${REGION} | grep 'push sweep'"
