# HU-W27 AC3: volumen, errores y p95 por operación (una serie por servicio y
# operación, un tablero por ambiente), junto con saturación, colas y logs por
# correlationId.
locals {
  ns          = var.metrics_namespace
  env         = local.p.environment
  service_ids = keys(local.deployed)
  log_groups  = [for group in aws_cloudwatch_log_group.service : group.name]

  widgets = concat(
    [
      {
        type = "text", x = 0, y = 0, width = 24, height = 2
        properties = {
          markdown = "## ${local.prefix}\nAmbiente **${local.env}** · Servicios desplegados: ${join(", ", local.service_ids)} · Cada serie de las métricas de operación corresponde a un par Service/Operation; un tablero por ambiente."
        }
      },
      {
        type = "metric", x = 0, y = 2, width = 8, height = 6
        properties = {
          title   = "Volumen por operación", region = local.p.region, period = 60, stat = "SampleCount"
          metrics = [[{ expression = "SEARCH('{${local.ns},Environment,Operation,Service} MetricName=\"OperationDuration\" Environment=\"${local.env}\"', 'SampleCount', 60)", id = "volumen" }]]
        }
      },
      {
        type = "metric", x = 8, y = 2, width = 8, height = 6
        properties = {
          title   = "Errores por operación", region = local.p.region, period = 60, stat = "Sum"
          metrics = [[{ expression = "SEARCH('{${local.ns},Environment,Operation,Service} MetricName=\"OperationErrors\" Environment=\"${local.env}\"', 'Sum', 60)", id = "errores" }]]
        }
      },
      {
        type = "metric", x = 16, y = 2, width = 8, height = 6
        properties = {
          title       = "Latencia p95 por operación (ms)", region = local.p.region, period = 60, stat = "p95"
          metrics     = [[{ expression = "SEARCH('{${local.ns},Environment,Operation,Service} MetricName=\"OperationDuration\" Environment=\"${local.env}\"', 'p95', 60)", id = "p95" }]]
          annotations = { horizontal = [{ label = "Umbral perfilamiento", value = var.profiling_p95_ms }] }
        }
      },
    ],
    [for index, api in keys(local.p.apis) : {
      type = "metric", x = index * 12, y = 8, width = 12, height = 6
      properties = {
        title   = "API ${api}: solicitudes, 4XX, 5XX", region = local.p.region, period = 60, stat = "Sum"
        metrics = [for metric in ["Count", "4XXError", "5XXError"] : ["AWS/ApiGateway", metric, "ApiName", local.p.apis[api].name, "Stage", local.p.apis[api].stage]]
      }
    }],
    [
      {
        type = "metric", x = 0, y = 14, width = 12, height = 6
        properties = {
          title   = "ECS CPU %", region = local.p.region, period = 60, stat = "Average"
          metrics = [for name in local.service_ids : ["AWS/ECS", "CPUUtilization", "ClusterName", local.p.cluster.name, "ServiceName", name]]
        }
      },
      {
        type = "metric", x = 12, y = 14, width = 12, height = 6
        properties = {
          title   = "ECS memoria %", region = local.p.region, period = 60, stat = "Average"
          metrics = [for name in local.service_ids : ["AWS/ECS", "MemoryUtilization", "ClusterName", local.p.cluster.name, "ServiceName", name]]
        }
      },
      {
        type = "metric", x = 0, y = 20, width = 8, height = 6
        properties = {
          title   = "Destinos saludables (ALB)", region = local.p.region, period = 60, stat = "Minimum"
          metrics = [for name, group in local.p.target_groups : ["AWS/ApplicationELB", "HealthyHostCount", "LoadBalancer", local.p.alb.arn_suffix, "TargetGroup", group.arn_suffix]]
        }
      },
      {
        type = "metric", x = 8, y = 20, width = 8, height = 6
        properties = {
          title = "Colas: profundidad y DLQ", region = local.p.region, period = 60, stat = "Maximum"
          metrics = concat(
            [for name, queue in local.p.queues : ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", queue.name]],
            [for name, queue in local.p.queues : ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", queue.dlq_name]]
          )
        }
      },
      {
        type = "metric", x = 16, y = 20, width = 8, height = 6
        properties = {
          title   = "Colas: antigüedad (s)", region = local.p.region, period = 60, stat = "Maximum"
          metrics = [for name, queue in local.p.queues : ["AWS/SQS", "ApproximateAgeOfOldestMessage", "QueueName", queue.name]]
        }
      },
      {
        type = "metric", x = 0, y = 26, width = 12, height = 6
        properties = {
          title   = "Socios: limitados (429) por API key", region = local.p.region, period = 300, stat = "Sum"
          metrics = [[{ expression = "SEARCH('{Solventa/Socios,ApiKeyId} MetricName=\"PartnerThrottled\"', 'Sum', 300)", id = "limitados" }]]
        }
      },
      {
        type = "metric", x = 12, y = 26, width = 12, height = 6
        properties = {
          title   = "RDS: CPU y conexiones", region = local.p.region, period = 60, stat = "Average"
          metrics = [for metric in ["CPUUtilization", "DatabaseConnections"] : ["AWS/RDS", metric, "DBInstanceIdentifier", local.p.database.identifier]]
        }
      },
    ],
    length(local.log_groups) == 0 ? [] : [{
      type = "log", x = 0, y = 32, width = 24, height = 8
      properties = {
        title  = "Recorridos por correlationId (errores recientes)"
        region = local.p.region
        view   = "table"
        query  = "SOURCE ${join(" | SOURCE ", [for group in local.log_groups : "'${group}'"])} | fields @timestamp, service, operation, result, duration_ms, correlation_id | filter result = 'error' or status >= 500 | sort @timestamp desc | limit 100"
      }
    }]
  )
}

resource "aws_cloudwatch_dashboard" "main" {
  dashboard_name = local.prefix
  dashboard_body = jsonencode({ widgets = local.widgets })
}
