output "alb_dns_name" {
  description = "Public URL of the load balancer serving the dashboard"
  value       = "http://${aws_lb.main.dns_name}"
}

output "ecr_repository_url" {
  description = "ECR repository to build and push the container image to"
  value       = aws_ecr_repository.app.repository_url
}

output "ecs_cluster_name" {
  description = "ECS cluster name used for deployment verification"
  value       = aws_ecs_cluster.main.name
}

output "ecs_service_name" {
  description = "ECS service name used for deployment verification"
  value       = aws_ecs_service.app.name
}

output "aws_region" {
  description = "Deployment region used by the AWS CLI in deploy.sh"
  value       = var.aws_region
}

output "ecs_task_definition_arn" {
  description = "Expected task definition for deployment verification"
  value       = aws_ecs_task_definition.app.arn
}
