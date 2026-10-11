#!/usr/bin/env bash
# Run one command against the dedicated LLMOps AWS account - and only that account.
#
#   export LLMOPS_AWS_PROFILE=llmops-sandbox      # named profile (SSO or role) of the dedicated account
#   export LLMOPS_ACCOUNT_ID=111122223333         # that account's id
#   scripts/in_account.sh aws cloudformation deploy --stack-name llmops-data ...
#   scripts/in_account.sh agentcore deploy
#
# Before running the command it checks, and refuses on any mismatch:
#   * the profile's caller identity is LLMOPS_ACCOUNT_ID (sts get-caller-identity);
#   * agentcore/aws-targets.json, when present, deploys to that same account;
#   * the command does not carry its own --profile (which would bypass the check).
# The command then runs with AWS_PROFILE set to that profile and every other AWS credential variable cleared, so
# keys exported for another account (for example a personal one) cannot leak into it.
set -euo pipefail

die() { echo "in_account: $*" >&2; exit 2; }
other_accounts() {  # accounts in an aws-targets.json other than $2, space separated
  python3 -I - "$1" "$2" <<'PY'
import json, sys
targets = json.load(open(sys.argv[1]))
print(" ".join(sorted({str(t.get("account")) for t in targets} - {sys.argv[2]})))
PY
}

[ $# -gt 0 ] || die "usage: scripts/in_account.sh <command> [args...]"
: "${LLMOPS_AWS_PROFILE:?set LLMOPS_AWS_PROFILE to the AWS CLI profile of the dedicated account}"
: "${LLMOPS_ACCOUNT_ID:?set LLMOPS_ACCOUNT_ID to the dedicated account id (12 digits)}"
[[ "$LLMOPS_ACCOUNT_ID" =~ ^[0-9]{12}$ ]] || die "LLMOPS_ACCOUNT_ID must be 12 digits"
for a in "$@"; do
  case "$a" in --profile|--profile=*) die "do not pass --profile; the profile comes from LLMOPS_AWS_PROFILE" ;; esac
done

# Only the dedicated profile may supply credentials.
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_SECURITY_TOKEN AWS_DEFAULT_PROFILE
export AWS_PROFILE="$LLMOPS_AWS_PROFILE"

actual="$(aws sts get-caller-identity --query Account --output text 2>/dev/null)" \
  || die "cannot read the caller identity of profile '$AWS_PROFILE' (run: aws sso login --profile $AWS_PROFILE)"
[ "$actual" = "$LLMOPS_ACCOUNT_ID" ] \
  || die "profile '$AWS_PROFILE' is account $actual, not the dedicated account $LLMOPS_ACCOUNT_ID - nothing was run"

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
targets="$root/agentcore/aws-targets.json"
if [ -f "$targets" ]; then
  # every deployment target must name the dedicated account
  others="$(other_accounts "$targets" "$LLMOPS_ACCOUNT_ID")" || die "cannot read $targets"
  [ -z "$others" ] || die "agentcore/aws-targets.json also targets account(s) $others - fix it before deploying"
fi

echo "in_account: account $actual (profile $AWS_PROFILE)" >&2
exec "$@"
