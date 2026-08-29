#!/usr/bin/env bash
# Deploy Blink to Cloud Run (focus-agent-506601; service name stays "focus-agent"
# to preserve the URL and the blink.oapps.dev / www.blink.oapps.dev domain mappings).
#
# PREREQUISITE — deploy identity:
#   gcloud is currently authed as agent-824@ (the app's Vertex SA), which CANNOT
#   deploy. Log in with the account that OWNS focus-agent-506601 first:
#       gcloud auth login
#   (or grant agent-824 roles/run.admin + roles/iam.serviceAccountUser +
#    roles/cloudbuild.builds.editor + roles/artifactregistry.writer +
#    roles/serviceusage.serviceUsageAdmin, then keep using it.)
#
# Then run this script. It builds from the Dockerfile via Cloud Build and deploys,
# with the runtime service account agent-824@ providing keyless Vertex access.
set -euo pipefail

PROJECT="focus-agent-506601"
REGION="us-central1"
SERVICE="focus-agent"
RUNTIME_SA="agent-824@${PROJECT}.iam.gserviceaccount.com"

# Enable the APIs the deploy needs (Cloud Run, Cloud Build, Artifact Registry).
gcloud services enable \
  run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  aiplatform.googleapis.com firestore.googleapis.com \
  --project "${PROJECT}"

# P2-01: durable state lives in the native-mode Firestore database "blink"
# (created once with: gcloud firestore databases create --database=blink
#  --location=nam5 --type=firestore-native). The runtime SA needs read/write.
gcloud projects add-iam-policy-binding "${PROJECT}" \
  --member "serviceAccount:${RUNTIME_SA}" \
  --role "roles/datastore.user" \
  --condition=None \
  --quiet >/dev/null

# Build from the Dockerfile (Cloud Build) and deploy. Scale-to-zero, capped.
gcloud run deploy "${SERVICE}" \
  --source . \
  --project "${PROJECT}" \
  --region "${REGION}" \
  --service-account "${RUNTIME_SA}" \
  --allow-unauthenticated \
  --quiet \
  --set-env-vars "GOOGLE_GENAI_USE_VERTEXAI=TRUE,GOOGLE_CLOUD_PROJECT=${PROJECT},GOOGLE_CLOUD_LOCATION=global,GOOGLE_OAUTH_CLIENT_ID=553785321909-6lrjgbtvpq2ki570vouqhuhp90oe6n5d.apps.googleusercontent.com,GOOGLE_OAUTH_REDIRECT_URI=https://blink.oapps.dev/oauth/callback,BLINK_FIRESTORE=1,FIRESTORE_DATABASE=blink,APNS_TEAM_ID=W893S8L2T5,APNS_TOPIC=dev.oapps.blink.companion" \
  `# P14: blink-session-secret signs the sign-in session cookie. Created once:` \
  `#   openssl rand -hex 32 | gcloud secrets create blink-session-secret --data-file=- --project focus-agent-506601` \
  `#   gcloud secrets add-iam-policy-binding blink-session-secret --member serviceAccount:agent-824@focus-agent-506601.iam.gserviceaccount.com --role roles/secretmanager.secretAccessor --project focus-agent-506601` \
  `# Missing secret = sign-in disabled with one log line; guest mode unaffected.` \
  `# P15-10 push: APNS_TEAM_ID and APNS_TOPIC are not secrets, so they ride` \
  `# --set-env-vars above; the .p8 key and its id do not.` \
  `#   gcloud secrets create blink-apns-key --data-file=AuthKey_XXXXXXXXXX.p8 --project focus-agent-506601` \
  `#   printf 'XXXXXXXXXX' | gcloud secrets create blink-apns-key-id --data-file=- --project focus-agent-506601` \
  `#   openssl rand -hex 32 | gcloud secrets create blink-sweep-secret --data-file=- --project focus-agent-506601` \
  `#   for s in blink-apns-key blink-apns-key-id blink-sweep-secret; do` \
  `#     gcloud secrets add-iam-policy-binding "$s" --member serviceAccount:agent-824@focus-agent-506601.iam.gserviceaccount.com --role roles/secretmanager.secretAccessor --project focus-agent-506601; done` \
  `# Missing APNs secrets = push disabled with one log line; everything else unaffected.` \
  `# Missing blink-sweep-secret = /internal/sweep answers 403 to everyone, which is the safe default.` \
  --set-secrets "GOOGLE_OAUTH_CLIENT_SECRET=blink-oauth-client-secret:latest,BLINK_SESSION_SECRET=blink-session-secret:latest,APNS_KEY_P8=blink-apns-key:latest,APNS_KEY_ID=blink-apns-key-id:latest,BLINK_SWEEP_SECRET=blink-sweep-secret:latest" \
  `# min-instances 1 = judging keep-alive (P9-06, 2026-08-26); drop to 0 after judging` \
  --min-instances 1 \
  --max-instances 3 \
  --memory 1Gi \
  --cpu 1 \
  --port 8080 \
  --timeout 120

echo
echo "Deployed. URL:"
gcloud run services describe "${SERVICE}" --project "${PROJECT}" --region "${REGION}" --format="value(status.url)"
