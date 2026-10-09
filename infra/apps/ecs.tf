resource "aws_cloudwatch_log_group" "service" {
  for_each          = local.deployed
  name              = "/ecs/${local.prefix}/${each.key}"
  retention_in_days = var.log_retention_days
  kms_key_id        = local.p.kms_key_arn
}

resource "aws_cloudwatch_log_group" "proxy" {
  name              = "/ecs/${local.prefix}/service-connect"
  retention_in_days = var.log_retention_days
  kms_key_id        = local.p.kms_key_arn
}

locals {
  env_name = { for name in keys(local.p.catalog) : name => upper(replace(name, "-", "_")) }

  # Colector ADOT: recibe OTLP de la aplicación, envía trazas a X-Ray y métricas
  # EMF a CloudWatch con dimensiones Environment/Service/Operation (HU-W27).
  otel_config = yamlencode({
    extensions = { health_check = {} }
    receivers = {
      otlp = { protocols = { grpc = { endpoint = "0.0.0.0:4317" }, http = { endpoint = "0.0.0.0:4318" } } }
    }
    processors = {
      "batch/traces"  = { timeout = "1s", send_batch_size = 50 }
      "batch/metrics" = { timeout = "60s" }
    }
    exporters = {
      # correlation_id y operation como anotaciones: permiten buscar el recorrido en
      # X-Ray (annotation.correlation_id = "...") desde la consola y desde bff-web.
      awsxray = { region = local.p.region, indexed_attributes = ["correlation_id", "operation"] }
      awsemf = {
        region                           = local.p.region
        namespace                        = var.metrics_namespace
        log_group_name                   = local.p.metrics_log_group
        log_stream_name                  = "{TaskId}"
        dimension_rollup_option          = "NoDimensionRollup"
        resource_to_telemetry_conversion = { enabled = true }
        metric_declarations = [{
          dimensions            = [["Environment", "Service", "Operation"], ["Environment", "Service"]]
          metric_name_selectors = ["^Operation.*"]
        }]
      }
    }
    service = {
      extensions = ["health_check"]
      pipelines = {
        traces  = { receivers = ["otlp"], processors = ["batch/traces"], exporters = ["awsxray"] }
        metrics = { receivers = ["otlp"], processors = ["batch/metrics"], exporters = ["awsemf"] }
      }
    }
  })

  environment = {
    for name, service in local.deployed : name => merge(
      {
        PORT               = "8080"
        SERVICE_NAME       = name
        ENVIRONMENT        = local.p.environment
        AWS_REGION         = local.p.region
        LOG_FORMAT         = "json"
        CORRELATION_HEADER = "X-Correlation-Id"
        REQUEST_ID_HEADER  = "X-Request-Id"
        METRICS_NAMESPACE  = var.metrics_namespace
        OTEL_SERVICE_NAME  = name
        # Service/Environment se convierten en dimensiones de las métricas EMF.
        OTEL_RESOURCE_ATTRIBUTES = "Service=${name},Environment=${local.p.environment},deployment.environment=${local.p.environment}"
        OTEL_PROPAGATORS         = "tracecontext,baggage,xray"
        # Histograma exponencial: el colector publica valores que permiten p95 en CloudWatch.
        OTEL_EXPORTER_OTLP_METRICS_DEFAULT_HISTOGRAM_AGGREGATION = "base2_exponential_bucket_histogram"
      },
      var.enable_tracing ? { OTEL_EXPORTER_OTLP_ENDPOINT = "http://localhost:4317", OTEL_SDK_DISABLED = "false" } : { OTEL_SDK_DISABLED = "true" },
      { for callee in service.calls : "${local.env_name[callee]}_URL" => "http://${callee}:8080" },
      service.database ? {
        DB_HOST      = local.p.database.host
        DB_PORT      = tostring(local.p.database.port)
        DB_NAME      = local.p.database.names[name]
        DB_SSLMODE   = "require"
        DB_POOL_SIZE = tostring(var.db_pool_size)
      } : {},
      service.publishes ? { EVENTS_TOPIC_ARN = local.p.events_topic_arn, OUTBOX_ENABLED = "true" } : {},
      { for queue in service.consumes : "SQS_${upper(replace(queue, "-", "_"))}_URL" => local.p.queues[queue].url },
      merge([for ally in service.allies : {
        "ALLY_${upper(replace(ally, "-", "_"))}_URL"        = lookup(var.ally_endpoints, ally, "http://simulador-aliados:8080/${ally}")
        "ALLY_${upper(replace(ally, "-", "_"))}_TIMEOUT_MS" = tostring(lookup(var.ally_timeouts_ms, ally, 1000))
      }]...),
      contains(local.audit_writers, name) ? { AUDIT_BUCKET = local.p.audit_bucket.name } : {},
      contains(local.partner_admins, name) ? {
        PARTNERS_USER_POOL_ID    = local.p.cognito.partners_pool_id
        PARTNERS_TOKEN_URL       = local.p.cognito.partners_token_url
        PARTNERS_RESOURCE_SERVER = local.p.cognito.partners_resource
        PARTNERS_API_ID          = local.p.apis.socios.id
        PARTNERS_USAGE_PLANS     = jsonencode(local.p.usage_plans)
      } : {},
      service.tier == "acceso" ? { JWT_ISSUERS = jsonencode(local.p.cognito.issuers) } : {},
      # Orígenes del portal para CORS: API Gateway reenvía el preflight al BFF.
      contains(local.cors_services, name) ? { CORS_ORIGINS = jsonencode(local.web_origins) } : {},
      contains(local.backoffice_user_admins, name) ? { BACKOFFICE_USER_POOL_ID = local.p.cognito.backoffice_pool_id } : {},
    )
  }

  secrets = {
    for name, service in local.deployed : name => concat(
      service.database ? [
        { name = "DB_USER", valueFrom = "${local.p.database.secrets[name]}:username::" },
        { name = "DB_PASSWORD", valueFrom = "${local.p.database.secrets[name]}:password::" },
      ] : [],
      [for ally in service.allies : { name = "ALLY_${upper(replace(ally, "-", "_"))}_CREDENTIALS", valueFrom = local.p.allies[ally].secret_arn }],
    )
  }

  containers = {
    for name, service in local.deployed : name => concat(
      [{
        name         = name
        image        = "${service.ecr.url}@${data.aws_ecr_image.service[name].image_digest}"
        essential    = true
        portMappings = [{ name = "http", containerPort = 8080, protocol = "tcp", appProtocol = "http" }]
        environment  = [for key, value in local.environment[name] : { name = key, value = value }]
        secrets      = local.secrets[name]
        healthCheck  = { command = var.health_check_command, interval = 10, timeout = 3, retries = 3, startPeriod = 30 }
        stopTimeout  = 30
        logConfiguration = {
          logDriver = "awslogs"
          options = {
            awslogs-group         = aws_cloudwatch_log_group.service[name].name
            awslogs-region        = local.p.region
            awslogs-stream-prefix = "app"
          }
        }
      }],
      var.enable_tracing ? [{
        name              = "otel-collector"
        image             = var.otel_collector_image
        essential         = false
        memoryReservation = 64
        environment       = [{ name = "AOT_CONFIG_CONTENT", value = local.otel_config }]
        logConfiguration = {
          logDriver = "awslogs"
          options = {
            awslogs-group         = aws_cloudwatch_log_group.service[name].name
            awslogs-region        = local.p.region
            awslogs-stream-prefix = "otel"
          }
        }
      }] : []
    )
  }

  # Service Connect entrega a cada tarea solo los endpoints que existían cuando
  # arrancó. Se calcula la profundidad de dependencias para crear primero a los
  # invocados (capa 0) y después a quienes los llaman.
  deployed_calls = { for name, service in local.deployed : name => [for callee in service.calls : callee if contains(keys(local.deployed), callee)] }
  depth_0        = { for name in keys(local.deployed) : name => 0 }
  depth_1        = { for name, calls in local.deployed_calls : name => max(0, [for callee in calls : local.depth_0[callee] + 1]...) }
  depth_2        = { for name, calls in local.deployed_calls : name => max(0, [for callee in calls : local.depth_1[callee] + 1]...) }
  depth_3        = { for name, calls in local.deployed_calls : name => max(0, [for callee in calls : local.depth_2[callee] + 1]...) }
  depth_4        = { for name, calls in local.deployed_calls : name => max(0, [for callee in calls : local.depth_3[callee] + 1]...) }
  depth_5        = { for name, calls in local.deployed_calls : name => max(0, [for callee in calls : local.depth_4[callee] + 1]...) }
  depth          = { for name, calls in local.deployed_calls : name => max(0, [for callee in calls : local.depth_5[callee] + 1]...) }

  service_inputs = {
    for name, service in local.deployed : name => {
      family                = "${local.prefix}-${name}"
      security_group_id     = aws_security_group.service[name].id
      execution_role_arn    = aws_iam_role.execution[name].arn
      task_role_arn         = aws_iam_role.task[name].arn
      container_definitions = jsonencode(local.containers[name])
      cpu                   = local.sizing[name].cpu
      memory                = local.sizing[name].memory
      desired_count         = local.sizing[name].min
      discoverable          = service.discoverable
      target_group_arn      = service.listener_port == null ? null : local.p.target_groups[name].arn
    }
  }
}

check "dependency_depth" {
  assert {
    condition     = alltrue([for name, level in local.depth : level <= 5 && level == local.depth_5[name]])
    error_message = "Las dependencias del catálogo superan 6 capas o forman un ciclo; revisar calls."
  }
}

module "layer_0" {
  source                = "./modules/ecs-service"
  for_each              = { for name, level in local.depth : name => local.service_inputs[name] if level == 0 }
  name                  = each.key
  family                = each.value.family
  cluster_id            = local.p.cluster.id
  namespace_arn         = local.p.namespace_arn
  subnet_ids            = local.p.private_subnet_ids
  security_group_id     = each.value.security_group_id
  execution_role_arn    = each.value.execution_role_arn
  task_role_arn         = each.value.task_role_arn
  container_definitions = each.value.container_definitions
  cpu                   = each.value.cpu
  memory                = each.value.memory
  desired_count         = each.value.desired_count
  use_fargate_spot      = var.use_fargate_spot
  discoverable          = each.value.discoverable
  target_group_arn      = each.value.target_group_arn
  proxy_log_group       = aws_cloudwatch_log_group.proxy.name
  region                = local.p.region
  depends_on            = [aws_iam_role_policy.execution, aws_iam_role_policy.task]
}

module "layer_1" {
  source                = "./modules/ecs-service"
  for_each              = { for name, level in local.depth : name => local.service_inputs[name] if level == 1 }
  name                  = each.key
  family                = each.value.family
  cluster_id            = local.p.cluster.id
  namespace_arn         = local.p.namespace_arn
  subnet_ids            = local.p.private_subnet_ids
  security_group_id     = each.value.security_group_id
  execution_role_arn    = each.value.execution_role_arn
  task_role_arn         = each.value.task_role_arn
  container_definitions = each.value.container_definitions
  cpu                   = each.value.cpu
  memory                = each.value.memory
  desired_count         = each.value.desired_count
  use_fargate_spot      = var.use_fargate_spot
  discoverable          = each.value.discoverable
  target_group_arn      = each.value.target_group_arn
  proxy_log_group       = aws_cloudwatch_log_group.proxy.name
  region                = local.p.region
  depends_on            = [module.layer_0]
}

module "layer_2" {
  source                = "./modules/ecs-service"
  for_each              = { for name, level in local.depth : name => local.service_inputs[name] if level == 2 }
  name                  = each.key
  family                = each.value.family
  cluster_id            = local.p.cluster.id
  namespace_arn         = local.p.namespace_arn
  subnet_ids            = local.p.private_subnet_ids
  security_group_id     = each.value.security_group_id
  execution_role_arn    = each.value.execution_role_arn
  task_role_arn         = each.value.task_role_arn
  container_definitions = each.value.container_definitions
  cpu                   = each.value.cpu
  memory                = each.value.memory
  desired_count         = each.value.desired_count
  use_fargate_spot      = var.use_fargate_spot
  discoverable          = each.value.discoverable
  target_group_arn      = each.value.target_group_arn
  proxy_log_group       = aws_cloudwatch_log_group.proxy.name
  region                = local.p.region
  depends_on            = [module.layer_1]
}

module "layer_3" {
  source                = "./modules/ecs-service"
  for_each              = { for name, level in local.depth : name => local.service_inputs[name] if level == 3 }
  name                  = each.key
  family                = each.value.family
  cluster_id            = local.p.cluster.id
  namespace_arn         = local.p.namespace_arn
  subnet_ids            = local.p.private_subnet_ids
  security_group_id     = each.value.security_group_id
  execution_role_arn    = each.value.execution_role_arn
  task_role_arn         = each.value.task_role_arn
  container_definitions = each.value.container_definitions
  cpu                   = each.value.cpu
  memory                = each.value.memory
  desired_count         = each.value.desired_count
  use_fargate_spot      = var.use_fargate_spot
  discoverable          = each.value.discoverable
  target_group_arn      = each.value.target_group_arn
  proxy_log_group       = aws_cloudwatch_log_group.proxy.name
  region                = local.p.region
  depends_on            = [module.layer_2]
}

module "layer_4" {
  source                = "./modules/ecs-service"
  for_each              = { for name, level in local.depth : name => local.service_inputs[name] if level == 4 }
  name                  = each.key
  family                = each.value.family
  cluster_id            = local.p.cluster.id
  namespace_arn         = local.p.namespace_arn
  subnet_ids            = local.p.private_subnet_ids
  security_group_id     = each.value.security_group_id
  execution_role_arn    = each.value.execution_role_arn
  task_role_arn         = each.value.task_role_arn
  container_definitions = each.value.container_definitions
  cpu                   = each.value.cpu
  memory                = each.value.memory
  desired_count         = each.value.desired_count
  use_fargate_spot      = var.use_fargate_spot
  discoverable          = each.value.discoverable
  target_group_arn      = each.value.target_group_arn
  proxy_log_group       = aws_cloudwatch_log_group.proxy.name
  region                = local.p.region
  depends_on            = [module.layer_3]
}

module "layer_5" {
  source                = "./modules/ecs-service"
  for_each              = { for name, level in local.depth : name => local.service_inputs[name] if level >= 5 }
  name                  = each.key
  family                = each.value.family
  cluster_id            = local.p.cluster.id
  namespace_arn         = local.p.namespace_arn
  subnet_ids            = local.p.private_subnet_ids
  security_group_id     = each.value.security_group_id
  execution_role_arn    = each.value.execution_role_arn
  task_role_arn         = each.value.task_role_arn
  container_definitions = each.value.container_definitions
  cpu                   = each.value.cpu
  memory                = each.value.memory
  desired_count         = each.value.desired_count
  use_fargate_spot      = var.use_fargate_spot
  discoverable          = each.value.discoverable
  target_group_arn      = each.value.target_group_arn
  proxy_log_group       = aws_cloudwatch_log_group.proxy.name
  region                = local.p.region
  depends_on            = [module.layer_4]
}

locals {
  ecs_services = merge(
    { for name, module_output in module.layer_0 : name => module_output },
    { for name, module_output in module.layer_1 : name => module_output },
    { for name, module_output in module.layer_2 : name => module_output },
    { for name, module_output in module.layer_3 : name => module_output },
    { for name, module_output in module.layer_4 : name => module_output },
    { for name, module_output in module.layer_5 : name => module_output },
  )
}
