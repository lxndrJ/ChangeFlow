#!/usr/bin/env bash
# End-to-end smoke test against the deployed stack.
# Usage: ./scripts/smoke-test.sh [stack-name] [region]
set -euo pipefail

STACK_NAME="${1:-changeflow}"
AWS_REGION="${2:-$(aws configure get region 2>/dev/null || true)}"
AWS_REGION="${AWS_REGION:-eu-central-1}"

BASE="$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$AWS_REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='ApiEndpoint'].OutputValue" --output text)"

echo "API: $BASE"
echo
echo "1) Submitting a change request..."
REQ="$(curl -s -X POST "$BASE/requests" -H 'content-type: application/json' -d '{
  "partNumber": "SMOKE-TEST-001",
  "title": "Smoke test change",
  "description": "This request only exists to verify the end-to-end flow.",
  "impact": "LOW",
  "requestedBy": "smoke-test"
}')"
ID="$(echo "$REQ" | jq -r '.request.requestId')"
echo "   created: $ID"

echo "2) Waiting for the workflow to park the approval callback..."
sleep 4
curl -s "$BASE/requests/$ID" | jq '{status: .request.status, events: [.request.history[].event]}'

echo "3) Approving..."
curl -s -X POST "$BASE/requests/$ID/decision" -H 'content-type: application/json' \
  -d '{"decision": "approve", "by": "Smoke Tester", "comment": "LGTM"}' | jq .

echo "4) Waiting for execution..."
for _ in $(seq 1 10); do
  STATUS="$(curl -s "$BASE/requests/$ID" | jq -r '.request.status')"
  echo "   status: $STATUS"
  [ "$STATUS" = "EXECUTED" ] && break
  sleep 3
done

echo "5) Final state:"
curl -s "$BASE/requests/$ID" | jq '{status: .request.status, events: [.request.history[].event]}'

[ "$STATUS" = "EXECUTED" ] && echo "OK: end-to-end flow works." || { echo "FAIL: expected EXECUTED"; exit 1; }
