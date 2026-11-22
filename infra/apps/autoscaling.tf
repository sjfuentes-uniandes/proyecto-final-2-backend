# ECS Service Auto Scaling (ARQ-003): APIs por CPU y workers por profundidad de
# cola; un servicio síncrono que además consume una cola ligera (cotizacion) escala
# por CPU.
# HU-W25: un worker escala por los mensajes visibles de sus colas (backlog; por tarea
# si enciende queue_backlog_per_task, en su override o por el global) y, si define
# queue_age_target, también por la antigüedad del mensaje más viejo de cada cola
# (backlog_age), solo hacia fuera. Con varias
# políticas de target tracking, App Auto Scaling sube si cualquiera lo pide y baja solo
# si lo piden todas las que pueden bajar: la reducción la decide solo la política por
# mensajes, y así las dos no oscilan entre sí.
locals {
  # Workers con colas que pueden crecer: los que tienen la política backlog.
  queue_scaled = { for name, service in local.deployed : name => service if service.tier == "worker" && length(service.consumes) > 0 && local.sizing[name].max > local.sizing[name].min }

  # HU-W25: una política por antigüedad por (servicio, cola) de los que definen
  # queue_age_target. avisos no puede definirlo: lo rechaza la validación de
  # service_overrides (variables.tf), la única fuente del campo.
  queue_age_policies = merge([
    for name, service in local.queue_scaled : {
      for queue in service.consumes : "${name}/${queue}" => { service = name, queue = queue }
    } if local.sizing[name].queue_age_target != null
  ]...)
}

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
    target_value       = local.sizing[each.key].cpu_target
    scale_out_cooldown = local.sizing[each.key].scale_out_cooldown
    scale_in_cooldown  = local.sizing[each.key].scale_in_cooldown
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
  }
}

resource "aws_appautoscaling_policy" "backlog" {
  for_each           = local.queue_scaled
  name               = "${local.prefix}-${each.key}-backlog"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.service[each.key].resource_id
  scalable_dimension = aws_appautoscaling_target.service[each.key].scalable_dimension
  service_namespace  = aws_appautoscaling_target.service[each.key].service_namespace
  target_tracking_scaling_policy_configuration {
    target_value       = local.sizing[each.key].queue_backlog_target
    scale_out_cooldown = local.sizing[each.key].queue_scale_out_cooldown
    scale_in_cooldown  = local.sizing[each.key].queue_scale_in_cooldown
    customized_metric_specification {
      # HU-W25: con queue_backlog_per_task (service_overrides o, sin él, el global), los
      # visibles por tarea: (m0 + m1) / tareas.
      # Sin datos de tareas, la alarma queda sin datos y no actúa en ningún sentido.
      metrics {
        id          = "backlog"
        label       = local.sizing[each.key].queue_backlog_per_task ? "Mensajes visibles por tarea en las colas de ${each.key}" : "Mensajes visibles en las colas de ${each.key}"
        expression  = format(local.sizing[each.key].queue_backlog_per_task ? "(%s) / tareas" : "%s", join(" + ", [for index, queue in each.value.consumes : "m${index}"]))
        return_data = true
      }
      dynamic "metrics" {
        for_each = local.sizing[each.key].queue_backlog_per_task ? ["tareas"] : []
        content {
          id          = metrics.value
          return_data = false
          metric_stat {
            stat = "Average"
            metric {
              namespace   = "ECS/ContainerInsights"
              metric_name = "RunningTaskCount"
              dimensions {
                name  = "ClusterName"
                value = local.p.cluster.name
              }
              dimensions {
                name  = "ServiceName"
                value = local.ecs_services[each.key].service_name
              }
            }
          }
        }
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
  lifecycle {
    # HU-W25 (§b-9): RunningTaskCount solo existe con Container Insights en el clúster. Un
    # check solo advertiría en el plan; la precondición lo detiene. Solo la comprueban los
    # servicios que encienden el interruptor (25.3b).
    precondition {
      condition     = !local.sizing[each.key].queue_backlog_per_task || try(local.p.container_insights, false)
      error_message = "HU-W25: queue_backlog_per_task = true en ${each.key} divide los mensajes por ECS/ContainerInsights RunningTaskCount, pero la salida platform.container_insights de este ambiente no es true (o no existe). Aplica antes infra/platform con container_insights = true (Infra - Desplegar, alcance completo o plataforma) o deja el interruptor apagado en ese servicio (service_overrides) y en el global."
    }
  }
}

# HU-W25: solo de salida (disable_scale_in), una por cola y sin metric math: subir si
# cualquiera lo pide equivale al máximo de las antigüedades de las colas.
# El proveedor (v6.64.0) no reintenta ConcurrentUpdateException en PutScalingPolicy:
# depends_on crea (y actualiza) backlog antes que estas políticas sobre el mismo objetivo.
# Las de las dos colas de un servicio siguen yendo en paralelo entre sí.
resource "aws_appautoscaling_policy" "backlog_age" {
  for_each           = local.queue_age_policies
  name               = "${local.prefix}-${each.value.service}-antiguedad-${each.value.queue}"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.service[each.value.service].resource_id
  scalable_dimension = aws_appautoscaling_target.service[each.value.service].scalable_dimension
  service_namespace  = aws_appautoscaling_target.service[each.value.service].service_namespace
  target_tracking_scaling_policy_configuration {
    target_value       = local.sizing[each.value.service].queue_age_target
    scale_out_cooldown = local.sizing[each.value.service].queue_scale_out_cooldown
    disable_scale_in   = true
    customized_metric_specification {
      namespace   = "AWS/SQS"
      metric_name = "ApproximateAgeOfOldestMessage"
      statistic   = "Maximum"
      dimensions {
        name  = "QueueName"
        value = local.p.queues[each.value.queue].name
      }
    }
  }
  depends_on = [aws_appautoscaling_policy.backlog]
}
