#!/usr/bin/env bash
# Install the ASK CLI pre-push hook in a clone of the Alexa-hosted repository,
# the way `ask init` does. Needs the files the ask-cli action downloads.
#
# Usage: scripts/install_hosted_hook.sh HOSTED_DIR
# Env: SKILL_ID
set -euo pipefail

hosted=$1
test -n "${SKILL_ID:-}" || { echo "SKILL_ID is required" >&2; exit 1; }
test -f ~/.ask/git-hooks/pre-push || { echo "Run the ask-cli action first" >&2; exit 1; }

install -m 700 ~/.ask/git-hooks/pre-push "$hosted/.git/hooks/pre-push"
mkdir -p "$hosted/.ask"
printf '{"askcliStatesVersion":"2020-03-31","profiles":{"default":{"skillId":"%s"}}}\n' \
  "$SKILL_ID" > "$hosted/.ask/ask-states.json"
git -C "$hosted" check-ignore -q .ask/ask-states.json
