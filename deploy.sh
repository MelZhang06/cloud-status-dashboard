#!/usr/bin/env bash
# Run from any directory. AWS_PROFILE selects your AWS CLI profile.
# Usage: ./deploy.sh | IMAGE_TAG=<existing-tag> ./deploy.sh --skip-build | ./deploy.sh destroy
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TF_DIR="$ROOT_DIR/terraform"
ACTION="${1:-deploy}"
case "$ACTION" in deploy|--skip-build|destroy) ;; *) echo "Usage: $0 [--skip-build|destroy]" >&2; exit 1;; esac
for cmd in aws terraform; do
  command -v "$cmd" >/dev/null || { echo "$cmd is required" >&2; exit 1; }
done
if [[ "$ACTION" != "destroy" ]]; then
  for cmd in python3 curl; do
    command -v "$cmd" >/dev/null || { echo "$cmd is required" >&2; exit 1; }
  done
  if [[ "$ACTION" == "deploy" ]]; then
    command -v docker >/dev/null || { echo "Docker is required" >&2; exit 1; }
    docker info >/dev/null
  elif [[ -z "${IMAGE_TAG:-}" ]]; then
    echo "--skip-build requires IMAGE_TAG set to an existing ECR image tag" >&2
    exit 1
  fi
fi
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
if [[ -n "${EXPECTED_AWS_ACCOUNT_ID:-}" && "$ACCOUNT_ID" != "$EXPECTED_AWS_ACCOUNT_ID" ]]; then
  echo "Wrong AWS account: expected $EXPECTED_AWS_ACCOUNT_ID, got $ACCOUNT_ID" >&2
  exit 1
fi
echo "AWS account: $ACCOUNT_ID | Profile: ${AWS_PROFILE:-default}"
cd "$TF_DIR"
terraform init -input=false
if [[ "$ACTION" == "destroy" ]]; then
  echo "This deletes all project infrastructure AND all images in its ECR repository."
  terraform destroy
  exit 0
fi
# One-time bootstrap only. Provision ECR before ECS can reference an uploaded image.
# Targeted apply is intentionally restricted to the initial ECR prerequisite.
if ! terraform state list | grep -qx 'aws_ecr_repository.app'; then
  echo "First deployment: review and approve creation of the ECR repository."
  terraform apply -target=aws_ecr_repository.app
fi
ECR_REPO="$(terraform output -raw ecr_repository_url)"
REGISTRY="${ECR_REPO%%/*}"
REPO_NAME="${ECR_REPO#*/}"
# The initial targeted apply saves the ECR output, but not unrelated outputs.
AWS_REGION="${REGISTRY#*.dkr.ecr.}"
AWS_REGION="${AWS_REGION%.amazonaws.com}"
# Cross-check the registry account before uploading any image.
if [[ "$REGISTRY" != "$ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com" ]]; then
  echo "Terraform state and active AWS account/region do not match: $REGISTRY" >&2
  exit 1
fi
IMAGE_TAG="${IMAGE_TAG:-$(date -u +%Y%m%dT%H%M%SZ)}"
echo "Image: $ECR_REPO:$IMAGE_TAG | Region: $AWS_REGION"
if [[ "$ACTION" == "deploy" ]]; then
  aws ecr get-login-password --region "$AWS_REGION" \
    | docker login --username AWS --password-stdin "$REGISTRY"
  docker build --pull --platform linux/amd64 -t "$ECR_REPO:$IMAGE_TAG" "$ROOT_DIR/app"
  docker push "$ECR_REPO:$IMAGE_TAG"
else
  aws ecr describe-images --repository-name "$REPO_NAME" \
    --image-ids "imageTag=$IMAGE_TAG" --region "$AWS_REGION" >/dev/null
fi
# Unique immutable tags create a new task definition. Terraform asks for approval.
terraform apply -var="image_tag=$IMAGE_TAG"
CLUSTER="$(terraform output -raw ecs_cluster_name)"
SERVICE="$(terraform output -raw ecs_service_name)"
EXPECTED_TASK="$(terraform output -raw ecs_task_definition_arn)"
echo "Waiting for ECS service stability..."
aws ecs wait services-stable --cluster "$CLUSTER" --services "$SERVICE" --region "$AWS_REGION"
# A circuit-breaker rollback can also yield a stable service; reject that outcome.
ACTUAL_TASK="$(aws ecs describe-services --cluster "$CLUSTER" --services "$SERVICE" \
  --region "$AWS_REGION" --query 'services[0].taskDefinition' --output text)"
if [[ "$ACTUAL_TASK" != "$EXPECTED_TASK" ]]; then
  echo "Deployment rolled back or the service differs from the expected task definition." >&2
  echo "Inspect ECS service events and CloudWatch logs before retrying." >&2
  exit 1
fi
URL="$(terraform output -raw alb_dns_name)"
curl --fail --silent --show-error --retry 6 --retry-delay 5 --retry-all-errors \
  --connect-timeout 5 --max-time 15 "$URL/health" \
  | python3 -c 'import json,sys; assert json.load(sys.stdin).get("status") == "healthy"'
curl --fail --silent --show-error --connect-timeout 5 --max-time 20 "$URL/" >/dev/null
curl --fail --silent --show-error --connect-timeout 5 --max-time 20 "$URL/api/status" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["data_state"] == "ok", d; assert len(d["services"]) == 10; print("Public feed verified. Last success:", d["last_success_at"])'
echo "Deployment verified: $URL"
echo "Deployed image tag: $IMAGE_TAG (save this for re-apply or rollback)"
