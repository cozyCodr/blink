#!/usr/bin/env bash
# Seed the demo workspace on Blink so every horizon level has something to show
# BEFORE recording. Run against the live service (default) or local.
#
#   bash deployment/seed_demo.sh                 # seeds https://blink.oapps.dev
#   BASE=http://localhost:8000 bash deployment/seed_demo.sh
#
# What it does (workspace ws_demo — the one the web app uses):
#   1. A goal through the FULL elicitation loop (fills the profile: platforms,
#      level, 6 h/week, "6 months") -> Gemini synthesizes a sequenced plan and
#      the scheduler places it. This is also the flow you demo live, so seeding
#      a SECOND workspace-warming goal keeps the recorded one fresh: we seed
#      concrete tasks here instead, and leave the big vague goal for on-camera.
#   2. Two milestones with target dates on the seeded commitment, so quarter
#      shows diamonds + the pacing sentence and year shows nodes + the star.
#
# Idempotency note: replans replace planned blocks (no duplicates), but the
# commitment/milestone POSTs append. For a clean slate, restart the service
# (in-memory store) or use a fresh workspace id.
set -euo pipefail

BASE="${BASE:-https://blink.oapps.dev}"
WS="${WS:-ws_demo}"
API="$BASE/v1/workspaces/$WS"

say() { printf '\n== %s\n' "$*"; }

say "1/4 concrete tasks -> commitment + scheduled blocks"
curl -sS -m 90 -X POST "$API/turn" -H "Content-Type: application/json" \
  -d '{"message":"add: write the project report for two hours, review pull requests for one hour, gym session for one hour, prep the sprint demo for ninety minutes"}' \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('   planned:',d.get('tasks'),'tasks,',(d.get('schedule') or {}).get('blocks_scheduled'),'blocks')"

say "2/4 profile (pacing needs hours_per_week + target_timeline)"
for kv in '"field":"platforms","value":["YouTube","Coursera"]' \
          '"field":"current_level","value":"intermediate"' \
          '"field":"hours_per_week","value":6' \
          '"field":"target_timeline","value":"6 months"'; do
  curl -sS -m 40 -X POST "$API/elicit/answer" -H "Content-Type: application/json" \
    -d "{\"commitment_id\":\"seed\",\"goal\":\"profile seed\",$kv}" -o /dev/null
done
echo "   profile set (6h/week, 6 months)"

say "3/4 milestones on the seeded commitment"
CID=$(curl -sS -m 20 "$API/details" | python3 -c "import sys,json;c=json.load(sys.stdin)['commitments'];print(c[0]['id'] if c else '')")
if [ -n "$CID" ]; then
  curl -sS -m 20 -X POST "$API/milestones" -H "Content-Type: application/json" \
    -d "{\"title\":\"Foundations locked\",\"horizon\":\"quarter\",\"target_hours\":40,\"target_date\":\"2026-10-15\",\"commitment_id\":\"$CID\"}" -o /dev/null
  curl -sS -m 20 -X POST "$API/milestones" -H "Content-Type: application/json" \
    -d "{\"title\":\"Portfolio project shipped\",\"horizon\":\"quarter\",\"target_hours\":60,\"target_date\":\"2026-12-20\",\"commitment_id\":\"$CID\"}" -o /dev/null
  echo "   2 milestones on $CID (Oct 15 / Dec 20)"
else
  echo "   WARNING: no commitment found; milestones skipped"
fi

say "4/4 sanity"
curl -sS -m 20 "$API/details?days=35" | python3 -c "
import sys,json
d=json.load(sys.stdin)
print('   commitments:',len(d['commitments']),'| planned blocks:',len([b for b in d['blocks'] if b['status']=='planned']),'| milestones:',len(d['milestones']))
print('   profile hours/wk:',(d.get('profile') or {}).get('hours_per_week'),'| ledger days:',len(d['ledger_days']))
sr=d.get('schedule_report') or {}
print('   utilization:',sr.get('utilization_pct'),'%')"

echo
echo "Seeded. Demo arc: on camera, give Blink the BIG vague goal by voice"
echo "(\"I want to become a data scientist\") so judges see elicitation ->"
echo "synthesis -> the heart -> the morph, on top of this baseline data."
