# Ambiente exp (HU-S03 AC4) con su archivo versionado y el fixture reducido de apps_sin_cache.
# Se ejecuta aparte, desde infra-validar:
#   terraform -chdir=infra/apps test -test-directory=tests/exp -var-file=../envs/exp/apps.tfvars
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
        environment          = "exp"
        prefix               = "solventa-exp"
        region               = "us-east-1"
        account_id           = "123456789012"
        vpc_id               = "vpc-1"
        private_subnet_ids   = ["subnet-a", "subnet-b"]
        private_subnet_cidrs = ["10.60.10.0/24", "10.60.11.0/24"]
        kms_key_arn          = null
        cluster              = { id = "arn:aws:ecs:us-east-1:123456789012:cluster/solventa-exp", arn = "arn:aws:ecs:us-east-1:123456789012:cluster/solventa-exp", name = "solventa-exp" }
        namespace_arn        = "arn:aws:servicediscovery:us-east-1:123456789012:namespace/ns-1"
        catalog = {
          "catalogo" = { tier = "nucleo", listener_port = null, database = true, publishes = false, cache = true, calls = [], consumes = [], allies = [], stories = [], discoverable = true, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-exp/catalogo", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-exp/catalogo", name = "solventa-exp/catalogo" } }
          "consulta" = { tier = "proyeccion", listener_port = null, database = true, publishes = false, cache = true, calls = [], consumes = ["consulta-proyeccion"], allies = [], stories = [], discoverable = true, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-exp/consulta", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-exp/consulta", name = "solventa-exp/consulta" } }
        }
        database = {
          host              = "solventa-exp.abc.us-east-1.rds.amazonaws.com"
          port              = 5432
          identifier        = "solventa-exp"
          instance_class    = "db.t4g.micro"
          multi_az          = false
          security_group_id = "sg-db"
          names             = { catalogo = "catalogo", consulta = "consulta" }
          secrets = {
            catalogo = "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-exp/db/catalogo"
            consulta = "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-exp/db/consulta"
          }
        }
        target_groups = {}
        alb           = { arn_suffix = "app/solventa-exp-access/1", security_group_id = "sg-alb" }
        queues = {
          consulta-proyeccion = { arn = "arn:aws:sqs:us-east-1:123456789012:solventa-exp-consulta-proyeccion", url = "https://sqs.us-east-1.amazonaws.com/123456789012/solventa-exp-consulta-proyeccion", name = "solventa-exp-consulta-proyeccion", dlq_arn = "arn:aws:sqs:us-east-1:123456789012:solventa-exp-consulta-proyeccion-dlq", dlq_name = "solventa-exp-consulta-proyeccion-dlq" }
        }
        events_topic_arn        = "arn:aws:sns:us-east-1:123456789012:solventa-exp-eventos-negocio"
        alerts_topic_arn        = "arn:aws:sns:us-east-1:123456789012:solventa-exp-alertas"
        audit_bucket            = { name = "solventa-exp-auditoria-123456789012", arn = "arn:aws:s3:::solventa-exp-auditoria-123456789012" }
        policy_documents_bucket = { name = "solventa-exp-polizas-123456789012", arn = "arn:aws:s3:::solventa-exp-polizas-123456789012", sse = "AES256" }
        evidence_bucket         = { name = "solventa-exp-evidencias-123456789012", arn = "arn:aws:s3:::solventa-exp-evidencias-123456789012", sse = "AES256", telemetry_prefix = "telemetria/" }
        cache                   = { enabled = true, url = "rediss://solventa-exp-cache.abc.cache.amazonaws.com:6379", security_group_id = "sg-cache", cluster_ids = ["solventa-exp-cache-001"], services = ["catalogo", "consulta", "cotizacion"] }
        allies                  = {}
        registro                = { token_secret_arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-exp/registro/token" }
        cognito = {
          partners_pool_id    = "us-east-1_p"
          partners_pool_arn   = "arn:aws:cognito-idp:us-east-1:123456789012:userpool/us-east-1_p"
          partners_token_url  = "https://solventa-exp-socios-x.auth.us-east-1.amazoncognito.com/oauth2/token"
          partners_resource   = "solventa"
          customers_pool_id   = "us-east-1_c"
          customers_pool_arn  = "arn:aws:cognito-idp:us-east-1:123456789012:userpool/us-east-1_c"
          backoffice_pool_id  = "us-east-1_b"
          backoffice_pool_arn = "arn:aws:cognito-idp:us-east-1:123456789012:userpool/us-east-1_b"
          issuers             = { customers = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_c", backoffice = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_b", partners = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_p" }
        }
        apis = {
          canales = { id = "a1", name = "solventa-exp-canales", arn = "arn:aws:apigateway:us-east-1::/restapis/a1", stage = "v1", url = "https://a1.execute-api.us-east-1.amazonaws.com/v1", log_group = "/aws/apigateway/solventa-exp-canales" }
        }
        usage_plans       = {}
        metrics_log_group = "/ecs/solventa-exp/metrics"
        events_log_group  = "/ecs/solventa-exp/events"
      }
    }
  }
}


variables {
  state_bucket = "solventa-tfstate-123456789012"
  environment  = "exp"
  image_digests = {
    "catalogo" = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "consulta" = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
  }
}

run "ambiente_exp" {
  command = plan

  assert {
    # modules/ecs-service elige FARGATE o FARGATE_SPOT con esta variable.
    condition     = !var.use_fargate_spot
    error_message = "AC4: exp no usa Fargate Spot (una interrupción no se confunde con la falla inyectada)."
  }
  assert {
    condition     = alltrue([for name, size in local.sizing : size.min == 2 && size.max >= 6])
    error_message = "HU-W29: al menos una réplica activa por zona y margen para escalar."
  }
  assert {
    condition     = var.service_overrides["pagos-parametricos"].max == 10 && var.service_overrides["adaptador-telemetria"].cpu == 512
    error_message = "Capacidad adicional para el recorrido paramétrico de HU-W26."
  }
  assert {
    condition     = local.prefix == "solventa-exp" && aws_cloudwatch_dashboard.main.dashboard_name == "solventa-exp"
    error_message = "exp tiene su tablero y sus alarmas con su propio prefijo."
  }
}
