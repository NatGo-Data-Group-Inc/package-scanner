#!/usr/bin/env bash
set -euo pipefail

REGION="us-east-1"
PROFILE=""
ALLOW_DEFAULT_PROFILE="false"
STACK_NAME="package-scanner-webapp-dev"
ACTION=""
LEASE_MINUTES="60"
WAIT_TIMEOUT_SECONDS="600"
CUSTOM_DOMAIN_NAME=""
CUSTOM_DOMAIN_HOSTED_ZONE_ID=""
TLS_CERTIFICATE_ARN=""
CLEAR_CUSTOM_DOMAIN="false"
CLEAR_TLS_CERTIFICATE="false"

usage() {
  cat <<'EOF'
Usage: set-webapp-runtime.sh --action <enable|disable|reconcile> [options]

Options:
  --action <enable|disable|reconcile>
  --lease-minutes <minutes>      (default: 60; used by enable)
  --wait-timeout-seconds <secs>  (default: 600; used by enable)
  --custom-domain-name <name>    (optional; used by enable)
  --custom-domain-hosted-zone-id <zone-id>
  --tls-certificate-arn <arn>    (optional; used by enable)
  --clear-custom-domain          (used by enable)
  --clear-tls-certificate        (used by enable)
  --stack-name <name>
  --region <region>
  --profile <profile>
  --allow-default-profile
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --action) ACTION="$2"; shift 2 ;;
    --lease-minutes) LEASE_MINUTES="$2"; shift 2 ;;
    --wait-timeout-seconds) WAIT_TIMEOUT_SECONDS="$2"; shift 2 ;;
    --custom-domain-name) CUSTOM_DOMAIN_NAME="$2"; shift 2 ;;
    --custom-domain-hosted-zone-id) CUSTOM_DOMAIN_HOSTED_ZONE_ID="$2"; shift 2 ;;
    --tls-certificate-arn) TLS_CERTIFICATE_ARN="$2"; shift 2 ;;
    --clear-custom-domain) CLEAR_CUSTOM_DOMAIN="true"; shift 1 ;;
    --clear-tls-certificate) CLEAR_TLS_CERTIFICATE="true"; shift 1 ;;
    --stack-name) STACK_NAME="$2"; shift 2 ;;
    --region) REGION="$2"; shift 2 ;;
    --profile) PROFILE="$2"; shift 2 ;;
    --allow-default-profile) ALLOW_DEFAULT_PROFILE="true"; shift 1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 1 ;;
  esac
done

if [[ -z "${ACTION}" ]]; then
  echo "--action is required." >&2
  usage >&2
  exit 1
fi
if [[ "${ALLOW_DEFAULT_PROFILE}" != "true" && -z "${PROFILE}" ]]; then
  echo "Guardrail: --profile is required unless --allow-default-profile is explicitly set." >&2
  exit 1
fi
if [[ -n "${CUSTOM_DOMAIN_NAME}" && -z "${CUSTOM_DOMAIN_HOSTED_ZONE_ID}" ]]; then
  echo "Guardrail: --custom-domain-hosted-zone-id is required when --custom-domain-name is set." >&2
  exit 1
fi
if [[ -z "${CUSTOM_DOMAIN_NAME}" && -n "${CUSTOM_DOMAIN_HOSTED_ZONE_ID}" ]]; then
  echo "Guardrail: --custom-domain-name is required when --custom-domain-hosted-zone-id is set." >&2
  exit 1
fi
if [[ "${CLEAR_CUSTOM_DOMAIN}" == "true" && ( -n "${CUSTOM_DOMAIN_NAME}" || -n "${CUSTOM_DOMAIN_HOSTED_ZONE_ID}" ) ]]; then
  echo "Guardrail: --clear-custom-domain cannot be combined with custom domain values." >&2
  exit 1
fi
if [[ "${CLEAR_TLS_CERTIFICATE}" == "true" && -n "${TLS_CERTIFICATE_ARN}" ]]; then
  echo "Guardrail: --clear-tls-certificate cannot be combined with --tls-certificate-arn." >&2
  exit 1
fi

AWS_ARGS=(--region "${REGION}")
if [[ -n "${PROFILE}" ]]; then
  AWS_ARGS+=(--profile "${PROFILE}")
fi

FUNCTION_NAME="$(
  aws cloudformation describe-stacks \
    --stack-name "${STACK_NAME}" \
    --query "Stacks[0].Outputs[?OutputKey=='WebappControllerFunctionName'].OutputValue | [0]" \
    --output text \
    "${AWS_ARGS[@]}"
)"

if [[ -z "${FUNCTION_NAME}" || "${FUNCTION_NAME}" == "None" ]]; then
  echo "Could not resolve WebappControllerFunctionName from stack ${STACK_NAME}." >&2
  exit 1
fi

PAYLOAD="$(
  ACTION="${ACTION}" \
  LEASE_MINUTES="${LEASE_MINUTES}" \
  CUSTOM_DOMAIN_NAME="${CUSTOM_DOMAIN_NAME}" \
  CUSTOM_DOMAIN_HOSTED_ZONE_ID="${CUSTOM_DOMAIN_HOSTED_ZONE_ID}" \
  TLS_CERTIFICATE_ARN="${TLS_CERTIFICATE_ARN}" \
  CLEAR_CUSTOM_DOMAIN="${CLEAR_CUSTOM_DOMAIN}" \
  CLEAR_TLS_CERTIFICATE="${CLEAR_TLS_CERTIFICATE}" \
  python - <<'PY'
import json
import os

payload = {
    "action": os.environ["ACTION"],
    "lease_minutes": int(os.environ["LEASE_MINUTES"]),
}

if os.environ["CUSTOM_DOMAIN_NAME"]:
    payload["custom_domain_name"] = os.environ["CUSTOM_DOMAIN_NAME"]
    payload["custom_domain_hosted_zone_id"] = os.environ["CUSTOM_DOMAIN_HOSTED_ZONE_ID"]

if os.environ["TLS_CERTIFICATE_ARN"]:
    payload["tls_certificate_arn"] = os.environ["TLS_CERTIFICATE_ARN"]

if os.environ["CLEAR_CUSTOM_DOMAIN"] == "true":
    payload["clear_custom_domain"] = True

if os.environ["CLEAR_TLS_CERTIFICATE"] == "true":
    payload["clear_tls_certificate"] = True

print(json.dumps(payload, separators=(",", ":")))
PY
)"

aws lambda invoke \
  --function-name "${FUNCTION_NAME}" \
  --cli-binary-format raw-in-base64-out \
  --payload "${PAYLOAD}" \
  /dev/stdout \
  "${AWS_ARGS[@]}"
echo

if [[ "${ACTION}" == "enable" ]]; then
  end_epoch="$(( $(date +%s) + WAIT_TIMEOUT_SECONDS ))"
  while true; do
    STACK_STATUS="$(
      aws cloudformation describe-stacks \
        --stack-name "${STACK_NAME}" \
        --query "Stacks[0].StackStatus" \
        --output text \
        "${AWS_ARGS[@]}"
    )"
    if [[ "${STACK_STATUS}" != *_IN_PROGRESS ]]; then
      break
    fi
    if (( $(date +%s) >= end_epoch )); then
      echo "Timed out waiting for ${STACK_NAME} to finish starting the webapp runtime." >&2
      exit 1
    fi
    sleep 10
  done

  if [[ "${STACK_STATUS}" != "UPDATE_COMPLETE" && "${STACK_STATUS}" != "CREATE_COMPLETE" ]]; then
    echo "Webapp runtime enable did not complete successfully. stack_status=${STACK_STATUS}" >&2
    exit 1
  fi

  WEBAPP_URL="$(
    aws cloudformation describe-stacks \
      --stack-name "${STACK_NAME}" \
      --query "Stacks[0].Outputs[?OutputKey=='WebappUrl'].OutputValue | [0]" \
      --output text \
      "${AWS_ARGS[@]}"
  )"

  if [[ -z "${WEBAPP_URL}" || "${WEBAPP_URL}" == "None" ]]; then
    echo "Webapp runtime started, but WebappUrl is blank." >&2
    exit 1
  fi

  echo "WebappUrl=${WEBAPP_URL}"
fi
