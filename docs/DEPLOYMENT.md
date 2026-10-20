# Deployment verification

Use this checklist to deploy, verify, update and remove the demonstration environment.

1. Select your AWS profile; run `aws sts get-caller-identity`; set `EXPECTED_AWS_ACCOUNT_ID` to the verified account. Review permissions, current regional pricing and budget notifications.
2. Run local tests, then `terraform -chdir=terraform init` and `terraform -chdir=terraform plan`. The first full plan can reference a not-yet-uploaded image; it is a preview, not a deployment. Never commit state or credentials.
3. Run `./deploy.sh`, review the initial ECR-only apply and subsequent full apply. Save the unique image tag printed by the script.
4. Confirm `Deployment verified`, inspect the dashboard and API, and capture a screenshot. In the ECS console verify one running task and an ALB target with `healthy` status. In CloudWatch verify the `/ecs/cloud-status` log stream.
5. To verify an infrastructure update, change `retention_in_days` in `terraform/cloudwatch.tf` from 7 to 14. Use `IMAGE_TAG=<deployed-tag> ./deploy.sh --skip-build`, review the plan and verify the log retention setting. The image stays unchanged because only log retention is being updated.
6. Record the deployed image tag, verification results and infrastructure changes. Keep credentials and Terraform state out of version control.
7. Run `./deploy.sh destroy` from the same checkout/profile. Confirm project resources are removed. ECR images are deleted too. Keep local state until cleanup has been verified.

## If deployment verification fails

- Authentication/permissions: verify the active account and profile; inspect the exact denied action. Do not solve every denial by adding wildcard permissions to the app role.
- Task start/image pull: inspect ECS service events and stopped-task reasons; verify the ECR image tag and `linux/amd64` image, execution role, NAT route and security-group egress.
- Unhealthy target: check container logs, port 8080, `/health` and the ALB-to-task security-group rule.
- Stable but wrong task definition: the circuit breaker may have restored the previous revision. The script treats that as failure, not a successful release.
- Dashboard shows unknown: inspect `/api/status` and CloudWatch logs. Feed failure does not fail ALB liveness. Cached errors can remain visible for up to 60 seconds.
- ECS waiter timeout: inspect service events instead of assuming resources were rolled back or cleaned up. Resolve the cause, rerun the script with a new image tag, or destroy the partial environment.
