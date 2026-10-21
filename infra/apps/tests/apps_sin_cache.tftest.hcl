# HU-S02: platform con cache_enabled = false. Fixture reducido (solo los campos que
# usa apps) con dos clientes de la caché; terraform test no permite reutilizar el
# fixture de apps.tftest.hcl desde otro archivo.
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
          "catalogo" = { tier = "nucleo", listener_port = null, database = true, publishes = false, cache = true, calls = [], consumes = [], allies = [], stories = [], discoverable = true, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-int/catalogo", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-int/catalogo", name = "solventa-int/catalogo" } }
          "consulta" = { tier = "proyeccion", listener_port = null, database = true, publishes = false, cache = true, calls = [], consumes = ["consulta-proyeccion"], allies = [], stories = [], discoverable = true, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-int/consulta", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-int/consulta", name = "solventa-int/consulta" } }
        }
        database = {
          host              = "solventa-int.abc.us-east-1.rds.amazonaws.com"
          port              = 5432
          identifier        = "solventa-int"
          instance_class    = "db.t4g.micro"
          multi_az          = false
          security_group_id = "sg-db"
          names             = { catalogo = "catalogo", consulta = "consulta" }
          secrets = {
            catalogo = "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-int/db/catalogo"
            consulta = "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-int/db/consulta"
          }
        }
        target_groups = {}
        alb           = { arn_suffix = "app/solventa-int-access/1", security_group_id = "sg-alb" }
        queues = {
          consulta-proyeccion = { arn = "arn:aws:sqs:us-east-1:123456789012:solventa-int-consulta-proyeccion", url = "https://sqs.us-east-1.amazonaws.com/123456789012/solventa-int-consulta-proyeccion", name = "solventa-int-consulta-proyeccion", dlq_arn = "arn:aws:sqs:us-east-1:123456789012:solventa-int-consulta-proyeccion-dlq", dlq_name = "solventa-int-consulta-proyeccion-dlq" }
        }
        events_topic_arn        = "arn:aws:sns:us-east-1:123456789012:solventa-int-eventos-negocio"
        alerts_topic_arn        = "arn:aws:sns:us-east-1:123456789012:solventa-int-alertas"
        audit_bucket            = { name = "solventa-int-auditoria-123456789012", arn = "arn:aws:s3:::solventa-int-auditoria-123456789012" }
        policy_documents_bucket = { name = "solventa-int-polizas-123456789012", arn = "arn:aws:s3:::solventa-int-polizas-123456789012", sse = "AES256" }
        cache                   = { enabled = false, url = "", security_group_id = null, cluster_ids = [], services = ["catalogo", "consulta", "cotizacion"] }
        allies                  = {}
        registro                = { token_secret_arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-int/registro/token" }
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
        }
        usage_plans       = {}
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
    "catalogo" = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "consulta" = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
  }
}

run "cache_deshabilitada" {
  command = plan

  assert {
    condition     = length(aws_vpc_security_group_ingress_rule.cache) == 0
    error_message = "Sin caché no hay reglas hacia ElastiCache."
  }
  assert {
    condition     = local.environment["catalogo"]["CACHE_URL"] == "" && local.environment["consulta"]["CACHE_URL"] == ""
    error_message = "Sin caché, CACHE_URL queda vacía y los servicios leen de RDS."
  }
  assert {
    condition = (
      length([for w in local.widgets : w if try(w.properties.title, "") == "Caché: aciertos, fallos y CPU"]) == 0 &&
      strcontains(one([for w in local.widgets : w.properties.markdown if w.type == "text" && startswith(w.properties.markdown, "### Consulta")]), "caché deshabilitada")
    )
    error_message = "El tablero indica que la caché está deshabilitada y no grafica ElastiCache."
  }
  assert {
    condition     = length(aws_cloudwatch_metric_alarm.pending_collections) == 0 && toset(keys(local.alert_catalog)) == toset([for alarm in local.all_alarms : trimprefix(alarm.alarm_name, "solventa-int-")])
    error_message = "Sin las colas de la Saga no hay alarma de recaudos pendientes, y el catálogo de alertas sigue coincidiendo."
  }
}
