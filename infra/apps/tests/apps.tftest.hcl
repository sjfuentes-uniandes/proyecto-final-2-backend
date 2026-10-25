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
          "bff-web"             = { tier = "acceso", listener_port = 8081, database = false, publishes = false, cache = false, calls = ["clientes", "cotizacion", "api-socios", "polizas", "consulta"], consumes = [], allies = [], stories = [], discoverable = false, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-int/bff-web", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-int/bff-web", name = "solventa-int/bff-web" } }
          "bff-movil"           = { tier = "acceso", listener_port = 8082, database = false, publishes = false, cache = false, calls = ["clientes", "consulta", "avisos"], consumes = [], allies = [], stories = [], discoverable = false, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-int/bff-movil", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-int/bff-movil", name = "solventa-int/bff-movil" } }
          "api-socios"          = { tier = "acceso", listener_port = 8083, database = true, publishes = true, cache = false, calls = ["cotizacion"], consumes = [], allies = [], stories = [], discoverable = true, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-int/api-socios", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-int/api-socios", name = "solventa-int/api-socios" } }
          "clientes"            = { tier = "nucleo", listener_port = null, database = true, publishes = true, cache = false, calls = ["adaptador-identidad"], consumes = [], allies = [], stories = [], discoverable = true, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-int/clientes", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-int/clientes", name = "solventa-int/clientes" } }
          "catalogo"            = { tier = "nucleo", listener_port = null, database = true, publishes = false, cache = true, calls = [], consumes = [], allies = [], stories = [], discoverable = true, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-int/catalogo", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-int/catalogo", name = "solventa-int/catalogo" } }
          "cotizacion"          = { tier = "nucleo", listener_port = null, database = true, publishes = true, cache = true, calls = ["catalogo", "clientes", "adaptador-datos"], consumes = ["consentimientos-cotizacion"], allies = [], stories = [], discoverable = true, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-int/cotizacion", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-int/cotizacion", name = "solventa-int/cotizacion" } }
          "adaptador-datos"     = { tier = "adaptador", listener_port = null, database = false, publishes = false, cache = false, calls = ["simulador-aliados"], consumes = [], allies = ["open-finance", "datos-abiertos"], stories = [], discoverable = true, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-int/adaptador-datos", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-int/adaptador-datos", name = "solventa-int/adaptador-datos" } }
          "adaptador-identidad" = { tier = "adaptador", listener_port = null, database = false, publishes = false, cache = false, calls = ["simulador-aliados"], consumes = [], allies = ["kyc"], stories = [], discoverable = true, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-int/adaptador-identidad", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-int/adaptador-identidad", name = "solventa-int/adaptador-identidad" } }
          "polizas"             = { tier = "nucleo", listener_port = null, database = true, publishes = true, cache = false, calls = ["adaptador-firma"], consumes = ["polizas-recaudos"], allies = [], stories = [], discoverable = true, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-int/polizas", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-int/polizas", name = "solventa-int/polizas" } }
          "dinero"              = { tier = "nucleo", listener_port = null, database = true, publishes = true, cache = false, calls = ["adaptador-pagos"], consumes = ["dinero-aceptaciones"], allies = [], stories = [], discoverable = false, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-int/dinero", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-int/dinero", name = "solventa-int/dinero" } }
          "adaptador-pagos"     = { tier = "adaptador", listener_port = null, database = false, publishes = false, cache = false, calls = ["simulador-aliados"], consumes = [], allies = ["pagos"], stories = [], discoverable = true, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-int/adaptador-pagos", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-int/adaptador-pagos", name = "solventa-int/adaptador-pagos" } }
          "adaptador-firma"     = { tier = "adaptador", listener_port = null, database = false, publishes = false, cache = false, calls = ["simulador-aliados"], consumes = [], allies = ["firma", "push"], stories = [], discoverable = true, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-int/adaptador-firma", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-int/adaptador-firma", name = "solventa-int/adaptador-firma" } }
          "consulta"            = { tier = "proyeccion", listener_port = null, database = true, publishes = false, cache = true, calls = [], consumes = ["consulta-proyeccion"], allies = [], stories = [], discoverable = true, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-int/consulta", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-int/consulta", name = "solventa-int/consulta" } }
          "avisos"              = { tier = "worker", listener_port = null, database = true, publishes = false, cache = false, calls = ["adaptador-firma"], consumes = ["avisos"], allies = [], stories = [], discoverable = true, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-int/avisos", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-int/avisos", name = "solventa-int/avisos" } }
          "auditoria"           = { tier = "worker", listener_port = null, database = false, publishes = false, cache = false, calls = [], consumes = ["auditoria"], allies = [], stories = [], discoverable = false, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-int/auditoria", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-int/auditoria", name = "solventa-int/auditoria" } }
          "simulador-aliados"   = { tier = "soporte", listener_port = null, database = false, publishes = false, cache = false, calls = [], consumes = [], allies = [], stories = [], discoverable = true, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-int/simulador-aliados", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-int/simulador-aliados", name = "solventa-int/simulador-aliados" } }
        }
        database = {
          host              = "solventa-int.abc.us-east-1.rds.amazonaws.com"
          port              = 5432
          identifier        = "solventa-int"
          instance_class    = "db.t4g.micro"
          multi_az          = false
          security_group_id = "sg-db"
          names             = { "api-socios" = "api_socios", clientes = "clientes", catalogo = "catalogo", cotizacion = "cotizacion", polizas = "polizas", dinero = "dinero", consulta = "consulta", avisos = "avisos" }
          secrets = {
            "api-socios" = "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-int/db/api-socios"
            clientes     = "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-int/db/clientes"
            catalogo     = "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-int/db/catalogo"
            cotizacion   = "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-int/db/cotizacion"
            polizas      = "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-int/db/polizas"
            dinero       = "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-int/db/dinero"
            consulta     = "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-int/db/consulta"
            avisos       = "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-int/db/avisos"
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
          dinero-aceptaciones        = { arn = "arn:aws:sqs:us-east-1:123456789012:solventa-int-dinero-aceptaciones", url = "https://sqs.us-east-1.amazonaws.com/123456789012/solventa-int-dinero-aceptaciones", name = "solventa-int-dinero-aceptaciones", dlq_arn = "arn:aws:sqs:us-east-1:123456789012:solventa-int-dinero-aceptaciones-dlq", dlq_name = "solventa-int-dinero-aceptaciones-dlq" }
          polizas-recaudos           = { arn = "arn:aws:sqs:us-east-1:123456789012:solventa-int-polizas-recaudos", url = "https://sqs.us-east-1.amazonaws.com/123456789012/solventa-int-polizas-recaudos", name = "solventa-int-polizas-recaudos", dlq_arn = "arn:aws:sqs:us-east-1:123456789012:solventa-int-polizas-recaudos-dlq", dlq_name = "solventa-int-polizas-recaudos-dlq" }
          consulta-proyeccion        = { arn = "arn:aws:sqs:us-east-1:123456789012:solventa-int-consulta-proyeccion", url = "https://sqs.us-east-1.amazonaws.com/123456789012/solventa-int-consulta-proyeccion", name = "solventa-int-consulta-proyeccion", dlq_arn = "arn:aws:sqs:us-east-1:123456789012:solventa-int-consulta-proyeccion-dlq", dlq_name = "solventa-int-consulta-proyeccion-dlq" }
          avisos                     = { arn = "arn:aws:sqs:us-east-1:123456789012:solventa-int-avisos", url = "https://sqs.us-east-1.amazonaws.com/123456789012/solventa-int-avisos", name = "solventa-int-avisos", dlq_arn = "arn:aws:sqs:us-east-1:123456789012:solventa-int-avisos-dlq", dlq_name = "solventa-int-avisos-dlq" }
        }
        events_topic_arn        = "arn:aws:sns:us-east-1:123456789012:solventa-int-eventos-negocio"
        alerts_topic_arn        = "arn:aws:sns:us-east-1:123456789012:solventa-int-alertas"
        audit_bucket            = { name = "solventa-int-auditoria-123456789012", arn = "arn:aws:s3:::solventa-int-auditoria-123456789012" }
        policy_documents_bucket = { name = "solventa-int-polizas-123456789012", arn = "arn:aws:s3:::solventa-int-polizas-123456789012", sse = "AES256" }
        cache                   = { enabled = true, url = "rediss://solventa-int-cache.abc.cache.amazonaws.com:6379", security_group_id = "sg-cache", cluster_ids = ["solventa-int-cache-001"], services = ["catalogo", "consulta", "cotizacion"] }
        allies = {
          open-finance   = { secret_arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-int/aliados/open-finance" }
          datos-abiertos = { secret_arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-int/aliados/datos-abiertos" }
          kyc            = { secret_arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-int/aliados/kyc" }
          pagos          = { secret_arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-int/aliados/pagos" }
          firma          = { secret_arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-int/aliados/firma" }
          push           = { secret_arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-int/aliados/push" }
        }
        registro = { token_secret_arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-int/registro/token" }
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
    "polizas"             = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "dinero"              = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "adaptador-pagos"     = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "adaptador-firma"     = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "consulta"            = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "avisos"              = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
  }
}

run "todos_los_servicios" {
  command = plan

  assert {
    # HU-S02: los servicios nuevos no cambian la capa de bff-web (5) ni la de bff-movil (3),
    # así que sus servicios ECS no se recrean (module.layer_N conserva la dirección).
    condition = local.depth == {
      "simulador-aliados" = 0, auditoria = 0, catalogo = 0, consulta = 0
      "adaptador-datos"   = 1, "adaptador-identidad" = 1, "adaptador-pagos" = 1, "adaptador-firma" = 1
      clientes            = 2, polizas = 2, dinero = 2, avisos = 2
      "bff-movil"         = 3, cotizacion = 3
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
    condition     = contains([for s in local.secrets["clientes"] : s.name], "REGISTRO_TOKEN_SECRET") && !contains([for s in local.secrets["bff-movil"] : s.name], "REGISTRO_TOKEN_SECRET") && one([for s in local.secrets["clientes"] : s.valueFrom if s.name == "REGISTRO_TOKEN_SECRET"]) == "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-int/registro/token:secret::"
    error_message = "Solo clientes recibe el secreto de firma del token de registro (clave secret del JSON)."
  }
  assert {
    condition     = local.environment["cotizacion"]["SQS_CONSENTIMIENTOS_COTIZACION_URL"] != "" && local.environment["cotizacion"]["CATALOGO_URL"] == "http://catalogo:8080"
    error_message = "Cotización debe recibir su cola y las URLs de sus dependencias."
  }
  assert {
    condition     = toset(keys(aws_appautoscaling_policy.backlog)) == toset(["auditoria", "avisos"]) && alltrue([for name in ["polizas", "dinero", "consulta", "adaptador-pagos", "adaptador-firma"] : contains(keys(aws_appautoscaling_policy.cpu), name)])
    error_message = "Solo los workers escalan por profundidad de cola; los demás servicios nuevos, por CPU."
  }
  assert {
    condition     = toset(keys(aws_vpc_security_group_ingress_rule.database)) == toset(["api-socios", "clientes", "catalogo", "cotizacion", "polizas", "dinero", "consulta", "avisos"])
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
      length(local.dashboard_variables[0].values) == 17
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
  # HU-W02
  assert {
    condition = alltrue(concat(
      [for name, statements in local.task_statements : strcontains(jsonencode(statements), "cloudwatch:GetMetricStatistics") == (name == "api-socios")],
      [for plan in ["up1", "up2"] : alltrue([for sufijo in ["", "/keys", "/keys/*", "/usage"] : strcontains(jsonencode(local.task_statements["api-socios"]), "::/usageplans/${plan}${sufijo}\"")])],
    ))
    error_message = "api-socios lee cada plan de uso, mueve API keys entre planes, consulta su uso y lee los 429 por socio; ningún otro servicio."
  }
  assert {
    condition = alltrue([for name, statements in local.task_statements :
      contains(flatten([for s in statements : s.Action]), "apigateway:PUT") == (name == "api-socios") &&
      alltrue([for s in statements : !contains(s.Action, "apigateway:PUT") || (length(s.Action) == 1 && length(s.Resource) == 1 && endswith(s.Resource[0], "%2Fapikeys%2F*"))])
    ])
    error_message = "Solo api-socios etiqueta API keys al crearlas, y únicamente API keys."
  }
  assert {
    condition     = jsondecode(local.environment["api-socios"]["PARTNERS_USAGE_PLANS"]) == { basico = "up1", estandar = "up2" }
    error_message = "api-socios recibe el mapeo plan -> plan de uso."
  }
  assert {
    condition     = length(one([for widget in local.widgets : widget.properties.metrics if try(widget.properties.title, "") == "Tareas en ejecución por servicio"])) == length(local.deployed)
    error_message = "El tablero debe mostrar las tareas en ejecución de cada servicio desplegado."
  }
  # HU-W28
  assert {
    condition = alltrue([
      for alarm in [aws_cloudwatch_metric_alarm.latency["perfilamiento-p95-advertencia"], aws_cloudwatch_metric_alarm.latency["perfilamiento-p95-critica"]] :
      alarm.period == 60 && alarm.evaluation_periods == 5 && alarm.datapoints_to_alarm == 3 && alarm.extended_statistic == "p95" &&
      alarm.treat_missing_data == "notBreaching" && alarm.comparison_operator == "GreaterThanThreshold" &&
      alarm.dimensions == tomap({ Environment = "int", Service = "cotizacion", Operation = "perfilamiento" })
    ]) && aws_cloudwatch_metric_alarm.latency["perfilamiento-p95-critica"].threshold == 400 && aws_cloudwatch_metric_alarm.latency["perfilamiento-p95-advertencia"].threshold == 360
    error_message = "AC1: p95 del perfilamiento > 400 ms (advertencia al 90 %) en 3 de 5 periodos de 60 s; sin datos queda en OK."
  }
  assert {
    condition = alltrue([
      for alarm in local.all_alarms :
      try(length(alarm.alarm_actions), 0) == 0 && try(length(alarm.ok_actions), 0) == 0 && try(length(alarm.insufficient_data_actions), 0) == 0 &&
      startswith(alarm.alarm_name, "solventa-int-") && strcontains(alarm.alarm_description, "[int] ") &&
      strcontains(alarm.alarm_description, "Tablero: https://us-east-1.console.aws.amazon.com/cloudwatch/home?region=us-east-1#dashboards/dashboard/solventa-int")
    ])
    error_message = "AC2: las alarmas no notifican directamente (un solo correo por EventBridge) y su descripción tiene ambiente y tablero."
  }
  assert {
    condition = (
      jsondecode(aws_cloudwatch_event_rule.alert["alarma"].event_pattern).detail == { alarmName = [{ prefix = "solventa-int-" }], state = { value = ["ALARM"] } } &&
      jsondecode(aws_cloudwatch_event_rule.alert["recuperada"].event_pattern).detail == { alarmName = [{ prefix = "solventa-int-" }], state = { value = ["OK"] }, previousState = { value = ["ALARM"] } } &&
      jsondecode(aws_cloudwatch_event_rule.alert["alarma"].event_pattern)["detail-type"] == ["CloudWatch Alarm State Change"] &&
      alltrue([for target in aws_cloudwatch_event_target.alert : target.arn == "arn:aws:sns:us-east-1:123456789012:solventa-int-alertas"])
    )
    error_message = "AC3: se notifica el paso a ALARM y la recuperación a OK desde ALARM, al tópico de alertas del ambiente."
  }
  assert {
    condition = alltrue([
      for key, target in aws_cloudwatch_event_target.alert :
      target.input_transformer[0].input_paths == tomap({
        alarma = "$.detail.alarmName", descripcion = "$.detail.configuration.description", motivo = "$.detail.state.reason",
        valor  = "$.detail.state.reasonData", hora = "$.time"
      }) &&
      strcontains(target.input_transformer[0].input_template, key == "alarma" ? "Estado: ALARMA" : "Estado: RECUPERADA") &&
      alltrue([for texto in ["Ambiente: int", "<alarma>", "<descripcion>", "Valor observado: <valor>", "<hora>", "Tablero: https://"] : strcontains(target.input_transformer[0].input_template, texto)]) &&
      alltrue([for dato in ["123456789012", "arn:", "$.account", "$.resources", "metrics", "Authorization", "token"] : !strcontains(target.input_transformer[0].input_template, dato)])
    ])
    error_message = "AC1: el correo transformado tiene estado, ambiente, alarma, umbral, valor observado, hora y tablero, sin cuenta, ARN ni datos sensibles."
  }
  assert {
    condition = (
      length(aws_cloudwatch_metric_alarm.error_rate) == 2 * length(local.deployed) &&
      aws_cloudwatch_metric_alarm.error_rate["cotizacion-errores-critica"].threshold == 1 &&
      aws_cloudwatch_metric_alarm.error_rate["cotizacion-errores-advertencia"].threshold == 0.9 &&
      alltrue([for alarm in aws_cloudwatch_metric_alarm.error_rate :
        alarm.datapoints_to_alarm == 3 && alarm.evaluation_periods == 5 && alarm.treat_missing_data == "notBreaching" &&
        one([for q in alarm.metric_query : q.expression if q.return_data == true]) == "IF(total > 0, 100 * FILL(errores, 0) / total, 0)" &&
        toset([for q in alarm.metric_query : "${q.metric[0].metric_name}/${q.metric[0].stat}" if length(q.metric) > 0]) == toset(["OperationErrors/Sum", "OperationDuration/SampleCount"])
      ])
    )
    error_message = "AC2: tasa de error por servicio = OperationErrors / SampleCount(OperationDuration) en %, con umbral configurable."
  }
  assert {
    condition = (
      aws_cloudwatch_metric_alarm.availability["api-canales-disponibilidad-critica"].threshold == 99.9 &&
      aws_cloudwatch_metric_alarm.availability["api-canales-disponibilidad-advertencia"].threshold == 99.91 &&
      aws_cloudwatch_metric_alarm.availability["api-socios-disponibilidad-critica"].comparison_operator == "LessThanThreshold" &&
      strcontains(one([for q in aws_cloudwatch_metric_alarm.partner_quota["socio-cuota-critica"].metric_query : q.expression]), "MAX(PartnerQuotaUsage)") &&
      strcontains(one([for q in aws_cloudwatch_metric_alarm.partner_quota["socio-cuota-critica"].metric_query : q.expression]), "Environment = 'int'") &&
      aws_cloudwatch_metric_alarm.partner_quota["socio-cuota-advertencia"].threshold == 90
    )
    error_message = "Disponibilidad por API y cuota de socio con advertencia y crítica configurables."
  }
  assert {
    condition = (
      toset(keys(local.alert_catalog)) == toset([for alarm in local.all_alarms : trimprefix(alarm.alarm_name, "solventa-int-")]) &&
      jsondecode(local.environment["bff-web"]["ALERTAS_CATALOGO"])["perfilamiento-p95-critica"] == { severidad = "critica", titulo = "Perfilamiento p95 por encima de la meta", unidad = "ms", enlace = { tipo = "traza", min_ms = 400 } } &&
      jsondecode(local.environment["bff-web"]["ALERTAS_CATALOGO"])["cola-auditoria-dlq"].severidad == "informativa" &&
      jsondecode(local.environment["bff-web"]["ALERTAS_UMBRALES"])[0] == { metrica = "Perfilamiento p95", unidad = "ms", meta = 400, advertencia = 360, critica = 400 } &&
      local.environment["bff-web"]["ALERTAS_PREFIJO"] == "solventa-int-" && local.environment["bff-web"]["ALERTAS_ADVERTENCIA_PCT"] == "90" &&
      !contains(keys(local.environment["clientes"]), "ALERTAS_CATALOGO")
    )
    error_message = "bff-web recibe el catálogo de alertas, que coincide con las alarmas creadas, y los umbrales de la pantalla."
  }
  assert {
    condition = alltrue([
      for name, statements in local.task_statements :
      strcontains(jsonencode(statements), "cloudwatch:DescribeAlarms") == (name == "bff-web") &&
      strcontains(jsonencode(statements), "cloudwatch:PutMetricData") == (name == "api-socios")
    ]) && !strcontains(jsonencode(local.task_statements["bff-web"]), "cloudwatch:PutMetricAlarm") && !strcontains(jsonencode(local.task_statements["bff-web"]), "cloudwatch:SetAlarmState") && strcontains(jsonencode(local.task_statements["api-socios"]), "\"cloudwatch:namespace\":\"Solventa/Socios\"")
    error_message = "Solo bff-web lee las alarmas (sin modificarlas) y solo api-socios publica PartnerQuotaUsage en Solventa/Socios."
  }
  assert {
    condition     = local.environment["api-socios"]["QUOTA_METRIC_INTERVAL_S"] == "300"
    error_message = "api-socios publica el consumo de cuota cada 5 minutos."
  }
  assert {
    condition     = !contains(keys(local.environment["clientes"]), "CLIENTES_SEMILLA_SINTETICA")
    error_message = "Sin service_environment, clientes no siembra datos sintéticos."
  }
}

# HU-S02: servicios, IAM mínimo, variables, caché, alarmas y tablero del Sprint 2.
run "sprint2" {
  command = plan

  # La política de ejecución incluye el ARN de los log groups: se conoce en el plan.
  override_resource {
    target          = aws_cloudwatch_log_group.service
    override_during = plan
    values          = { arn = "arn:aws:logs:us-east-1:123456789012:log-group:servicio" }
  }
  override_resource {
    target          = aws_cloudwatch_log_group.proxy
    override_during = plan
    values          = { arn = "arn:aws:logs:us-east-1:123456789012:log-group:proxy" }
  }

  assert {
    condition = alltrue([
      for name in ["polizas", "dinero", "adaptador-pagos", "adaptador-firma", "consulta", "avisos"] :
      contains(keys(local.ecs_services), name) && contains(keys(aws_iam_role.execution), name) && contains(keys(aws_iam_role.task), name) &&
      aws_cloudwatch_log_group.service[name].name == "/ecs/solventa-int/${name}" && contains(keys(aws_appautoscaling_target.service), name) &&
      contains(keys(aws_cloudwatch_metric_alarm.error_rate), "${name}-errores-critica") && contains(keys(aws_cloudwatch_metric_alarm.saturation), "${name}-cpu")
    ])
    error_message = "AC1: cada servicio nuevo tiene servicio ECS, roles, logs, autoescalado y alarmas."
  }
  assert {
    condition = alltrue([
      for name, statements in local.task_statements :
      strcontains(jsonencode(statements), "secretsmanager:") == false &&
      contains(flatten([for s in jsondecode(aws_iam_role_policy.execution[name].policy).Statement : s.Resource]), "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-int/aliados/pagos") == (name == "adaptador-pagos") &&
      contains(flatten([for s in jsondecode(aws_iam_role_policy.execution[name].policy).Statement : s.Resource]), "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-int/aliados/firma") == (name == "adaptador-firma") &&
      contains(flatten([for s in jsondecode(aws_iam_role_policy.execution[name].policy).Statement : s.Resource]), "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-int/aliados/push") == (name == "adaptador-firma")
    ])
    error_message = "AC3: solo adaptador-pagos lee el secreto pagos y solo adaptador-firma los de firma y push."
  }
  assert {
    condition = alltrue([
      for name in keys(local.deployed) : alltrue([
        for db, secret in {
          polizas = "solventa-int/db/polizas", dinero = "solventa-int/db/dinero", consulta = "solventa-int/db/consulta", avisos = "solventa-int/db/avisos"
        } : contains(flatten([for s in jsondecode(aws_iam_role_policy.execution[name].policy).Statement : s.Resource]), "arn:aws:secretsmanager:us-east-1:123456789012:secret:${secret}") == (name == db)
      ])
    ]) && local.environment["polizas"]["DB_NAME"] == "polizas" && local.environment["consulta"]["DB_NAME"] == "consulta"
    error_message = "AC3: cada servicio nuevo lee solo las credenciales de su propia base."
  }
  assert {
    condition = alltrue([
      for name, statements in local.task_statements :
      contains(flatten([for s in statements : s.Action if contains(s.Resource, "arn:aws:s3:::solventa-int-polizas-123456789012/*")]), "s3:PutObject") == (name == "polizas") &&
      contains(flatten([for s in statements : s.Action if contains(s.Resource, "arn:aws:s3:::solventa-int-polizas-123456789012/*")]), "s3:GetObject") == contains(["polizas", "consulta"], name) &&
      !contains(flatten([for s in statements : s.Action if contains(s.Resource, "arn:aws:s3:::solventa-int-polizas-123456789012/*")]), "s3:DeleteObject")
    ])
    error_message = "AC3: solo polizas escribe documentos de póliza; polizas y consulta los leen para firmar URL prefirmadas; nadie los borra."
  }
  assert {
    condition = alltrue([
      for name, env in local.environment :
      (lookup(env, "POLIZAS_BUCKET", "") == "solventa-int-polizas-123456789012") == contains(["polizas", "consulta"], name) &&
      (lookup(env, "POLIZAS_BUCKET_SSE", "") == "AES256") == contains(["polizas", "consulta"], name) &&
      (lookup(env, "CACHE_URL", null) == "rediss://solventa-int-cache.abc.cache.amazonaws.com:6379") == contains(["catalogo", "cotizacion", "consulta"], name) &&
      # HU-W05: cotizacion fija el deadline; api-socios y bff-web inician X-Deadline-Ms.
      contains(keys(env), "LATENCY_BUDGET_MS") == contains(["cotizacion", "api-socios", "bff-web"], name)
      ]) && alltrue([
      for name in ["cotizacion", "api-socios", "bff-web"] :
      jsondecode(local.environment[name]["LATENCY_BUDGET_MS"]) == { cotizacion = 700, "socio.cotizar" = 1600, "portal.cotizar" = 1600 }
    ])
    error_message = "POLIZAS_BUCKET, CACHE_URL y LATENCY_BUDGET_MS solo llegan a los servicios que los usan."
  }
  assert {
    condition = (
      toset(keys(aws_vpc_security_group_ingress_rule.cache)) == toset(["catalogo", "cotizacion", "consulta"]) &&
      alltrue([for rule in aws_vpc_security_group_ingress_rule.cache : rule.security_group_id == "sg-cache" && rule.from_port == 6379 && rule.to_port == 6379])
    )
    error_message = "La caché solo admite a catalogo, cotizacion y consulta."
  }
  assert {
    condition = (
      local.environment["adaptador-pagos"]["ALLY_PAGOS_TIMEOUT_MS"] == "5000" && local.environment["adaptador-pagos"]["ALLY_PAGOS_URL"] == "http://simulador-aliados:8080/pagos" &&
      local.environment["adaptador-firma"]["ALLY_FIRMA_TIMEOUT_MS"] == "5000" && local.environment["adaptador-firma"]["ALLY_PUSH_TIMEOUT_MS"] == "2000" &&
      local.environment["polizas"]["ADAPTADOR_FIRMA_URL"] == "http://adaptador-firma:8080" && local.environment["dinero"]["ADAPTADOR_PAGOS_URL"] == "http://adaptador-pagos:8080" &&
      local.environment["avisos"]["ADAPTADOR_FIRMA_URL"] == "http://adaptador-firma:8080" &&
      local.environment["bff-web"]["POLIZAS_URL"] == "http://polizas:8080" && local.environment["bff-web"]["CONSULTA_URL"] == "http://consulta:8080" &&
      local.environment["bff-movil"]["CONSULTA_URL"] == "http://consulta:8080" && local.environment["bff-movil"]["AVISOS_URL"] == "http://avisos:8080" &&
      local.environment["dinero"]["SQS_DINERO_ACEPTACIONES_URL"] == "https://sqs.us-east-1.amazonaws.com/123456789012/solventa-int-dinero-aceptaciones" &&
      local.environment["polizas"]["SQS_POLIZAS_RECAUDOS_URL"] != "" && local.environment["consulta"]["SQS_CONSULTA_PROYECCION_URL"] != "" && local.environment["avisos"]["SQS_AVISOS_URL"] != "" &&
      local.environment["polizas"]["EVENTS_TOPIC_ARN"] != "" && local.environment["dinero"]["OUTBOX_ENABLED"] == "true" && !contains(keys(local.environment["consulta"]), "EVENTS_TOPIC_ARN")
    )
    error_message = "Timeouts de aliados, URLs de Service Connect, colas y Outbox de los servicios nuevos."
  }
  assert {
    condition = alltrue([
      for link in ["bff-web-polizas", "bff-web-consulta", "bff-movil-consulta", "bff-movil-avisos", "dinero-adaptador-pagos", "polizas-adaptador-firma", "avisos-adaptador-firma"] :
      contains(keys(local.service_links), link)
    ]) && !contains(keys(local.service_links), "bff-movil-polizas") && !contains(keys(local.service_links), "polizas-adaptador-pagos")
    error_message = "Solo los enlaces del Sprint 2 declarados en el catálogo."
  }
  assert {
    condition = alltrue([
      for name in ["dinero-aceptaciones", "polizas-recaudos", "consulta-proyeccion", "avisos"] :
      aws_cloudwatch_metric_alarm.dlq[name].dimensions.QueueName == "solventa-int-${name}-dlq" && aws_cloudwatch_metric_alarm.dlq[name].threshold == 0 &&
      aws_cloudwatch_metric_alarm.queue_age[name].dimensions.QueueName == "solventa-int-${name}"
    ])
    error_message = "AC2: la DLQ de cada cola nueva tiene alarma (mecanismo existente)."
  }
  assert {
    condition = (
      toset(keys(aws_cloudwatch_metric_alarm.provider_degradation)) == toset([for p in ["open-finance", "datos-abiertos", "kyc", "pagos", "firma", "push"] : "proveedor-${p}-degradado"]) &&
      one([for q in aws_cloudwatch_metric_alarm.provider_degradation["proveedor-open-finance-degradado"].metric_query : q.expression if q.id == "errores"]) == "FILL(e0, 0) + FILL(e1, 0) + FILL(e2, 0)" &&
      one([for q in aws_cloudwatch_metric_alarm.provider_degradation["proveedor-open-finance-degradado"].metric_query : q.expression if q.return_data == true]) == "IF(total > 0, 100 * errores / total, 0)" &&
      toset([for q in aws_cloudwatch_metric_alarm.provider_degradation["proveedor-open-finance-degradado"].metric_query : q.metric[0].dimensions.Operation if length(q.metric) > 0]) == toset(["consulta.FA_PRODUCTOS_VIGENTES", "consulta.FA_HISTORIAL_PAGOS_12M", "consulta.FA_INGRESOS_AGREGADOS"]) &&
      toset([for q in aws_cloudwatch_metric_alarm.provider_degradation["proveedor-pagos-degradado"].metric_query : "${q.metric[0].metric_name}/${q.metric[0].stat}/${q.metric[0].dimensions.Service}/${q.metric[0].dimensions.Operation}" if length(q.metric) > 0]) == toset(["OperationErrors/Sum/adaptador-pagos/cobro.pagos", "OperationDuration/SampleCount/adaptador-pagos/cobro.pagos"]) &&
      aws_cloudwatch_metric_alarm.provider_degradation["proveedor-push-degradado"].threshold == 20 &&
      aws_cloudwatch_metric_alarm.provider_degradation["proveedor-firma-degradado"].treat_missing_data == "notBreaching"
    )
    error_message = "Degradación por proveedor = OperationErrors / SampleCount(OperationDuration) de sus operaciones (HU-W06)."
  }
  assert {
    condition = (
      one(aws_cloudwatch_metric_alarm.pending_collections).threshold == 900 &&
      one([for q in one(aws_cloudwatch_metric_alarm.pending_collections).metric_query : q.expression if q.return_data == true]) == "MAX([a0, a1])" &&
      toset([for q in one(aws_cloudwatch_metric_alarm.pending_collections).metric_query : q.metric[0].dimensions.QueueName if length(q.metric) > 0]) == toset(["solventa-int-dinero-aceptaciones", "solventa-int-polizas-recaudos"]) &&
      aws_cloudwatch_metric_alarm.push_failures.dimensions == tomap({ Environment = "int", Service = "avisos", Operation = "aviso.push" }) &&
      aws_cloudwatch_metric_alarm.push_failures.threshold == 5 && aws_cloudwatch_metric_alarm.push_failures.comparison_operator == "GreaterThanOrEqualToThreshold"
    )
    error_message = "Alarmas de recaudos pendientes (antigüedad de la Saga) y de fallos de push de avisos."
  }
  assert {
    condition = (
      toset(keys(local.alert_catalog)) == toset([for alarm in local.all_alarms : trimprefix(alarm.alarm_name, "solventa-int-")]) &&
      jsondecode(local.environment["bff-web"]["ALERTAS_CATALOGO"])["recaudos-pendientes"].severidad == "critica" &&
      jsondecode(local.environment["bff-web"]["ALERTAS_CATALOGO"])["cola-dinero-aceptaciones-dlq"].severidad == "informativa"
    )
    error_message = "bff-web conoce las alarmas nuevas (pantalla Operación › Alertas)."
  }
  assert {
    condition = (
      length([for w in local.widgets : w if w.type == "text" && startswith(w.properties.markdown, "### Contratación")]) == 1 &&
      length([for w in local.widgets : w if w.type == "text" && startswith(w.properties.markdown, "### Consulta")]) == 1 &&
      contains(one([for w in local.widgets : w.properties.alarms if w.type == "alarm"]), "arn:aws:cloudwatch:us-east-1:123456789012:alarm:solventa-int-proveedor-pagos-degradado") &&
      length([for w in local.widgets : w if try(w.properties.title, "") == "Caché: aciertos, fallos y CPU"]) == 1 &&
      alltrue([for w in local.widgets : w.y + w.height <= 200])
    )
    error_message = "El tablero tiene las filas Contratación y Consulta (con la caché habilitada)."
  }
}

run "umbrales_configurables" {
  command = plan

  variables {
    profiling_p95_ms           = 500
    warning_ratio              = 0.8
    error_rate_percent         = 2
    error_rate_warning_percent = 1.5
    latency_targets = [
      { id = "emision", label = "Emisión p95", service = "cotizacion", operation = "emision", target_ms = 1500, warning_ms = 1350 },
    ]
  }

  assert {
    condition = (
      aws_cloudwatch_metric_alarm.latency["perfilamiento-p95-critica"].threshold == 500 &&
      aws_cloudwatch_metric_alarm.latency["perfilamiento-p95-advertencia"].threshold == 400 &&
      aws_cloudwatch_metric_alarm.latency["emision-p95-advertencia"].threshold == 1350 &&
      aws_cloudwatch_metric_alarm.latency["emision-p95-critica"].dimensions.Operation == "emision" &&
      aws_cloudwatch_metric_alarm.error_rate["clientes-errores-critica"].threshold == 2 &&
      aws_cloudwatch_metric_alarm.error_rate["clientes-errores-advertencia"].threshold == 1.5 &&
      length(aws_cloudwatch_metric_alarm.latency) == 4
    )
    error_message = "Los umbrales salen de las variables, no del código."
  }
  assert {
    condition     = [for fila in jsondecode(local.environment["bff-web"]["ALERTAS_UMBRALES"]) : fila.metrica] == ["Perfilamiento p95", "Emisión p95", "Tasa de error técnico", "Cuota de socio", "Disponibilidad"]
    error_message = "La tabla de umbrales sigue el orden de las metas configuradas."
  }
}

run "intervalo_de_cuota_invalido" {
  command = plan

  variables {
    partner_quota_interval_seconds = 90
  }

  expect_failures = [var.partner_quota_interval_seconds]
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
    condition = (
      aws_cloudwatch_metric_alarm.latency["perfilamiento-p95-critica"].threshold == 400 &&
      aws_cloudwatch_metric_alarm.latency["perfilamiento-p95-advertencia"].threshold == 360
    )
    error_message = "La alarma de perfilamiento existe sin cotización desplegada (AC4 reproducible)."
  }
  assert {
    condition     = toset(keys(aws_cloudwatch_metric_alarm.error_rate)) == toset(["clientes-errores-advertencia", "clientes-errores-critica", "adaptador-identidad-errores-advertencia", "adaptador-identidad-errores-critica"])
    error_message = "La tasa de error se alarma solo para los servicios desplegados."
  }
  assert {
    condition = (
      toset(keys(local.alert_catalog)) == toset([for alarm in local.all_alarms : trimprefix(alarm.alarm_name, "solventa-int-")])
    )
    error_message = "El catálogo de alertas de bff-web coincide con las alarmas creadas."
  }
}

run "variables_de_qa_por_servicio" {
  command = plan

  variables {
    service_environment = {
      clientes = { CLIENTES_SEMILLA_SINTETICA = "true", ENVIRONMENT = "qa-sobrescrito" }
    }
  }

  assert {
    condition = contains(
      local.containers["clientes"][0].environment,
      { name = "CLIENTES_SEMILLA_SINTETICA", value = "true" }
    )
    error_message = "El contenedor de clientes recibe CLIENTES_SEMILLA_SINTETICA (DEF-01)."
  }
  assert {
    condition     = local.environment["clientes"]["ENVIRONMENT"] == "qa-sobrescrito"
    error_message = "service_environment va al final del merge: tiene prioridad."
  }
  assert {
    condition     = alltrue([for name, env in local.environment : !contains(keys(env), "CLIENTES_SEMILLA_SINTETICA") if name != "clientes"])
    error_message = "La variable de QA solo llega al servicio que la declara."
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
