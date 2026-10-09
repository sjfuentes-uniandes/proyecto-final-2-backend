# HU-W28: alarmas con umbrales configurables. Cada alarma envía ALARM y OK al
# tópico de alertas (recuperación, AC3). CloudWatch notifica solo en cambios de
# estado, así que una condición sostenida produce una única alerta (AC2). La
# descripción incluye ambiente, operación, umbral y enlace al tablero.
locals {
  alarm_actions = [local.p.alerts_topic_arn]
  dashboard_url = "https://${local.p.region}.console.aws.amazon.com/cloudwatch/home?region=${local.p.region}#dashboards/dashboard/${local.prefix}"

  alarm_common = {
    period              = var.alarm_period_seconds
    evaluation_periods  = var.alarm_evaluation_periods
    datapoints_to_alarm = var.alarm_datapoints_to_alarm
  }
}

# --- Recorridos --------------------------------------------------------------------
# Métrica de aplicación publicada por el colector ADOT (contrato en infra/PLATAFORMA.md).
resource "aws_cloudwatch_metric_alarm" "profiling_latency" {
  count               = contains(keys(local.deployed), "cotizacion") ? 1 : 0
  alarm_name          = "${local.prefix}-perfilamiento-p95"
  alarm_description   = "[${local.p.environment}] Operación perfilamiento (cotizacion): p95 > ${var.profiling_p95_ms} ms. Tablero: ${local.dashboard_url}"
  namespace           = var.metrics_namespace
  metric_name         = "OperationDuration"
  extended_statistic  = "p95"
  unit                = "Milliseconds"
  comparison_operator = "GreaterThanThreshold"
  threshold           = var.profiling_p95_ms
  period              = local.alarm_common.period
  evaluation_periods  = local.alarm_common.evaluation_periods
  datapoints_to_alarm = local.alarm_common.datapoints_to_alarm
  treat_missing_data  = "notBreaching"
  dimensions = {
    Environment = local.p.environment
    Service     = "cotizacion"
    Operation   = "perfilamiento"
  }
  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "api_errors" {
  for_each            = local.p.apis
  alarm_name          = "${local.prefix}-api-${each.key}-5xx"
  alarm_description   = "[${local.p.environment}] API ${each.key}: tasa de errores 5XX > ${var.api_5xx_rate * 100} %. Tablero: ${local.dashboard_url}"
  namespace           = "AWS/ApiGateway"
  metric_name         = "5XXError"
  statistic           = "Average"
  comparison_operator = "GreaterThanThreshold"
  threshold           = var.api_5xx_rate
  period              = local.alarm_common.period
  evaluation_periods  = local.alarm_common.evaluation_periods
  datapoints_to_alarm = local.alarm_common.datapoints_to_alarm
  treat_missing_data  = "notBreaching"
  dimensions          = { ApiName = each.value.name, Stage = each.value.stage }
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "api_latency" {
  for_each            = local.p.apis
  alarm_name          = "${local.prefix}-api-${each.key}-p95"
  alarm_description   = "[${local.p.environment}] API ${each.key}: latencia p95 > ${var.api_p95_ms} ms. Tablero: ${local.dashboard_url}"
  namespace           = "AWS/ApiGateway"
  metric_name         = "Latency"
  extended_statistic  = "p95"
  comparison_operator = "GreaterThanThreshold"
  threshold           = var.api_p95_ms
  period              = local.alarm_common.period
  evaluation_periods  = local.alarm_common.evaluation_periods
  datapoints_to_alarm = local.alarm_common.datapoints_to_alarm
  treat_missing_data  = "notBreaching"
  dimensions          = { ApiName = each.value.name, Stage = each.value.stage }
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}

# --- Disponibilidad del recorrido: destinos saludables detrás del ALB --------
resource "aws_cloudwatch_metric_alarm" "healthy_targets" {
  for_each            = { for name, service in local.deployed : name => service if service.listener_port != null && !var.paused }
  alarm_name          = "${local.prefix}-${each.key}-sin-destinos"
  alarm_description   = "[${local.p.environment}] ${each.key}: ningún destino saludable en el ALB. Tablero: ${local.dashboard_url}"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HealthyHostCount"
  statistic           = "Minimum"
  comparison_operator = "LessThanThreshold"
  threshold           = 1
  period              = 60
  evaluation_periods  = 2
  treat_missing_data  = "breaching"
  dimensions          = { LoadBalancer = local.p.alb.arn_suffix, TargetGroup = local.p.target_groups[each.key].arn_suffix }
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}

# --- Saturación básica -------------------------------------------------------------
locals {
  saturation_alarms = {
    for pair in setproduct(keys(local.deployed), ["CPUUtilization", "MemoryUtilization"]) :
    "${pair[0]}-${pair[1] == "CPUUtilization" ? "cpu" : "memoria"}" => {
      service   = pair[0]
      metric    = pair[1]
      threshold = pair[1] == "CPUUtilization" ? var.cpu_alarm_percent : var.memory_alarm_percent
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "saturation" {
  for_each            = local.saturation_alarms
  alarm_name          = "${local.prefix}-${each.key}"
  alarm_description   = "[${local.p.environment}] ${each.value.service}: ${each.value.metric} > ${each.value.threshold} %. Tablero: ${local.dashboard_url}"
  namespace           = "AWS/ECS"
  metric_name         = each.value.metric
  statistic           = "Average"
  comparison_operator = "GreaterThanThreshold"
  threshold           = each.value.threshold
  period              = local.alarm_common.period
  evaluation_periods  = local.alarm_common.evaluation_periods
  datapoints_to_alarm = local.alarm_common.datapoints_to_alarm
  treat_missing_data  = "notBreaching"
  dimensions          = { ClusterName = local.p.cluster.name, ServiceName = local.ecs_services[each.value.service].service_name }
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}

# --- Colas: atraso y mensajes fallidos -------------------------------------------
resource "aws_cloudwatch_metric_alarm" "queue_age" {
  for_each            = local.p.queues
  alarm_name          = "${local.prefix}-cola-${each.key}-atraso"
  alarm_description   = "[${local.p.environment}] Cola ${each.key}: mensaje más antiguo > ${var.queue_age_alarm_seconds} s. Tablero: ${local.dashboard_url}"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateAgeOfOldestMessage"
  statistic           = "Maximum"
  comparison_operator = "GreaterThanThreshold"
  threshold           = var.queue_age_alarm_seconds
  period              = local.alarm_common.period
  evaluation_periods  = local.alarm_common.evaluation_periods
  datapoints_to_alarm = local.alarm_common.datapoints_to_alarm
  treat_missing_data  = "notBreaching"
  dimensions          = { QueueName = each.value.name }
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "dlq" {
  for_each            = local.p.queues
  alarm_name          = "${local.prefix}-cola-${each.key}-dlq"
  alarm_description   = "[${local.p.environment}] Cola ${each.key}: hay mensajes en la DLQ que requieren intervención. Tablero: ${local.dashboard_url}"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Maximum"
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  period              = 300
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"
  dimensions          = { QueueName = each.value.dlq_name }
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}

# --- Base de datos --------------------------------------------------------------------
resource "aws_cloudwatch_metric_alarm" "database_cpu" {
  alarm_name          = "${local.prefix}-rds-cpu"
  alarm_description   = "[${local.p.environment}] RDS ${local.p.database.identifier}: CPU > ${var.cpu_alarm_percent} %. Tablero: ${local.dashboard_url}"
  namespace           = "AWS/RDS"
  metric_name         = "CPUUtilization"
  statistic           = "Average"
  comparison_operator = "GreaterThanThreshold"
  threshold           = var.cpu_alarm_percent
  period              = local.alarm_common.period
  evaluation_periods  = local.alarm_common.evaluation_periods
  datapoints_to_alarm = local.alarm_common.datapoints_to_alarm
  treat_missing_data  = "notBreaching"
  dimensions          = { DBInstanceIdentifier = local.p.database.identifier }
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "database_storage" {
  alarm_name          = "${local.prefix}-rds-almacenamiento"
  alarm_description   = "[${local.p.environment}] RDS ${local.p.database.identifier}: menos de 2 GiB libres. Tablero: ${local.dashboard_url}"
  namespace           = "AWS/RDS"
  metric_name         = "FreeStorageSpace"
  statistic           = "Minimum"
  comparison_operator = "LessThanThreshold"
  threshold           = 2147483648
  period              = 300
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"
  dimensions          = { DBInstanceIdentifier = local.p.database.identifier }
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}
