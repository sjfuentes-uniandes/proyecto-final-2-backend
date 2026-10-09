# HU-W27 AC3: volumen, tasa de error y p95 por operación (una serie por servicio y
# operación), filtrable por servicio con la variable del tablero y por ambiente
# (un tablero por ambiente: solventa-<ambiente>), junto con saturación, colas y
# logs por correlationId.
locals {
  ns          = var.metrics_namespace
  env         = local.p.environment
  service_ids = keys(local.deployed)
  log_groups  = [for group in aws_cloudwatch_log_group.service : group.name]

  # Filtro por servicio: la variable "servicio" reemplaza este texto en todas las
  # búsquedas de métricas de operación (Todos = solo el ambiente).
  env_filter      = "Environment=\"${local.env}\""
  search_duration = "SEARCH('{${local.ns},Environment,Operation,Service} MetricName=\"OperationDuration\" ${local.env_filter}'"
  search_errors   = "SEARCH('{${local.ns},Environment,Operation,Service} MetricName=\"OperationErrors\" ${local.env_filter}'"
  log_sources     = join(" | ", [for group in local.log_groups : "SOURCE '${group}'"])
  row             = 6 # desplazamiento de las filas de infraestructura bajo las de operación

  dashboard_variables = [{
    type         = "pattern"
    pattern      = local.env_filter
    inputType    = "select"
    id           = "servicio"
    label        = "Servicio"
    defaultValue = local.env_filter
    visible      = true
    values = concat(
      [{ label = "Todos", value = local.env_filter }],
      [for name in local.service_ids : { label = name, value = "${local.env_filter} Service=\"${name}\"" }]
    )
  }]

  widgets = concat(
    [
      {
        type = "text", x = 0, y = 0, width = 24, height = 2
        properties = {
          markdown = "## ${local.prefix}\nAmbiente **${local.env}** · Servicios desplegados: ${join(", ", local.service_ids)} · Cada serie de las métricas de operación corresponde a un par Service/Operation; use la variable **Servicio** para filtrar (un tablero por ambiente). Trazas por correlationId: [X-Ray](https://${local.p.region}.console.aws.amazon.com/cloudwatch/home?region=${local.p.region}#xray:traces/query) o la pantalla Operación › Trazas del portal."
        }
      },
      {
        type = "metric", x = 0, y = 2, width = 8, height = 6
        properties = {
          title   = "Volumen por operación", region = local.p.region, period = 60, stat = "SampleCount"
          metrics = [[{ expression = "${local.search_duration}, 'SampleCount', 60)", id = "volumen" }]]
        }
      },
      {
        type = "metric", x = 8, y = 2, width = 8, height = 6
        properties = {
          title   = "Errores por operación", region = local.p.region, period = 60, stat = "Sum"
          metrics = [[{ expression = "${local.search_errors}, 'Sum', 60)", id = "errores" }]]
        }
      },
      {
        type = "metric", x = 16, y = 2, width = 8, height = 6
        properties = {
          title       = "Latencia p95 por operación (ms)", region = local.p.region, period = 60, stat = "p95"
          metrics     = [[{ expression = "${local.search_duration}, 'p95', 60)", id = "p95" }]]
          annotations = { horizontal = [{ label = "Umbral perfilamiento", value = var.profiling_p95_ms }] }
        }
      },
    ],
    [
      {
        type = "metric", x = 0, y = 8, width = 8, height = 6
        properties = {
          title = "Tasa de error (%)", region = local.p.region, period = 60, view = "timeSeries"
          yAxis = { left = { min = 0, max = 100, label = "%", showUnits = false } }
          metrics = [
            [{ expression = "100 * SUM(err) / SUM(vol)", id = "tasa", label = "Errores / solicitudes (%)" }],
            [{ expression = "${local.search_errors}, 'Sum', 60)", id = "err", visible = false }],
            [{ expression = "${local.search_duration}, 'SampleCount', 60)", id = "vol", visible = false }],
          ]
        }
      },
    ],
    length(local.log_groups) == 0 ? [] : [{
      type = "log", x = 8, y = 8, width = 16, height = 6
      properties = {
        title  = "Resumen por operación: volumen, tasa de error y p95 (logs)"
        region = local.p.region
        view   = "table"
        query  = "${local.log_sources} | filter ispresent(duration_ms) and ispresent(operation) | fields strcontains(result, 'error') + strcontains(result, 'timeout') as fallo | stats count(*) as volumen, sum(fallo) * 100 / count(*) as tasa_error_pct, pct(duration_ms, 95) as p95_ms by service, operation | sort volumen desc | limit 50"
      }
    }],
    [for index, api in keys(local.p.apis) : {
      type = "metric", x = index * 12, y = 8 + local.row, width = 12, height = 6
      properties = {
        title   = "API ${api}: solicitudes, 4XX, 5XX", region = local.p.region, period = 60, stat = "Sum"
        metrics = [for metric in ["Count", "4XXError", "5XXError"] : ["AWS/ApiGateway", metric, "ApiName", local.p.apis[api].name, "Stage", local.p.apis[api].stage]]
      }
    }],
    [
      {
        type = "metric", x = 0, y = 14 + local.row, width = 12, height = 6
        properties = {
          title   = "ECS CPU %", region = local.p.region, period = 60, stat = "Average"
          metrics = [for name in local.service_ids : ["AWS/ECS", "CPUUtilization", "ClusterName", local.p.cluster.name, "ServiceName", name]]
        }
      },
      {
        type = "metric", x = 12, y = 14 + local.row, width = 12, height = 6
        properties = {
          title   = "ECS memoria %", region = local.p.region, period = 60, stat = "Average"
          metrics = [for name in local.service_ids : ["AWS/ECS", "MemoryUtilization", "ClusterName", local.p.cluster.name, "ServiceName", name]]
        }
      },
      {
        type = "metric", x = 0, y = 20 + local.row, width = 8, height = 6
        properties = {
          title   = "Destinos saludables (ALB)", region = local.p.region, period = 60, stat = "Minimum"
          metrics = [for name, group in local.p.target_groups : ["AWS/ApplicationELB", "HealthyHostCount", "LoadBalancer", local.p.alb.arn_suffix, "TargetGroup", group.arn_suffix]]
        }
      },
      {
        type = "metric", x = 8, y = 20 + local.row, width = 8, height = 6
        properties = {
          title = "Colas: profundidad y DLQ", region = local.p.region, period = 60, stat = "Maximum"
          metrics = concat(
            [for name, queue in local.p.queues : ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", queue.name]],
            [for name, queue in local.p.queues : ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", queue.dlq_name]]
          )
        }
      },
      {
        type = "metric", x = 16, y = 20 + local.row, width = 8, height = 6
        properties = {
          title   = "Colas: antigüedad (s)", region = local.p.region, period = 60, stat = "Maximum"
          metrics = [for name, queue in local.p.queues : ["AWS/SQS", "ApproximateAgeOfOldestMessage", "QueueName", queue.name]]
        }
      },
      {
        type = "metric", x = 0, y = 26 + local.row, width = 12, height = 6
        properties = {
          title   = "Socios: limitados (429) por API key", region = local.p.region, period = 300, stat = "Sum"
          metrics = [[{ expression = "SEARCH('{Solventa/Socios,ApiKeyId} MetricName=\"PartnerThrottled\"', 'Sum', 300)", id = "limitados" }]]
        }
      },
      {
        type = "metric", x = 12, y = 26 + local.row, width = 12, height = 6
        properties = {
          title   = "RDS: CPU y conexiones", region = local.p.region, period = 60, stat = "Average"
          metrics = [for metric in ["CPUUtilization", "DatabaseConnections"] : ["AWS/RDS", metric, "DBInstanceIdentifier", local.p.database.identifier]]
        }
      },
    ],
    [{
      # Tareas saludables de todos los servicios (también workers sin ALB). Sin Container
      # Insights, el SampleCount de CPUUtilization por minuto equivale a las tareas que
      # reportan; ECS reemplaza las que fallan el healthcheck del contenedor.
      type = "metric", x = 0, y = 32 + local.row, width = 24, height = 6
      properties = {
        title   = "Tareas en ejecución por servicio", region = local.p.region, period = 60, stat = "SampleCount"
        metrics = [for name in local.service_ids : ["AWS/ECS", "CPUUtilization", "ClusterName", local.p.cluster.name, "ServiceName", name, { label = name }]]
      }
    }],
    length(local.log_groups) == 0 ? [] : [{
      type = "log", x = 0, y = 38 + local.row, width = 24, height = 8
      properties = {
        title  = "Recorridos por correlationId (errores recientes)"
        region = local.p.region
        view   = "table"
        query  = "${local.log_sources} | fields @timestamp, service, operation, result, duration_ms, correlation_id | filter result = 'error' or status >= 500 | sort @timestamp desc | limit 100"
      }
    }]
  )
}

resource "aws_cloudwatch_dashboard" "main" {
  dashboard_name = local.prefix
  dashboard_body = jsonencode({ widgets = local.widgets, variables = local.dashboard_variables })
}
