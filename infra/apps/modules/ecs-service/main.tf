terraform {
  required_providers {
    aws = { source = "hashicorp/aws" }
  }
}

variable "name" { type = string }
variable "family" { type = string }
variable "cluster_id" { type = string }
variable "namespace_arn" { type = string }
variable "subnet_ids" { type = list(string) }
variable "security_group_id" { type = string }
variable "execution_role_arn" { type = string }
variable "task_role_arn" { type = string }
variable "container_definitions" { type = string }
variable "cpu" { type = number }
variable "memory" { type = number }
variable "desired_count" { type = number }
variable "use_fargate_spot" { type = bool }
variable "discoverable" { type = bool }
variable "target_group_arn" {
  type    = string
  default = null
}
variable "proxy_log_group" { type = string }
variable "region" { type = string }

resource "aws_ecs_task_definition" "this" {
  family                   = var.family
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = tostring(var.cpu)
  memory                   = tostring(var.memory)
  execution_role_arn       = var.execution_role_arn
  task_role_arn            = var.task_role_arn
  container_definitions    = var.container_definitions
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }
}

resource "aws_ecs_service" "this" {
  name                               = var.name
  cluster                            = var.cluster_id
  task_definition                    = aws_ecs_task_definition.this.arn
  desired_count                      = var.desired_count
  propagate_tags                     = "SERVICE"
  platform_version                   = "LATEST"
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  health_check_grace_period_seconds  = var.target_group_arn == null ? null : 60
  availability_zone_rebalancing      = "ENABLED"
  enable_ecs_managed_tags            = true
  tags                               = { Service = var.name }

  # FARGATE_SPOT cuesta ~70 % menos; AWS puede interrumpir una tarea con 2 min de
  # aviso y ECS la reemplaza. Para producción, usar FARGATE.
  capacity_provider_strategy {
    capacity_provider = var.use_fargate_spot ? "FARGATE_SPOT" : "FARGATE"
    weight            = 1
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = var.subnet_ids
    security_groups  = [var.security_group_id]
    assign_public_ip = false
  }

  dynamic "load_balancer" {
    for_each = var.target_group_arn == null ? [] : [var.target_group_arn]
    content {
      target_group_arn = load_balancer.value
      container_name   = var.name
      container_port   = 8080
    }
  }

  service_connect_configuration {
    enabled   = true
    namespace = var.namespace_arn
    dynamic "service" {
      for_each = var.discoverable ? [var.name] : []
      content {
        port_name      = "http"
        discovery_name = var.name
        client_alias {
          dns_name = var.name
          port     = 8080
        }
      }
    }
    log_configuration {
      log_driver = "awslogs"
      options = {
        awslogs-group         = var.proxy_log_group
        awslogs-region        = var.region
        awslogs-stream-prefix = var.name
      }
    }
  }

  lifecycle {
    ignore_changes = [desired_count]
  }
}

output "service_name" {
  value = aws_ecs_service.this.name
}

output "task_definition_arn" {
  value = aws_ecs_task_definition.this.arn
}
