#!/usr/bin/env bash
# Smoke-test the live Alexa-hosted skill endpoint.
#
# Pushing to prod triggers Amazon's automated security tests (up to ~5 minutes)
# before the live Lambda switches over. When PROMOTED is "true", this script
# polls the live endpoint for up to 10 minutes until the smoke test passes.
# When PROMOTED is "false", it still runs the smoke test once to confirm the
# live skill is healthy.
#
# Usage: scripts/wait_live_smoke.sh PROMOTED [DATE]
#   PROMOTED  "true" when this run just pushed to prod
#   DATE      optional YYYY-MM-DD (e.g. feed_end) to pin the smoke test date
# Env: SKILL_ID (optional: LIVE_SMOKE_ATTEMPTS, LIVE_SMOKE_SLEEP)
set -eo pipefail

promoted=${1:-false}
check_date=${2:-}
script_dir=$(cd "$(dirname "$0")" && pwd)

if [ -z "${SKILL_ID:-}" ]; then
  echo "SKILL_ID is required" >&2
  exit 1
fi

write_args=(request-live.json)
if [ -n "$check_date" ]; then
  write_args+=(--date "$check_date")
fi
python3 "$script_dir/smoke_test.py" write "${write_args[@]}"

run_once() {
  if ! ask smapi invoke-skill-end-point -s "$SKILL_ID" -g live --endpoint-region EU \
    --skill-request-body file:request-live.json > response-live.json; then
    cat response-live.json
    return 1
  fi
  python3 "$script_dir/smoke_test.py" check response-live.json
}

if [ "$promoted" != "true" ]; then
  run_once
  exit 0
fi

# Wait up to 10 minutes (30 x 20s) for Amazon's automated security validation
# and live Lambda cutover.
max_attempts=${LIVE_SMOKE_ATTEMPTS:-30}
sleep_seconds=${LIVE_SMOKE_SLEEP:-20}
for attempt in $(seq 1 "$max_attempts"); do
  echo "$(date -u +%H:%M:%S) live smoke test attempt ${attempt}/${max_attempts}"
  if run_once; then
    exit 0
  fi
  if [ "$attempt" -lt "$max_attempts" ]; then
    sleep "$sleep_seconds"
  fi
done

echo "::error::The live skill did not pass the smoke test within 10 minutes of pushing to prod. Check the Alexa Developer Console and your Amazon developer email for security-test feedback."
exit 1
