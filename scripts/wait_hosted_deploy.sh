#!/usr/bin/env bash
# Wait until Alexa has deployed the hosted clone's HEAD.
#
# On FAILED, push an empty commit and try again, up to two retries. If a push
# never shows up as a deploy within 15 minutes, exit 1. The console Deploy
# button is the fallback in that case.
#
# Usage: scripts/wait_hosted_deploy.sh HOSTED_DIR PUSHED SUBJECT
#   PUSHED   "true" when this job already pushed a commit
#   SUBJECT  short phrase used in the empty retry commit
# Env: SKILL_ID
# nounset stays off: a deploy status with no commit id leaves $2 empty,
# matching the refresh workflow this script was taken from.
set -eo pipefail

hosted=$(cd "$1" && pwd)
pushed=$2
subject=$3

if [ -z "${SKILL_ID:-}" ]; then
  echo "SKILL_ID is required" >&2
  exit 1
fi

cd "$hosted"

deploy_state() {
  ask smapi get-skill-status -s "$SKILL_ID" \
    | jq -r '.hostedSkillDeployment.lastUpdateRequest | "\(.status) \(.deploymentDetails.commitId // "")"'
}

redeploy() {
  git -c user.name=Artem -c user.email=artem.rebets@gmail.com \
    commit --quiet --allow-empty -m "Redeploy $subject (retry $1)"
  git push origin HEAD:master
}

attempt=1
set -- $(deploy_state)
if [ "$2" = "$(git rev-parse HEAD)" ] && [ "$1" = "SUCCEEDED" ]; then
  echo "Hosted master $2 is deployed."
  exit 0
fi
if [ "$pushed" != "true" ]; then
  echo "Hosted master is not the deployed commit ($*); redeploying."
  attempt=2
  redeploy 1
fi
while :; do
  commit=$(git rev-parse HEAD)
  result=timeout
  for i in $(seq 1 60); do
    set -- $(deploy_state)
    echo "$(date -u +%H:%M:%S) $*"
    if [ "$2" = "$commit" ] && [ "$1" != "IN_PROGRESS" ]; then result=$1; break; fi
    sleep 15
  done
  case "$result" in
    SUCCEEDED) echo "Deployed $commit."; exit 0 ;;
    timeout)
      echo "::error::The push did not trigger a hosted deploy within 15 minutes. Open the Alexa developer console, Code tab, and click Deploy."
      exit 1 ;;
  esac
  if [ "$attempt" -ge 3 ]; then
    echo "::error::The hosted deploy failed $attempt times. Check the Code tab in the Alexa developer console."
    exit 1
  fi
  echo "Deploy of $commit ended $result; retrying with an empty commit."
  redeploy "$attempt"
  attempt=$((attempt + 1))
done
