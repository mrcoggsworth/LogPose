#!/usr/bin/env bash
# seed-demo.sh — inject sample alerts into the LogPose demo stack.
#
# Usage:
#   bash docker/seed-demo.sh [cloudtrail|gcp|all]
#
# Defaults to "all" if no argument is given.
#
# The Universal HTTP consumer must be running (http://localhost:8090).
# Watch the full flow with:
#   docker logs logpose-splunk-hec-mock -f

set -euo pipefail

BASE_URL="http://localhost:8090/ingest"
TARGET="${1:-all}"

inject() {
  local label="$1"
  local payload="$2"
  echo ">>> Injecting: $label"
  curl -s -X POST "$BASE_URL" \
    -H "Content-Type: application/json" \
    -d "$payload" | python3 -m json.tool
  echo
}

cloudtrail_alert() {
  inject "AWS CloudTrail — ConsoleLogin" '{
    "raw_payload": {
      "eventVersion": "1.08",
      "eventTime": "2024-11-01T18:23:45Z",
      "eventSource": "signin.amazonaws.com",
      "eventName": "ConsoleLogin",
      "awsRegion": "us-east-1",
      "sourceIPAddress": "198.51.100.7",
      "userIdentity": {
        "type": "IAMUser",
        "userName": "demo-user",
        "arn": "arn:aws:iam::123456789012:user/demo-user"
      },
      "responseElements": {"ConsoleLogin": "Success"}
    }
  }'
}

gcp_alert() {
  inject "GCP Event Audit — SetIamPolicy" '{
    "raw_payload": {
      "logName": "projects/logpose-dev/logs/cloudaudit.googleapis.com%2Factivity",
      "severity": "NOTICE",
      "resource": {
        "type": "project",
        "labels": {"project_id": "logpose-dev"}
      },
      "protoPayload": {
        "@type": "type.googleapis.com/google.cloud.audit.AuditLog",
        "methodName": "SetIamPolicy",
        "authenticationInfo": {"principalEmail": "admin@example.com"},
        "requestMetadata": {"callerIp": "203.0.113.42"}
      },
      "timestamp": "2024-11-01T18:23:45Z"
    }
  }'
}

case "$TARGET" in
  cloudtrail) cloudtrail_alert ;;
  gcp)        gcp_alert ;;
  all)        cloudtrail_alert; sleep 1; gcp_alert ;;
  *)
    echo "Usage: $0 [cloudtrail|gcp|all]"
    exit 1
    ;;
esac

echo "Done. Watch enriched alerts:"
echo "  docker logs logpose-splunk-hec-mock -f"
