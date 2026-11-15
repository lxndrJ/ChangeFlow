#!/usr/bin/env bash
# Deploys the static UI to the website bucket and wires it to the deployed API.
# Usage: ./scripts/deploy-frontend.sh [stack-name] [region]
set -euo pipefail

STACK_NAME="${1:-changeflow}"
AWS_REGION="${2:-$(aws configure get region 2>/dev/null || true)}"
AWS_REGION="${AWS_REGION:-eu-central-1}"

get_output() {
  aws cloudformation describe-stacks \
    --stack-name "$STACK_NAME" --region "$AWS_REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}

API_ENDPOINT="$(get_output ApiEndpoint)"
BUCKET="$(get_output WebsiteBucketName)"

cat > frontend/config.js <<EOF
window.CHANGEFLOW_CONFIG = { apiBase: "${API_ENDPOINT}" };
EOF

aws s3 sync frontend/ "s3://${BUCKET}" --delete --region "$AWS_REGION"

echo
echo "  Website:  $(get_output WebsiteURL)"
echo "  API:      ${API_ENDPOINT}"
