# HU-W28: umbrales en apps.tfvars; advertencia (warning_ratio) y crítica por meta.
# Sin acciones: notifications.tf notifica. Guía: docs/hu-w28-alertas.md
locals {
  dashboard_url = "https://${local.p.region}.console.aws.amazon.com/cloudwatch/home?region=${local.p.region}#dashboards/dashboard/${local.prefix}"

  alarm_common = {
    period              = var.alarm_period_seconds
    evaluation_periods  = var.alarm_evaluation_periods
    datapoints_to_alarm = var.alarm_datapoints_to_alarm
  }

  severity_label = { critica = "CRÍTICA", advertencia = "ADVERTENCIA", informativa = "INFORMATIVA" }
  warning_pct    = format("%.0f", var.warning_ratio * 100)

  error_rate_warning   = tonumber(format("%.2f", coalesce(var.error_rate_warning_percent, var.error_rate_percent * var.warning_ratio)))
  availability_warning = tonumber(format("%.2f", coalesce(var.availability_warning_percent, 100 - (100 - var.availability_percent) * var.warning_ratio)))
  partner_quota_warn   = tonumber(format("%.2f", var.partner_quota_percent * var.warning_ratio))

  # --- Latencia p95 por operación prioritaria (AC1) ---------------------------------
  latency_slos = concat(
    [{ id = "perfilamiento", label = "Perfilamiento p95", service = "cotizacion", operation = "perfilamiento", target_ms = var.profiling_p95_ms, warning_ms = null }],
    var.latency_targets,
  )
  latency_alarms = merge([
    for slo in local.latency_slos : {
      "${slo.id}-p95-advertencia" = {
        slo   = slo, severity = "advertencia", threshold = tonumber(format("%.0f", coalesce(slo.warning_ms, slo.target_ms * var.warning_ratio)))
        title = "${slo.label} por encima del ${local.warning_pct} % de la meta"
      }
      "${slo.id}-p95-critica" = {
        slo   = slo, severity = "critica", threshold = slo.target_ms
        title = "${slo.label} por encima de la meta"
      }
    }
  ]...)

  # --- Tasa de error técnico por servicio desplegado (AC2) ---------------------------
  error_rate_alarms = merge({}, [
    for name in keys(local.deployed) : {
      "${name}-errores-advertencia" = {
        service = name, severity = "advertencia", threshold = local.error_rate_warning
        title   = "Tasa de error técnico de ${name} sobre ${replace(local.error_rate_warning, ".", ",")} %"
      }
      "${name}-errores-critica" = {
        service = name, severity = "critica", threshold = var.error_rate_percent
        title   = "Tasa de error técnico de ${name} sobre la meta"
      }
    }
  ]...)

  # --- Disponibilidad por API: 1 - 5XX / solicitudes ------------------------------
  availability_alarms = merge({}, [
    for name, api in local.p.apis : {
      "api-${name}-disponibilidad-advertencia" = {
        api   = api, severity = "advertencia", threshold = local.availability_warning
        title = "Disponibilidad del API ${name} bajo ${replace(local.availability_warning, ".", ",")} %"
      }
      "api-${name}-disponibilidad-critica" = {
        api   = api, severity = "critica", threshold = var.availability_percent
        title = "Disponibilidad del API ${name} bajo la meta"
      }
    }
  ]...)

  # --- Cuota de socio: el mayor consumo entre los socios del ambiente ---------------
  partner_quota_alarms = {
    "socio-cuota-advertencia" = { severity = "advertencia", threshold = local.partner_quota_warn, title = "Socio sobre el ${local.warning_pct} % de su cuota" }
    "socio-cuota-critica"     = { severity = "critica", threshold = var.partner_quota_percent, title = "Socio con la cuota agotada" }
  }

  saturation_alarms = {
    for pair in setproduct(keys(local.deployed), ["CPUUtilization", "MemoryUtilization"]) :
    "${pair[0]}-${pair[1] == "CPUUtilization" ? "cpu" : "memoria"}" => {
      service   = pair[0]
      metric    = pair[1]
      threshold = pair[1] == "CPUUtilization" ? var.cpu_alarm_percent : var.memory_alarm_percent
      title     = "${pair[1] == "CPUUtilization" ? "CPU" : "Memoria"} de ${pair[0]} sobre ${pair[1] == "CPUUtilization" ? var.cpu_alarm_percent : var.memory_alarm_percent} %"
    }
  }

  healthy_target_services = { for name, service in local.deployed : name => service if service.listener_port != null && !var.paused }

  # --- Sprint 2 (HU-S02) ---------------------------------------------------------------
  # Degradación por proveedor (HU-W06): llamadas fallidas / llamadas, sumando las
  # operaciones del proveedor. Existe aunque el adaptador no esté desplegado.
  provider_alarms = {
    for provider, config in var.provider_operations : "proveedor-${provider}-degradado" => {
      provider = provider, service = config.service, operations = config.operations, severity = "critica"
      title    = "Proveedor ${provider} degradado: más del ${var.provider_error_percent} % de llamadas fallidas"
    }
  }
  # Saga de contratación: aceptaciones sin recaudar o recaudos sin emitir.
  pending_collection_queues = [for name in ["dinero-aceptaciones", "polizas-recaudos"] : name if contains(keys(local.p.queues), name)]

  dashboard_link = { tipo = "tablero", url = local.dashboard_url }

  # Severidad, título, unidad y enlace de cada alarma, sin el prefijo del
  # ambiente. bff-web lo recibe en ALERTAS_CATALOGO (pantalla Operación › Alertas).
  alert_catalog = merge(
    { for key, a in local.latency_alarms : key => { severidad = a.severity, titulo = a.title, unidad = "ms", enlace = { tipo = "traza", min_ms = a.threshold } } },
    { for key, a in local.error_rate_alarms : key => { severidad = a.severity, titulo = a.title, unidad = "%", enlace = { tipo = "traza" } } },
    { for key, a in local.availability_alarms : key => { severidad = a.severity, titulo = a.title, unidad = "%", enlace = local.dashboard_link } },
    { for key, a in local.partner_quota_alarms : key => { severidad = a.severity, titulo = a.title, unidad = "%", enlace = { tipo = "socio" } } },
    { for name, api in local.p.apis : "api-${name}-5xx" => { severidad = "critica", titulo = "Errores 5XX del API ${name} sobre ${var.api_5xx_rate * 100} %", unidad = "proporcion", enlace = local.dashboard_link } },
    { for name, api in local.p.apis : "api-${name}-p95" => { severidad = "advertencia", titulo = "Latencia p95 del API ${name} sobre ${var.api_p95_ms} ms", unidad = "ms", enlace = local.dashboard_link } },
    { for name in keys(local.healthy_target_services) : "${name}-sin-destinos" => { severidad = "critica", titulo = "${name} sin destinos saludables en el ALB", unidad = "destinos", enlace = local.dashboard_link } },
    { for key, a in local.saturation_alarms : key => { severidad = "advertencia", titulo = a.title, unidad = "%", enlace = local.dashboard_link } },
    { for name, queue in local.p.queues : "cola-${name}-atraso" => { severidad = "advertencia", titulo = "Atraso en la cola ${name}", unidad = "s", enlace = { tipo = "cola", url = local.queue_console[name].main } } },
    { for name, queue in local.p.queues : "cola-${name}-dlq" => { severidad = "informativa", titulo = "Mensajes en la cola de descarte de ${name}", unidad = "mensajes", enlace = { tipo = "cola", url = local.queue_console[name].dlq } } },
    { for key, a in local.provider_alarms : key => { severidad = a.severity, titulo = a.title, unidad = "%", enlace = { tipo = "traza" } } },
    length(local.pending_collection_queues) == 0 ? {} : {
      "recaudos-pendientes" = { severidad = "critica", titulo = "Recaudos o emisiones pendientes por más de ${var.pending_collection_age_seconds} s", unidad = "s", enlace = { tipo = "cola", url = local.queue_console[local.pending_collection_queues[0]].main } }
    },
    { "avisos-push-fallos" = { severidad = "advertencia", titulo = "Avisos push fallidos en avisos", unidad = "envios", enlace = { tipo = "traza" } } },
    {
      "rds-cpu"            = { severidad = "advertencia", titulo = "CPU de la base de datos sobre ${var.cpu_alarm_percent} %", unidad = "%", enlace = local.dashboard_link }
      "rds-almacenamiento" = { severidad = "critica", titulo = "Base de datos con menos de 2 GiB libres", unidad = "bytes", enlace = local.dashboard_link }
    },
  )

  queue_console = {
    for name, queue in local.p.queues : name => {
      main = "https://${local.p.region}.console.aws.amazon.com/sqs/v3/home?region=${local.p.region}#/queues/${urlencode(queue.url)}"
      dlq  = "https://${local.p.region}.console.aws.amazon.com/sqs/v3/home?region=${local.p.region}#/queues/${urlencode(replace(queue.url, queue.name, queue.dlq_name))}"
    }
  }

  # Umbrales configurados (tabla de la pantalla), en el orden de las metas.
  alert_thresholds = concat(
    [for slo in local.latency_slos : { metrica = slo.label, unidad = "ms", meta = slo.target_ms, advertencia = tonumber(format("%.0f", coalesce(slo.warning_ms, slo.target_ms * var.warning_ratio))), critica = slo.target_ms }],
    [
      { metrica = "Tasa de error técnico", unidad = "%", meta = var.error_rate_percent, advertencia = local.error_rate_warning, critica = var.error_rate_percent },
      { metrica = "Cuota de socio", unidad = "%", meta = var.partner_quota_percent, advertencia = local.partner_quota_warn, critica = var.partner_quota_percent },
      { metrica = "Disponibilidad", unidad = "%", meta = var.availability_percent, advertencia = local.availability_warning, critica = var.availability_percent },
    ],
  )
}

# --- Recorridos ----------------------------------------------------------------------
# Métrica de aplicación publicada por el colector ADOT (contrato en infra/PLATAFORMA.md).
# Existe aunque el servicio no esté desplegado: sin datos queda en OK (AC4 reproducible).
resource "aws_cloudwatch_metric_alarm" "latency" {
  for_each            = local.latency_alarms
  alarm_name          = "${local.prefix}-${each.key}"
  alarm_description   = "[${local.p.environment}] ${local.severity_label[each.value.severity]} - Operación ${each.value.slo.operation} (${each.value.slo.service}): p95 > ${each.value.threshold} ms (meta ${each.value.slo.target_ms} ms). Tablero: ${local.dashboard_url}"
  namespace           = var.metrics_namespace
  metric_name         = "OperationDuration"
  extended_statistic  = "p95"
  unit                = "Milliseconds"
  comparison_operator = "GreaterThanThreshold"
  threshold           = each.value.threshold
  period              = local.alarm_common.period
  evaluation_periods  = local.alarm_common.evaluation_periods
  datapoints_to_alarm = local.alarm_common.datapoints_to_alarm
  treat_missing_data  = "notBreaching"
  dimensions = {
    Environment = local.p.environment
    Service     = each.value.slo.service
    Operation   = each.value.slo.operation
  }
}

resource "aws_cloudwatch_metric_alarm" "error_rate" {
  for_each            = local.error_rate_alarms
  alarm_name          = "${local.prefix}-${each.key}"
  alarm_description   = "[${local.p.environment}] ${local.severity_label[each.value.severity]} - Servicio ${each.value.service}: tasa de error técnico > ${each.value.threshold} % (meta ${var.error_rate_percent} %). Tablero: ${local.dashboard_url}"
  comparison_operator = "GreaterThanThreshold"
  threshold           = each.value.threshold
  evaluation_periods  = local.alarm_common.evaluation_periods
  datapoints_to_alarm = local.alarm_common.datapoints_to_alarm
  treat_missing_data  = "notBreaching"

  metric_query {
    id = "errores"
    metric {
      namespace   = var.metrics_namespace
      metric_name = "OperationErrors"
      period      = local.alarm_common.period
      stat        = "Sum"
      dimensions  = { Environment = local.p.environment, Service = each.value.service }
    }
  }
  metric_query {
    id = "total"
    metric {
      namespace   = var.metrics_namespace
      metric_name = "OperationDuration"
      period      = local.alarm_common.period
      stat        = "SampleCount"
      dimensions  = { Environment = local.p.environment, Service = each.value.service }
    }
  }
  metric_query {
    id          = "tasa"
    expression  = "IF(total > 0, 100 * FILL(errores, 0) / total, 0)"
    label       = "Tasa de error (%)"
    return_data = true
  }
}

resource "aws_cloudwatch_metric_alarm" "availability" {
  for_each            = local.availability_alarms
  alarm_name          = "${local.prefix}-${each.key}"
  alarm_description   = "[${local.p.environment}] ${local.severity_label[each.value.severity]} - API ${each.value.api.name}: disponibilidad < ${each.value.threshold} % (meta ${var.availability_percent} %). Tablero: ${local.dashboard_url}"
  comparison_operator = "LessThanThreshold"
  threshold           = each.value.threshold
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  treat_missing_data  = "notBreaching"

  metric_query {
    id = "errores"
    metric {
      namespace   = "AWS/ApiGateway"
      metric_name = "5XXError"
      period      = var.availability_period_seconds
      stat        = "Sum"
      dimensions  = { ApiName = each.value.api.name, Stage = each.value.api.stage }
    }
  }
  metric_query {
    id = "solicitudes"
    metric {
      namespace   = "AWS/ApiGateway"
      metric_name = "Count"
      period      = var.availability_period_seconds
      stat        = "Sum"
      dimensions  = { ApiName = each.value.api.name, Stage = each.value.api.stage }
    }
  }
  metric_query {
    id          = "disponibilidad"
    expression  = "IF(solicitudes > 0, 100 * (1 - FILL(errores, 0) / solicitudes), 100)"
    label       = "Disponibilidad (%)"
    return_data = true
  }
}

# PartnerQuotaUsage lo publica api-socios por socio (dimensión Partner). Metrics
# Insights evalúa el mayor consumo; bff-web identifica al socio al mostrar la alerta.
resource "aws_cloudwatch_metric_alarm" "partner_quota" {
  for_each            = local.partner_quota_alarms
  alarm_name          = "${local.prefix}-${each.key}"
  alarm_description   = "[${local.p.environment}] ${local.severity_label[each.value.severity]} - Cuota de socio: consumo >= ${each.value.threshold} % (meta ${var.partner_quota_percent} %). Tablero: ${local.dashboard_url}"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = each.value.threshold
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  treat_missing_data  = "notBreaching"

  metric_query {
    id          = "cuota"
    expression  = "SELECT MAX(PartnerQuotaUsage) FROM SCHEMA(\"Solventa/Socios\", Environment, Partner) WHERE Environment = '${local.p.environment}'"
    period      = max(var.partner_quota_interval_seconds, 60)
    label       = "Cuota de socio (%)"
    return_data = true
  }
}

resource "aws_cloudwatch_metric_alarm" "api_errors" {
  for_each            = local.p.apis
  alarm_name          = "${local.prefix}-api-${each.key}-5xx"
  alarm_description   = "[${local.p.environment}] CRÍTICA - API ${each.key}: tasa de errores 5XX > ${var.api_5xx_rate * 100} %. Tablero: ${local.dashboard_url}"
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
}

resource "aws_cloudwatch_metric_alarm" "api_latency" {
  for_each            = local.p.apis
  alarm_name          = "${local.prefix}-api-${each.key}-p95"
  alarm_description   = "[${local.p.environment}] ADVERTENCIA - API ${each.key}: latencia p95 > ${var.api_p95_ms} ms. Tablero: ${local.dashboard_url}"
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
}

# --- Disponibilidad del recorrido: destinos saludables detrás del ALB --------
resource "aws_cloudwatch_metric_alarm" "healthy_targets" {
  for_each            = local.healthy_target_services
  alarm_name          = "${local.prefix}-${each.key}-sin-destinos"
  alarm_description   = "[${local.p.environment}] CRÍTICA - ${each.key}: ningún destino saludable en el ALB. Tablero: ${local.dashboard_url}"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HealthyHostCount"
  statistic           = "Minimum"
  comparison_operator = "LessThanThreshold"
  threshold           = 1
  period              = 60
  evaluation_periods  = 2
  treat_missing_data  = "breaching"
  dimensions          = { LoadBalancer = local.p.alb.arn_suffix, TargetGroup = local.p.target_groups[each.key].arn_suffix }
}

# --- Saturación básica -------------------------------------------------------------
resource "aws_cloudwatch_metric_alarm" "saturation" {
  for_each            = local.saturation_alarms
  alarm_name          = "${local.prefix}-${each.key}"
  alarm_description   = "[${local.p.environment}] ADVERTENCIA - ${each.value.service}: ${each.value.metric} > ${each.value.threshold} %. Tablero: ${local.dashboard_url}"
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
}

# --- Colas: atraso y mensajes fallidos -------------------------------------------
resource "aws_cloudwatch_metric_alarm" "queue_age" {
  for_each            = local.p.queues
  alarm_name          = "${local.prefix}-cola-${each.key}-atraso"
  alarm_description   = "[${local.p.environment}] ADVERTENCIA - Cola ${each.key}: mensaje más antiguo > ${var.queue_age_alarm_seconds} s. Tablero: ${local.dashboard_url}"
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
}

resource "aws_cloudwatch_metric_alarm" "dlq" {
  for_each            = local.p.queues
  alarm_name          = "${local.prefix}-cola-${each.key}-dlq"
  alarm_description   = "[${local.p.environment}] INFORMATIVA - Cola ${each.key}: hay mensajes en la DLQ que requieren intervención. Tablero: ${local.dashboard_url}"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Maximum"
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  period              = 300
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"
  dimensions          = { QueueName = each.value.dlq_name }
}

# --- Sprint 2: proveedores, Saga de contratación y avisos ---------------------------
# Proporción de errores por proveedor: SUM(OperationErrors) / SUM(SampleCount de
# OperationDuration) de las operaciones de var.provider_operations, con las
# dimensiones Environment, Service y Operation (contracts/operaciones.md).
resource "aws_cloudwatch_metric_alarm" "provider_degradation" {
  for_each            = local.provider_alarms
  alarm_name          = "${local.prefix}-${each.key}"
  alarm_description   = "[${local.p.environment}] ${local.severity_label[each.value.severity]} - Proveedor ${each.value.provider} (${each.value.service}): llamadas fallidas > ${var.provider_error_percent} % en ${join(", ", each.value.operations)}. Tablero: ${local.dashboard_url}"
  comparison_operator = "GreaterThanThreshold"
  threshold           = var.provider_error_percent
  evaluation_periods  = local.alarm_common.evaluation_periods
  datapoints_to_alarm = local.alarm_common.datapoints_to_alarm
  treat_missing_data  = "notBreaching"

  dynamic "metric_query" {
    for_each = each.value.operations
    content {
      id = "e${metric_query.key}"
      metric {
        namespace   = var.metrics_namespace
        metric_name = "OperationErrors"
        period      = local.alarm_common.period
        stat        = "Sum"
        dimensions  = { Environment = local.p.environment, Service = each.value.service, Operation = metric_query.value }
      }
    }
  }
  dynamic "metric_query" {
    for_each = each.value.operations
    content {
      id = "t${metric_query.key}"
      metric {
        namespace   = var.metrics_namespace
        metric_name = "OperationDuration"
        period      = local.alarm_common.period
        stat        = "SampleCount"
        dimensions  = { Environment = local.p.environment, Service = each.value.service, Operation = metric_query.value }
      }
    }
  }
  metric_query {
    id         = "errores"
    expression = join(" + ", [for index, _ in each.value.operations : "FILL(e${index}, 0)"])
  }
  metric_query {
    id         = "total"
    expression = join(" + ", [for index, _ in each.value.operations : "FILL(t${index}, 0)"])
  }
  metric_query {
    id          = "tasa"
    expression  = "IF(total > 0, 100 * errores / total, 0)"
    label       = "Llamadas fallidas a ${each.value.provider} (%)"
    return_data = true
  }
}

resource "aws_cloudwatch_metric_alarm" "pending_collections" {
  count               = length(local.pending_collection_queues) > 0 ? 1 : 0
  alarm_name          = "${local.prefix}-recaudos-pendientes"
  alarm_description   = "[${local.p.environment}] CRÍTICA - Saga de contratación: mensaje más antiguo de ${join(" o ", local.pending_collection_queues)} > ${var.pending_collection_age_seconds} s. Tablero: ${local.dashboard_url}"
  comparison_operator = "GreaterThanThreshold"
  threshold           = var.pending_collection_age_seconds
  evaluation_periods  = local.alarm_common.evaluation_periods
  datapoints_to_alarm = local.alarm_common.datapoints_to_alarm
  treat_missing_data  = "notBreaching"

  dynamic "metric_query" {
    for_each = local.pending_collection_queues
    content {
      id = "a${metric_query.key}"
      metric {
        namespace   = "AWS/SQS"
        metric_name = "ApproximateAgeOfOldestMessage"
        period      = local.alarm_common.period
        stat        = "Maximum"
        dimensions  = { QueueName = local.p.queues[metric_query.value].name }
      }
    }
  }
  metric_query {
    id          = "antiguedad"
    expression  = "MAX([${join(", ", [for index, _ in local.pending_collection_queues : "a${index}"])}])"
    label       = "Antigüedad del recaudo pendiente (s)"
    return_data = true
  }
}

resource "aws_cloudwatch_metric_alarm" "push_failures" {
  alarm_name          = "${local.prefix}-avisos-push-fallos"
  alarm_description   = "[${local.p.environment}] ADVERTENCIA - avisos: ${var.push_failure_count} o más envíos push fallidos (aviso.push) en 5 minutos. Tablero: ${local.dashboard_url}"
  namespace           = var.metrics_namespace
  metric_name         = "OperationErrors"
  statistic           = "Sum"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = var.push_failure_count
  period              = 300
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"
  dimensions          = { Environment = local.p.environment, Service = "avisos", Operation = "aviso.push" }
}

# --- Base de datos --------------------------------------------------------------------
resource "aws_cloudwatch_metric_alarm" "database_cpu" {
  alarm_name          = "${local.prefix}-rds-cpu"
  alarm_description   = "[${local.p.environment}] ADVERTENCIA - RDS ${local.p.database.identifier}: CPU > ${var.cpu_alarm_percent} %. Tablero: ${local.dashboard_url}"
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
}

resource "aws_cloudwatch_metric_alarm" "database_storage" {
  alarm_name          = "${local.prefix}-rds-almacenamiento"
  alarm_description   = "[${local.p.environment}] CRÍTICA - RDS ${local.p.database.identifier}: menos de 2 GiB libres. Tablero: ${local.dashboard_url}"
  namespace           = "AWS/RDS"
  metric_name         = "FreeStorageSpace"
  statistic           = "Minimum"
  comparison_operator = "LessThanThreshold"
  threshold           = 2147483648
  period              = 300
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"
  dimensions          = { DBInstanceIdentifier = local.p.database.identifier }
}

locals {
  all_alarms = concat(
    values(aws_cloudwatch_metric_alarm.latency),
    values(aws_cloudwatch_metric_alarm.error_rate),
    values(aws_cloudwatch_metric_alarm.availability),
    values(aws_cloudwatch_metric_alarm.partner_quota),
    values(aws_cloudwatch_metric_alarm.api_errors),
    values(aws_cloudwatch_metric_alarm.api_latency),
    values(aws_cloudwatch_metric_alarm.healthy_targets),
    values(aws_cloudwatch_metric_alarm.saturation),
    values(aws_cloudwatch_metric_alarm.queue_age),
    values(aws_cloudwatch_metric_alarm.dlq),
    values(aws_cloudwatch_metric_alarm.provider_degradation),
    aws_cloudwatch_metric_alarm.pending_collections,
    [aws_cloudwatch_metric_alarm.push_failures],
    [aws_cloudwatch_metric_alarm.database_cpu, aws_cloudwatch_metric_alarm.database_storage],
  )
}
