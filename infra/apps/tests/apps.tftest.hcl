# Plan completo sin credenciales ni estado de platform: `terraform test` en infra/apps.
mock_provider "aws" {
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  mock_data "aws_ecr_image" {
    defaults = { image_digest = "sha256:1111111111111111111111111111111111111111111111111111111111111111" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/r" }
  }
  mock_resource "aws_cloudwatch_log_group" {
    defaults = { arn = "arn:aws:logs:us-east-1:123456789012:log-group:g" }
  }
  mock_resource "aws_ecs_task_definition" {
    defaults = { arn = "arn:aws:ecs:us-east-1:123456789012:task-definition/t:1" }
  }
}

override_data {
  target = data.terraform_remote_state.platform
  values = {
    outputs = {
      platform = {
        name                 = "solventa"
        environment          = "int"
        prefix               = "solventa-int"
        region               = "us-east-1"
        account_id           = "123456789012"
        vpc_id               = "vpc-1"
        private_subnet_ids   = ["subnet-a", "subnet-b"]
        private_subnet_cidrs = ["10.60.10.0/24", "10.60.11.0/24"]
        kms_key_arn          = null
        cluster              = { id = "arn:aws:ecs:us-east-1:123456789012:cluster/solventa-int", arn = "arn:aws:ecs:us-east-1:123456789012:cluster/solventa-int", name = "solventa-int" }
        namespace_arn        = "arn:aws:servicediscovery:us-east-1:123456789012:namespace/ns-1"
        catalog = {
          "bff-web"             = { tier = "acceso", listener_port = 8081, database = false, publishes = false, calls = ["clientes", "cotizacion", "api-socios"], consumes = [], allies = [], stories = [], discoverable = false, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-int/bff-web", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-int/bff-web", name = "solventa-int/bff-web" } }
          "bff-movil"           = { tier = "acceso", listener_port = 8082, database = false, publishes = false, calls = ["clientes"], consumes = [], allies = [], stories = [], discoverable = false, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-int/bff-movil", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-int/bff-movil", name = "solventa-int/bff-movil" } }
          "api-socios"          = { tier = "acceso", listener_port = 8083, database = true, publishes = true, calls = ["cotizacion"], consumes = [], allies = [], stories = [], discoverable = true, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-int/api-socios", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-int/api-socios", name = "solventa-int/api-socios" } }
          "clientes"            = { tier = "nucleo", listener_port = null, database = true, publishes = true, calls = ["adaptador-identidad"], consumes = [], allies = [], stories = [], discoverable = true, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-int/clientes", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-int/clientes", name = "solventa-int/clientes" } }
          "catalogo"            = { tier = "nucleo", listener_port = null, database = true, publishes = false, calls = [], consumes = [], allies = [], stories = [], discoverable = true, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-int/catalogo", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-int/catalogo", name = "solventa-int/catalogo" } }
          "cotizacion"          = { tier = "nucleo", listener_port = null, database = true, publishes = true, calls = ["catalogo", "clientes", "adaptador-datos"], consumes = ["consentimientos-cotizacion"], allies = [], stories = [], discoverable = true, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-int/cotizacion", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-int/cotizacion", name = "solventa-int/cotizacion" } }
          "adaptador-datos"     = { tier = "adaptador", listener_port = null, database = false, publishes = false, calls = ["simulador-aliados"], consumes = [], allies = ["open-finance", "datos-abiertos"], stories = [], discoverable = true, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-int/adaptador-datos", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-int/adaptador-datos", name = "solventa-int/adaptador-datos" } }
          "adaptador-identidad" = { tier = "adaptador", listener_port = null, database = false, publishes = false, calls = ["simulador-aliados"], consumes = [], allies = ["kyc"], stories = [], discoverable = true, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-int/adaptador-identidad", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-int/adaptador-identidad", name = "solventa-int/adaptador-identidad" } }
          "auditoria"           = { tier = "worker", listener_port = null, database = false, publishes = false, calls = [], consumes = ["auditoria"], allies = [], stories = [], discoverable = false, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-int/auditoria", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-int/auditoria", name = "solventa-int/auditoria" } }
          "simulador-aliados"   = { tier = "soporte", listener_port = null, database = false, publishes = false, calls = [], consumes = [], allies = [], stories = [], discoverable = true, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-int/simulador-aliados", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-int/simulador-aliados", name = "solventa-int/simulador-aliados" } }
        }
        database = {
          host              = "solventa-int.abc.us-east-1.rds.amazonaws.com"
          port              = 5432
          identifier        = "solventa-int"
          instance_class    = "db.t4g.micro"
          multi_az          = false
          security_group_id = "sg-db"
          names             = { "api-socios" = "api_socios", clientes = "clientes", catalogo = "catalogo", cotizacion = "cotizacion" }
          secrets = {
            "api-socios" = "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-int/db/api-socios"
            clientes     = "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-int/db/clientes"
            catalogo     = "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-int/db/catalogo"
            cotizacion   = "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-int/db/cotizacion"
          }
        }
        target_groups = {
          "bff-web"    = { arn = "arn:aws:elasticloadbalancing:us-east-1:123456789012:targetgroup/w/1", arn_suffix = "targetgroup/w/1" }
          "bff-movil"  = { arn = "arn:aws:elasticloadbalancing:us-east-1:123456789012:targetgroup/m/1", arn_suffix = "targetgroup/m/1" }
          "api-socios" = { arn = "arn:aws:elasticloadbalancing:us-east-1:123456789012:targetgroup/s/1", arn_suffix = "targetgroup/s/1" }
        }
        alb = { arn_suffix = "app/solventa-int-access/1", security_group_id = "sg-alb" }
        queues = {
          auditoria                  = { arn = "arn:aws:sqs:us-east-1:123456789012:solventa-int-auditoria", url = "https://sqs.us-east-1.amazonaws.com/123456789012/solventa-int-auditoria", name = "solventa-int-auditoria", dlq_arn = "arn:aws:sqs:us-east-1:123456789012:solventa-int-auditoria-dlq", dlq_name = "solventa-int-auditoria-dlq" }
          consentimientos-cotizacion = { arn = "arn:aws:sqs:us-east-1:123456789012:solventa-int-consentimientos-cotizacion", url = "https://sqs.us-east-1.amazonaws.com/123456789012/solventa-int-consentimientos-cotizacion", name = "solventa-int-consentimientos-cotizacion", dlq_arn = "arn:aws:sqs:us-east-1:123456789012:solventa-int-consentimientos-cotizacion-dlq", dlq_name = "solventa-int-consentimientos-cotizacion-dlq" }
        }
        events_topic_arn = "arn:aws:sns:us-east-1:123456789012:solventa-int-eventos-negocio"
        alerts_topic_arn = "arn:aws:sns:us-east-1:123456789012:solventa-int-alertas"
        audit_bucket     = { name = "solventa-int-auditoria-123456789012", arn = "arn:aws:s3:::solventa-int-auditoria-123456789012" }
        allies = {
          open-finance   = { secret_arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-int/aliados/open-finance" }
          datos-abiertos = { secret_arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-int/aliados/datos-abiertos" }
          kyc            = { secret_arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-int/aliados/kyc" }
        }
        cognito = {
          partners_pool_id    = "us-east-1_p"
          partners_pool_arn   = "arn:aws:cognito-idp:us-east-1:123456789012:userpool/us-east-1_p"
          partners_token_url  = "https://solventa-int-socios-x.auth.us-east-1.amazoncognito.com/oauth2/token"
          partners_resource   = "solventa"
          customers_pool_id   = "us-east-1_c"
          customers_pool_arn  = "arn:aws:cognito-idp:us-east-1:123456789012:userpool/us-east-1_c"
          backoffice_pool_id  = "us-east-1_b"
          backoffice_pool_arn = "arn:aws:cognito-idp:us-east-1:123456789012:userpool/us-east-1_b"
          issuers             = { customers = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_c", backoffice = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_b", partners = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_p" }
        }
        apis = {
          canales = { id = "a1", name = "solventa-int-canales", arn = "arn:aws:apigateway:us-east-1::/restapis/a1", stage = "v1", url = "https://a1.execute-api.us-east-1.amazonaws.com/v1", log_group = "/aws/apigateway/solventa-int-canales" }
          socios  = { id = "a2", name = "solventa-int-socios", arn = "arn:aws:apigateway:us-east-1::/restapis/a2", stage = "v1", url = "https://a2.execute-api.us-east-1.amazonaws.com/v1", log_group = "/aws/apigateway/solventa-int-socios" }
        }
        usage_plans       = { basico = "up1", estandar = "up2" }
        metrics_log_group = "/ecs/solventa-int/metrics"
        events_log_group  = "/ecs/solventa-int/events"
      }
    }
  }
}

variables {
  state_bucket = "solventa-tfstate-123456789012"
  environment  = "int"
  image_digests = {
    "bff-web"             = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "bff-movil"           = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "api-socios"          = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "clientes"            = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "catalogo"            = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "cotizacion"          = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "adaptador-datos"     = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "adaptador-identidad" = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "auditoria"           = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "simulador-aliados"   = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
  }
}

run "todos_los_servicios" {
  command = plan

  assert {
    condition = local.depth == {
      "simulador-aliados" = 0, auditoria = 0, catalogo = 0
      "adaptador-datos"   = 1, "adaptador-identidad" = 1
      clientes            = 2, "bff-movil" = 3, cotizacion = 3
      "api-socios"        = 4, "bff-web" = 5
    }
    error_message = "Las capas de Service Connect deben crear primero a los servicios invocados."
  }
  assert {
    # EC2 rechaza descripciones de reglas con caracteres fuera de este conjunto (p. ej. ">" o tildes).
    condition = alltrue([
      for regla in aws_vpc_security_group_ingress_rule.internal : can(regex("^[a-zA-Z0-9. _:/()#,@\\[\\]+=&;{}!$*-]{0,255}$", regla.description))
    ])
    error_message = "Las descripciones de las reglas de seguridad solo admiten los caracteres que acepta EC2."
  }
  assert {
    condition     = contains(keys(local.service_links), "cotizacion-adaptador-datos") && !contains(keys(local.service_links), "bff-web-catalogo")
    error_message = "Solo se permiten los enlaces declarados en el catálogo."
  }
  assert {
    condition     = local.environment["adaptador-identidad"]["ALLY_KYC_URL"] == "http://simulador-aliados:8080/kyc"
    error_message = "Sin endpoint real, el adaptador debe apuntar al simulador."
  }
  assert {
    condition     = local.environment["cotizacion"]["SQS_CONSENTIMIENTOS_COTIZACION_URL"] != "" && local.environment["cotizacion"]["CATALOGO_URL"] == "http://catalogo:8080"
    error_message = "Cotización debe recibir su cola y las URLs de sus dependencias."
  }
  assert {
    condition     = length(aws_appautoscaling_policy.backlog) == 1 && contains(keys(aws_appautoscaling_policy.backlog), "auditoria")
    error_message = "Solo los workers escalan por profundidad de cola."
  }
  assert {
    condition     = length(aws_cloudwatch_metric_alarm.profiling_latency) == 1 && aws_cloudwatch_metric_alarm.profiling_latency[0].threshold == 400
    error_message = "Debe existir la alarma de p95 del perfilamiento con umbral configurable."
  }
  assert {
    condition     = length(aws_vpc_security_group_ingress_rule.database) == 4
    error_message = "Solo los servicios con base llegan a PostgreSQL."
  }
  assert {
    condition     = toset(keys(aws_vpc_security_group_ingress_rule.alb)) == toset(["bff-web", "bff-movil", "api-socios"])
    error_message = "Solo los servicios de acceso reciben tráfico del ALB."
  }
  assert {
    condition     = !strcontains(jsonencode(local.task_statements), "kms:")
    error_message = "Sin clave propia no se otorgan permisos KMS."
  }
  # HU-W27
  assert {
    condition     = strcontains(local.otel_config, "correlation_id") && strcontains(local.otel_config, "^Operation.*")
    error_message = "El colector debe indexar correlation_id en X-Ray y publicar las métricas Operation*."
  }
  assert {
    condition = alltrue([
      for name, statements in local.task_statements :
      strcontains(jsonencode(statements), "xray:GetTraceSummaries") == (name == "bff-web")
    ])
    error_message = "Solo bff-web lee trazas de X-Ray (pantalla Operación › Trazas)."
  }
  assert {
    condition     = jsondecode(local.environment["bff-web"]["CORS_ORIGINS"]) == ["http://localhost:4200"] && !contains(keys(local.environment["clientes"]), "CORS_ORIGINS")
    error_message = "Solo el BFF web recibe los orígenes del portal para CORS."
  }
  assert {
    condition = alltrue([
      for widget in local.widgets : widget.type != "metric" || !strcontains(jsonencode(widget), "MetricName=\\\"Operation") || strcontains(jsonencode(widget), "Environment=\\\"int\\\"")
    ])
    error_message = "Las búsquedas de métricas de operación deben incluir el filtro reemplazable por la variable Servicio."
  }
  assert {
    condition = (
      length([for widget in local.widgets : widget if try(widget.properties.title, "") == "Tasa de error (%)"]) == 1 &&
      contains([for value in local.dashboard_variables[0].values : value.label], "cotizacion") &&
      length(local.dashboard_variables[0].values) == 11
    )
    error_message = "El tablero debe mostrar la tasa de error y filtrar por cada servicio desplegado (AC3)."
  }
  assert {
    condition     = length([for widget in local.widgets : widget if strcontains(try(widget.properties.query, ""), "pct(duration_ms, 95)")]) == 1
    error_message = "El tablero debe resumir volumen, tasa de error y p95 por operación."
  }
  assert {
    condition = alltrue([
      for name, statements in local.task_statements :
      strcontains(jsonencode(statements), "cognito-idp:AdminCreateUser") == (name == "bff-web")
    ]) && local.environment["bff-web"]["BACKOFFICE_USER_POOL_ID"] == "us-east-1_b"
    error_message = "Solo bff-web administra usuarios del back-office, y únicamente en su pool."
  }
  assert {
    condition     = length(one([for widget in local.widgets : widget.properties.metrics if try(widget.properties.title, "") == "Tareas en ejecución por servicio"])) == length(local.deployed)
    error_message = "El tablero debe mostrar las tareas en ejecución de cada servicio desplegado."
  }
}

run "despliegue_parcial" {
  command = plan

  variables {
    image_digests = {
      "clientes"            = "sha256:2222222222222222222222222222222222222222222222222222222222222222"
      "adaptador-identidad" = "sha256:2222222222222222222222222222222222222222222222222222222222222222"
    }
  }

  assert {
    condition     = toset(keys(local.deployed)) == toset(["clientes", "adaptador-identidad"])
    error_message = "Solo se despliegan servicios con imagen publicada."
  }
  assert {
    condition     = local.depth == { "adaptador-identidad" = 0, clientes = 1 }
    error_message = "Las capas se recalculan con los servicios desplegados."
  }
  assert {
    condition     = length(aws_cloudwatch_metric_alarm.profiling_latency) == 0
    error_message = "Sin cotización no hay alarma de perfilamiento."
  }
}

run "pausado" {
  command = plan

  variables {
    paused = true
  }

  assert {
    condition     = alltrue([for target in aws_appautoscaling_target.service : target.min_capacity == 0 && target.max_capacity == 0])
    error_message = "Pausado: todos los servicios quedan en 0 tareas."
  }
  assert {
    condition     = length(aws_cloudwatch_metric_alarm.healthy_targets) == 0
    error_message = "Pausado: no se alerta por falta de destinos."
  }
}
