#!/usr/bin/env bash
set -euo pipefail

# Registers a new ECS task definition revision for the R linux scanner using an immutable image tag.
# Usage:
#   scripts/register-r-task-def.sh --image 807497180525.dkr.ecr.us-east-1.amazonaws.com/package-scanner-dev/r-scan-linux:<tag> \
#     [--region us-east-1] [--profile PROFILE]
#
# The Step Functions orchestrator already targets the latest revision of the task definition family,
# so once this script succeeds the next scan run will use the freshly registered image tag.

REGION="us-east-1"
PROFILE=""
IMAGE=""
FAMILY="package-scanner-dev-r-linux-amd64"
TASK_ROLE_ARN="arn:aws:iam::807497180525:role/package-scanner-dev-r-task-role"
EXEC_ROLE_ARN="arn:aws:iam::807497180525:role/package-scanner-dev-r-task-execution-role"
LOG_GROUP="/ecs/package-scanner-dev/r/linux-amd64"

usage() {
  cat <<'EOF'
Usage: register-r-task-def.sh --image <ecr-uri[:tag]> [options]

Required:
  --image <uri>         Full ECR image URI (include immutable tag)

Optional:
  --region <region>     Default: us-east-1
  --profile <profile>   AWS CLI profile
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --image) IMAGE="$2"; shift 2 ;;
    --region) REGION="$2"; shift 2 ;;
    --profile) PROFILE="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 1 ;;
  esac
done

if [[ -z "${IMAGE}" ]]; then
  echo "--image is required." >&2
  usage >&2
  exit 1
fi

AWS_ARGS=(--region "${REGION}")
if [[ -n "${PROFILE}" ]]; then
  AWS_ARGS+=(--profile "${PROFILE}")
fi

read -r TASK_DEF_ARN REVISION <<EOF
$(aws ecs register-task-definition \
  --family "${FAMILY}" \
  --network-mode bridge \
  --requires-compatibilities EC2 \
  --cpu 4096 \
  --memory 8192 \
  --task-role-arn "${TASK_ROLE_ARN}" \
  --execution-role-arn "${EXEC_ROLE_ARN}" \
  --container-definitions "[
    {
      \"name\": \"scanner\",
      \"image\": \"${IMAGE}\",
      \"essential\": true,
      \"command\": [\"linux-amd64\"],
      \"environment\": [{\"name\": \"SCRIPT_ROOT\", \"value\": \"/opt/package-scanner/scripts\"}],
      \"logConfiguration\": {
        \"logDriver\": \"awslogs\",
        \"options\": {
          \"awslogs-group\": \"${LOG_GROUP}\",
          \"awslogs-region\": \"${REGION}\",
          \"awslogs-stream-prefix\": \"scanner\"
        }
      }
    }
  ]" \
  --query 'taskDefinition.{arn:taskDefinitionArn,rev:revision}' \
  --output text \
  "${AWS_ARGS[@]}") 
EOF

echo "Registered task definition:"
echo "  Family:    ${FAMILY}"
echo "  Image:     ${IMAGE}"
echo "  Revision:  ${REVISION}"
echo "  ARN:       ${TASK_DEF_ARN}"
echo
echo "State machines that use family ${FAMILY} and 'LATEST' will pick this up automatically."
