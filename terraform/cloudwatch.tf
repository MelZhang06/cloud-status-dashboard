resource "aws_cloudwatch_log_group" "app" {
  name              = "/ecs/${var.project_name}"
  retention_in_days = 14

  tags = {
    Name = "${var.project_name}-logs"
  }
}
