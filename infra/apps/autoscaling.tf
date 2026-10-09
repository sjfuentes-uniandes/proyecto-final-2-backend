# ECS Service Auto Scaling (ARQ-003): APIs por CPU y workers por profundidad de
# cola. Una sola política por servicio evita oscilaciones entre políticas; un
# servicio síncrono que además consume una cola ligera (cotizacion) escala por CPU.
resource "aws_appautoscaling_target" "service" {
  for_each           = local.deployed
  min_capacity       = local.sizing[each.key].min
  max_capacity       = max(local.sizing[each.key].min, local.sizing[each.key].max)
  resource_id        = "service/${local.p.cluster.name}/${local.ecs_services[each.key].service_name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}

resource "aws_appautoscaling_policy" "cpu" {
  for_each           = { for name, service in local.deployed : name => service if service.tier != "worker" && local.sizing[name].max > local.sizing[name].min }
  name               = "${local.prefix}-${each.key}-cpu"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.service[each.key].resource_id
  scalable_dimension = aws_appautoscaling_target.service[each.key].scalable_dimension
  service_namespace  = aws_appautoscaling_target.service[each.key].service_namespace
  target_tracking_scaling_policy_configuration {
    target_value       = var.cpu_target
    scale_out_cooldown = 30
    scale_in_cooldown  = 300
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
  }
}

resource "aws_appautoscaling_policy" "backlog" {
  for_each           = { for name, service in local.deployed : name => service if service.tier == "worker" && length(service.consumes) > 0 && local.sizing[name].max > local.sizing[name].min }
  name               = "${local.prefix}-${each.key}-backlog"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.service[each.key].resource_id
  scalable_dimension = aws_appautoscaling_target.service[each.key].scalable_dimension
  service_namespace  = aws_appautoscaling_target.service[each.key].service_namespace
  target_tracking_scaling_policy_configuration {
    target_value       = var.queue_backlog_target
    scale_out_cooldown = 60
    scale_in_cooldown  = 300
    customized_metric_specification {
      metrics {
        id          = "backlog"
        label       = "Mensajes visibles en las colas de ${each.key}"
        expression  = join(" + ", [for index, queue in each.value.consumes : "m${index}"])
        return_data = true
      }
      dynamic "metrics" {
        for_each = each.value.consumes
        content {
          id          = "m${metrics.key}"
          return_data = false
          metric_stat {
            stat = "Average"
            metric {
              namespace   = "AWS/SQS"
              metric_name = "ApproximateNumberOfMessagesVisible"
              dimensions {
                name  = "QueueName"
                value = local.p.queues[metrics.value].name
              }
            }
          }
        }
      }
    }
  }
}
