# HU-W25 (§b-9): platform sin la salida container_insights, como el estado de un ambiente al que
# todavía no se le aplicó la 25.1. Fixture reducido (solo los campos que usa apps) con
# pagos-parametricos y sus dos colas, sin sus llamadas; terraform test no permite reutilizar el
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
          "pagos-parametricos" = { tier = "worker", listener_port = null, database = true, publishes = true, cache = false, calls = [], consumes = ["parametricos-eventos", "parametricos-polizas"], allies = [], stories = [], discoverable = true, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-int/pagos-parametricos", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-int/pagos-parametricos", name = "solventa-int/pagos-parametricos" } }
        }
        database = {
          host              = "solventa-int.abc.us-east-1.rds.amazonaws.com"
          port              = 5432
          identifier        = "solventa-int"
          instance_class    = "db.t4g.micro"
          multi_az          = false
          security_group_id = "sg-db"
          names             = { "pagos-parametricos" = "pagos_parametricos" }
          secrets = {
            "pagos-parametricos" = "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-int/db/pagos-parametricos"
          }
        }
        target_groups = {}
        alb           = { arn_suffix = "app/solventa-int-access/1", security_group_id = "sg-alb" }
        queues = {
          parametricos-eventos = { arn = "arn:aws:sqs:us-east-1:123456789012:solventa-int-parametricos-eventos", url = "https://sqs.us-east-1.amazonaws.com/123456789012/solventa-int-parametricos-eventos", name = "solventa-int-parametricos-eventos", dlq_arn = "arn:aws:sqs:us-east-1:123456789012:solventa-int-parametricos-eventos-dlq", dlq_name = "solventa-int-parametricos-eventos-dlq" }
          parametricos-polizas = { arn = "arn:aws:sqs:us-east-1:123456789012:solventa-int-parametricos-polizas", url = "https://sqs.us-east-1.amazonaws.com/123456789012/solventa-int-parametricos-polizas", name = "solventa-int-parametricos-polizas", dlq_arn = "arn:aws:sqs:us-east-1:123456789012:solventa-int-parametricos-polizas-dlq", dlq_name = "solventa-int-parametricos-polizas-dlq" }
        }
        events_topic_arn        = "arn:aws:sns:us-east-1:123456789012:solventa-int-eventos-negocio"
        alerts_topic_arn        = "arn:aws:sns:us-east-1:123456789012:solventa-int-alertas"
        audit_bucket            = { name = "solventa-int-auditoria-123456789012", arn = "arn:aws:s3:::solventa-int-auditoria-123456789012" }
        policy_documents_bucket = { name = "solventa-int-polizas-123456789012", arn = "arn:aws:s3:::solventa-int-polizas-123456789012", sse = "AES256" }
        evidence_bucket         = { name = "solventa-int-evidencias-123456789012", arn = "arn:aws:s3:::solventa-int-evidencias-123456789012", sse = "AES256", telemetry_prefix = "telemetria/" }
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
    "pagos-parametricos" = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
  }
}

# Apagado el interruptor, la plataforma vieja no impide el plan: backlog queda como hoy. Es el
# caso de cd-servicios con un ambiente que todavía no tiene la 25.1 aplicada.
run "apagado_sin_container_insights" {
  command = plan

  assert {
    condition     = toset(keys(aws_appautoscaling_policy.backlog)) == toset(["pagos-parametricos"]) && one([for m in aws_appautoscaling_policy.backlog["pagos-parametricos"].target_tracking_scaling_policy_configuration[0].customized_metric_specification[0].metrics : m.expression if m.id == "backlog"]) == "m0 + m1"
    error_message = "HU-W25: sin el interruptor, backlog no necesita Container Insights y sigue sumando las colas."
  }
}

# Encendido y sin Container Insights, el plan falla en la precondición de backlog en vez de dejar
# a pagos-parametricos sin escalar (la expresión sin datos de tareas no actúa).
run "encendido_sin_container_insights" {
  command = plan

  variables {
    queue_backlog_per_task = true
  }

  expect_failures = [aws_appautoscaling_policy.backlog]
}

# HU-W25 (25.3b): la precondición mira solo los servicios que encienden el interruptor. Encendido
# solo en el override de pagos-parametricos, con el global apagado, el plan también falla.
run "encendido_por_servicio_sin_container_insights" {
  command = plan

  variables {
    service_overrides = { pagos-parametricos = { queue_backlog_per_task = true } }
  }

  expect_failures = [aws_appautoscaling_policy.backlog]
}

# Con el global encendido y pagos-parametricos apagado en su override, ningún servicio divide por
# tareas: la plataforma vieja no impide el plan.
run "global_con_servicio_apagado_sin_container_insights" {
  command = plan

  variables {
    queue_backlog_per_task = true
    service_overrides      = { pagos-parametricos = { queue_backlog_per_task = false } }
  }

  assert {
    condition     = one([for m in aws_appautoscaling_policy.backlog["pagos-parametricos"].target_tracking_scaling_policy_configuration[0].customized_metric_specification[0].metrics : m.expression if m.id == "backlog"]) == "m0 + m1"
    error_message = "HU-W25: con pagos-parametricos apagado en su override, su backlog suma las colas y no necesita Container Insights aunque el global esté encendido."
  }
}
