# Ambiente int (HU-W07) con su archivo versionado y un fixture reducido como el de tests/exp.
# Solo afirma los valores de cotizacion; para el resto de servicios, invariantes relativos a las
# variables (nunca sus números), para no romper los cambios de otros en envs/int/apps.tfvars.
# Se ejecuta aparte, desde infra-validar:
#   terraform -chdir=infra/apps test -test-directory=tests/int -var-file=../envs/int/apps.tfvars
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
          "catalogo"   = { tier = "nucleo", listener_port = null, database = true, publishes = false, cache = true, calls = [], consumes = [], allies = [], stories = [], discoverable = true, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-int/catalogo", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-int/catalogo", name = "solventa-int/catalogo" } }
          "consulta"   = { tier = "proyeccion", listener_port = null, database = true, publishes = false, cache = true, calls = [], consumes = ["consulta-proyeccion"], allies = [], stories = [], discoverable = true, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-int/consulta", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-int/consulta", name = "solventa-int/consulta" } }
          "cotizacion" = { tier = "nucleo", listener_port = null, database = true, publishes = true, cache = true, calls = ["catalogo", "clientes", "adaptador-datos"], consumes = ["consentimientos-cotizacion"], allies = [], stories = [], discoverable = true, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-int/cotizacion", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-int/cotizacion", name = "solventa-int/cotizacion" } }
          "auditoria"  = { tier = "worker", listener_port = null, database = true, publishes = false, cache = false, calls = [], consumes = ["auditoria"], allies = [], stories = [], discoverable = false, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-int/auditoria", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-int/auditoria", name = "solventa-int/auditoria" } }
          # En el catálogo pero sin imagen (no desplegados): cotizacion los llama y ecs.tf arma
          # su <SERVICIO>_URL desde el catálogo completo.
          "clientes"        = { tier = "nucleo", listener_port = null, database = true, publishes = true, cache = false, calls = ["adaptador-identidad"], consumes = [], allies = [], stories = [], discoverable = true, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-int/clientes", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-int/clientes", name = "solventa-int/clientes" } }
          "adaptador-datos" = { tier = "adaptador", listener_port = null, database = false, publishes = false, cache = false, calls = ["simulador-aliados"], consumes = [], allies = ["open-finance", "datos-abiertos"], stories = [], discoverable = true, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-int/adaptador-datos", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-int/adaptador-datos", name = "solventa-int/adaptador-datos" } }
        }
        database = {
          host              = "solventa-int.abc.us-east-1.rds.amazonaws.com"
          port              = 5432
          identifier        = "solventa-int"
          instance_class    = "db.t3.micro"
          multi_az          = false
          security_group_id = "sg-db"
          names             = { catalogo = "catalogo", consulta = "consulta", cotizacion = "cotizacion", auditoria = "auditoria" }
          secrets = {
            catalogo   = "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-int/db/catalogo"
            consulta   = "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-int/db/consulta"
            cotizacion = "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-int/db/cotizacion"
            auditoria  = "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-int/db/auditoria"
          }
        }
        target_groups = {}
        alb           = { arn_suffix = "app/solventa-int-access/1", security_group_id = "sg-alb" }
        queues = {
          consulta-proyeccion        = { arn = "arn:aws:sqs:us-east-1:123456789012:solventa-int-consulta-proyeccion", url = "https://sqs.us-east-1.amazonaws.com/123456789012/solventa-int-consulta-proyeccion", name = "solventa-int-consulta-proyeccion", dlq_arn = "arn:aws:sqs:us-east-1:123456789012:solventa-int-consulta-proyeccion-dlq", dlq_name = "solventa-int-consulta-proyeccion-dlq" }
          consentimientos-cotizacion = { arn = "arn:aws:sqs:us-east-1:123456789012:solventa-int-consentimientos-cotizacion", url = "https://sqs.us-east-1.amazonaws.com/123456789012/solventa-int-consentimientos-cotizacion", name = "solventa-int-consentimientos-cotizacion", dlq_arn = "arn:aws:sqs:us-east-1:123456789012:solventa-int-consentimientos-cotizacion-dlq", dlq_name = "solventa-int-consentimientos-cotizacion-dlq" }
          auditoria                  = { arn = "arn:aws:sqs:us-east-1:123456789012:solventa-int-auditoria", url = "https://sqs.us-east-1.amazonaws.com/123456789012/solventa-int-auditoria", name = "solventa-int-auditoria", dlq_arn = "arn:aws:sqs:us-east-1:123456789012:solventa-int-auditoria-dlq", dlq_name = "solventa-int-auditoria-dlq" }
        }
        events_topic_arn        = "arn:aws:sns:us-east-1:123456789012:solventa-int-eventos-negocio"
        alerts_topic_arn        = "arn:aws:sns:us-east-1:123456789012:solventa-int-alertas"
        audit_bucket            = { name = "solventa-int-auditoria-123456789012", arn = "arn:aws:s3:::solventa-int-auditoria-123456789012" }
        policy_documents_bucket = { name = "solventa-int-polizas-123456789012", arn = "arn:aws:s3:::solventa-int-polizas-123456789012", sse = "AES256" }
        evidence_bucket         = { name = "solventa-int-evidencias-123456789012", arn = "arn:aws:s3:::solventa-int-evidencias-123456789012", sse = "AES256", telemetry_prefix = "telemetria/" }
        cache                   = { enabled = true, url = "rediss://solventa-int-cache.abc.cache.amazonaws.com:6379", security_group_id = "sg-cache", cluster_ids = ["solventa-int-cache-001"], services = ["catalogo", "consulta", "cotizacion"] }
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
    "catalogo"   = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "consulta"   = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "cotizacion" = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "auditoria"  = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
  }
}

run "ambiente_int" {
  command = plan

  # --- cotizacion en int (HU-W07): dos réplicas siempre, una por zona; max hasta HU-W08 ----
  assert {
    condition     = aws_appautoscaling_target.service["cotizacion"].min_capacity == 2 && aws_appautoscaling_target.service["cotizacion"].max_capacity == 3
    error_message = "HU-W07: en int, cotizacion escala entre 2 y 3 tareas, al menos una por zona (min_capacity = ${aws_appautoscaling_target.service["cotizacion"].min_capacity}, max_capacity = ${aws_appautoscaling_target.service["cotizacion"].max_capacity})."
  }
  assert {
    condition     = contains(keys(aws_appautoscaling_policy.cpu), "cotizacion")
    error_message = "HU-W07: cotizacion escala por CPU en int (aws_appautoscaling_policy.cpu)."
  }
  assert {
    # HU-W08: cotizacion no ajusta su política en int; queda con los valores de hoy.
    condition = (
      aws_appautoscaling_policy.cpu["cotizacion"].target_tracking_scaling_policy_configuration[0].target_value == 60 &&
      aws_appautoscaling_policy.cpu["cotizacion"].target_tracking_scaling_policy_configuration[0].scale_out_cooldown == 30 &&
      aws_appautoscaling_policy.cpu["cotizacion"].target_tracking_scaling_policy_configuration[0].scale_in_cooldown == 300
    )
    error_message = "HU-W08: en int, la política cpu de cotizacion queda en 60 % y 30/300 s."
  }

  # --- Resto de servicios: solo invariantes relativos a las variables, sin sus números -----
  assert {
    # El mínimo sale de service_overrides si el servicio lo define; si no, de min_replicas.
    condition = alltrue([
      for name, target in aws_appautoscaling_target.service :
      target.min_capacity == (var.paused ? 0 : coalesce(try(var.service_overrides[name].min, null), var.min_replicas))
    ])
    error_message = "El mínimo de cada servicio debe salir de service_overrides o, sin él, de min_replicas."
  }
  assert {
    # El máximo sale de service_overrides o de max_replicas, y nunca queda por debajo del mínimo.
    condition = alltrue([
      for name, target in aws_appautoscaling_target.service :
      target.max_capacity == (var.paused ? 0 : max(
        coalesce(try(var.service_overrides[name].min, null), var.min_replicas),
        coalesce(try(var.service_overrides[name].max, null), var.max_replicas)
      ))
    ])
    error_message = "El máximo de cada servicio debe salir de service_overrides o, sin él, de max_replicas (y no ser menor que el mínimo)."
  }
  assert {
    # La política de CPU existe solo para los servicios que no son workers y pueden crecer.
    condition = alltrue([
      for name, target in aws_appautoscaling_target.service :
      contains(keys(aws_appautoscaling_policy.cpu), name) == (local.deployed[name].tier != "worker" && target.max_capacity > target.min_capacity)
    ])
    error_message = "La política cpu debe existir si y solo si el servicio no es worker y su máximo supera su mínimo."
  }
  assert {
    # HU-W08: el objetivo y los enfriamientos de cada política salen de service_overrides o, sin
    # él, de cpu_target, scale_out_cooldown_seconds y scale_in_cooldown_seconds.
    condition = alltrue([
      for name, policy in aws_appautoscaling_policy.cpu :
      policy.target_tracking_scaling_policy_configuration[0].target_value == coalesce(try(var.service_overrides[name].cpu_target, null), var.cpu_target) &&
      policy.target_tracking_scaling_policy_configuration[0].scale_out_cooldown == coalesce(try(var.service_overrides[name].scale_out_cooldown, null), var.scale_out_cooldown_seconds) &&
      policy.target_tracking_scaling_policy_configuration[0].scale_in_cooldown == coalesce(try(var.service_overrides[name].scale_in_cooldown, null), var.scale_in_cooldown_seconds)
    ])
    error_message = "El objetivo y los enfriamientos de cada política cpu deben salir de service_overrides o, sin él, de las variables del ambiente."
  }
}
