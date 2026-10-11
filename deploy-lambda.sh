#!/usr/bin/env bash
# deploy-lambda.sh - package + deploy the earthworm-news-feeds Lambda and its Function URL.
# Idempotent: creates on first run, updates code afterwards. Needs the AWS CLI configured
# (`aws configure`, region ap-southeast-2) - this script cannot create an account or keys.
#
#   BRAIN_TOKEN=<long-random-secret> ./deploy-lambda.sh
# Optional: AWS_REGION (default ap-southeast-2), FN_NAME (default earthworm-news-feeds),
#           ROLE_ARN (reuse an execution role; else a basic one is created).
#
# Free tier: Lambda 1M requests + 400k GB-s / month, perpetual. Four calls a day at
# 256 MB x ~40 s = ~1,200 GB-s / month. Function URLs are free. Set a $1 budget alarm anyway.
set -euo pipefail
FN_NAME="${FN_NAME:-earthworm-news-feeds}"
REGION="${AWS_REGION:-ap-southeast-2}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ZIP="/tmp/${FN_NAME}.zip"
: "${BRAIN_TOKEN:?Set BRAIN_TOKEN=<the shared secret the estate will send as x-brain-token>}"
command -v aws >/dev/null || { echo "aws CLI not found - install + 'aws configure' first"; exit 1; }

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
echo "== account $ACCOUNT  region $REGION  fn $FN_NAME =="
rm -f "$ZIP"
python3 - "$ZIP" "$HERE" <<'EOF'
import sys, zipfile, os
z = zipfile.ZipFile(sys.argv[1], "w", zipfile.ZIP_DEFLATED)
for f in ("lambda_function.py", "fetch_news.py", "feeds.json"):
    z.write(os.path.join(sys.argv[2], f), f)
z.close()
EOF
[[ -s "$ZIP" ]] || { echo "failed to build $ZIP" >&2; exit 1; }

if [[ -z "${ROLE_ARN:-}" ]]; then
  ROLE_NAME="${FN_NAME}-role"
  if ! aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
    echo "== creating execution role $ROLE_NAME =="
    aws iam create-role --role-name "$ROLE_NAME" \
      --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}' >/dev/null
    aws iam attach-role-policy --role-name "$ROLE_NAME" \
      --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole >/dev/null
    echo "   waiting 10s for the role to propagate"; sleep 10
  fi
  ROLE_ARN=$(aws iam get-role --role-name "$ROLE_NAME" --query Role.Arn --output text)
fi
echo "   role: $ROLE_ARN"

if aws lambda get-function --function-name "$FN_NAME" --region "$REGION" >/dev/null 2>&1; then
  echo "== updating code =="
  aws lambda update-function-code --function-name "$FN_NAME" --zip-file "fileb://$ZIP" --region "$REGION" >/dev/null
  aws lambda wait function-updated --function-name "$FN_NAME" --region "$REGION"
  aws lambda update-function-configuration --function-name "$FN_NAME" --region "$REGION" \
    --timeout 90 --memory-size 256 --environment "Variables={BRAIN_TOKEN=$BRAIN_TOKEN}" >/dev/null
else
  echo "== creating function =="
  aws lambda create-function --function-name "$FN_NAME" --region "$REGION" \
    --runtime python3.12 --role "$ROLE_ARN" --handler lambda_function.lambda_handler \
    --timeout 90 --memory-size 256 --zip-file "fileb://$ZIP" \
    --environment "Variables={BRAIN_TOKEN=$BRAIN_TOKEN}" >/dev/null
  aws lambda wait function-active --function-name "$FN_NAME" --region "$REGION"
fi

if ! aws lambda get-function-url-config --function-name "$FN_NAME" --region "$REGION" >/dev/null 2>&1; then
  echo "== creating Function URL =="
  aws lambda create-function-url-config --function-name "$FN_NAME" --region "$REGION" --auth-type NONE >/dev/null
  aws lambda add-permission --function-name "$FN_NAME" --region "$REGION" \
    --statement-id FunctionURLAllowPublicAccess --action lambda:InvokeFunctionUrl \
    --principal "*" --function-url-auth-type NONE >/dev/null || true
fi
URL=$(aws lambda get-function-url-config --function-name "$FN_NAME" --region "$REGION" --query FunctionUrl --output text)
echo
echo "DEPLOYED. Function URL: $URL"
echo "Test:  curl -s -H \"x-brain-token: \$BRAIN_TOKEN\" '${URL}?days=1' | head -c 400"
echo
echo "On CT185 add to /etc/landgrab/news-feeds.env (root:www-data 0640):"
echo "  NEWS_FEED_URLS=<github raw url>,${URL}?days=7"
echo "  NEWS_FEED_TOKEN=$BRAIN_TOKEN"
echo "then: systemctl start landgrab-news.service && tail -3 /opt/land-grab/backend/news.log"
