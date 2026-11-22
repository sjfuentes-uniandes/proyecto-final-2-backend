# Ambiente int (HU-W07) con su archivo versionado y un fixture reducido como el de tests/exp.
# Solo afirma los valores de cotizacion y, desde HU-W25, los de pagos-parametricos; para el resto de servicios, invariantes relativos a las
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
        container_insights   = true # HU-W25 (25.1): la precondición de backlog por tarea
        catalog = {
          "catalogo"   = { tier = "nucleo", listener_port = null, database = true, publishes = false, cache = true, calls = [], consumes = [], allies = [], stories = [], discoverable = true, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-int/catalogo", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-int/catalogo", name = "solventa-int/catalogo" } }
          "consulta"   = { tier = "proyeccion", listener_port = null, database = true, publishes = false, cache = true, calls = [], consumes = ["consulta-proyeccion"], allies = [], stories = [], discoverable = true, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-int/consulta", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-int/consulta", name = "solventa-int/consulta" } }
          "cotizacion" = { tier = "nucleo", listener_port = null, database = true, publishes = true, cache = true, calls = ["catalogo", "clientes", "adaptador-datos"], consumes = ["consentimientos-cotizacion"], allies = [], stories = [], discoverable = true, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-int/cotizacion", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-int/cotizacion", name = "solventa-int/cotizacion" } }
          "auditoria"  = { tier = "worker", listener_port = null, database = true, publishes = false, cache = false, calls = [], consumes = ["auditoria"], allies = [], stories = [], discoverable = false, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-int/auditoria", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-int/auditoria", name = "solventa-int/auditoria" } }
          # HU-W25: los otros dos workers con cola; pagos-parametricos escala por tarea y avisos no.
          "pagos-parametricos" = { tier = "worker", listener_port = null, database = true, publishes = true, cache = false, calls = ["siniestros"], consumes = ["parametricos-eventos", "parametricos-polizas"], allies = [], stories = [], discoverable = true, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-int/pagos-parametricos", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-int/pagos-parametricos", name = "solventa-int/pagos-parametricos" } }
          "avisos"             = { tier = "worker", listener_port = null, database = true, publishes = false, cache = false, calls = [], consumes = ["avisos"], allies = [], stories = [], discoverable = true, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-int/avisos", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-int/avisos", name = "solventa-int/avisos" } }
          # En el catálogo pero sin imagen (no desplegados): cotizacion los llama y ecs.tf arma
          # su <SERVICIO>_URL desde el catálogo completo.
          "clientes"        = { tier = "nucleo", listener_port = null, database = true, publishes = true, cache = false, calls = ["adaptador-identidad"], consumes = [], allies = [], stories = [], discoverable = true, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-int/clientes", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-int/clientes", name = "solventa-int/clientes" } }
          "adaptador-datos" = { tier = "adaptador", listener_port = null, database = false, publishes = false, cache = false, calls = ["simulador-aliados"], consumes = [], allies = ["open-finance", "datos-abiertos"], stories = [], discoverable = true, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-int/adaptador-datos", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-int/adaptador-datos", name = "solventa-int/adaptador-datos" } }
          "siniestros"      = { tier = "nucleo", listener_port = null, database = true, publishes = true, cache = false, calls = [], consumes = ["siniestros-indemnizaciones"], allies = [], stories = [], discoverable = true, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-int/siniestros", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-int/siniestros", name = "solventa-int/siniestros" } }
        }
        database = {
          host              = "solventa-int.abc.us-east-1.rds.amazonaws.com"
          port              = 5432
          identifier        = "solventa-int"
          instance_class    = "db.t3.micro"
          multi_az          = false
          security_group_id = "sg-db"
          names             = { catalogo = "catalogo", consulta = "consulta", cotizacion = "cotizacion", auditoria = "auditoria", "pagos-parametricos" = "pagos_parametricos", avisos = "avisos" }
          secrets = {
            catalogo   = "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-int/db/catalogo"
            consulta   = "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-int/db/consulta"
            cotizacion = "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-int/db/cotizacion"
            auditoria  = "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-int/db/auditoria"
            # HU-W25
            "pagos-parametricos" = "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-int/db/pagos-parametricos"
            avisos               = "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-int/db/avisos"
          }
        }
        target_groups = {}
        alb           = { arn_suffix = "app/solventa-int-access/1", security_group_id = "sg-alb" }
        queues = {
          consulta-proyeccion        = { arn = "arn:aws:sqs:us-east-1:123456789012:solventa-int-consulta-proyeccion", url = "https://sqs.us-east-1.amazonaws.com/123456789012/solventa-int-consulta-proyeccion", name = "solventa-int-consulta-proyeccion", dlq_arn = "arn:aws:sqs:us-east-1:123456789012:solventa-int-consulta-proyeccion-dlq", dlq_name = "solventa-int-consulta-proyeccion-dlq" }
          consentimientos-cotizacion = { arn = "arn:aws:sqs:us-east-1:123456789012:solventa-int-consentimientos-cotizacion", url = "https://sqs.us-east-1.amazonaws.com/123456789012/solventa-int-consentimientos-cotizacion", name = "solventa-int-consentimientos-cotizacion", dlq_arn = "arn:aws:sqs:us-east-1:123456789012:solventa-int-consentimientos-cotizacion-dlq", dlq_name = "solventa-int-consentimientos-cotizacion-dlq" }
          auditoria                  = { arn = "arn:aws:sqs:us-east-1:123456789012:solventa-int-auditoria", url = "https://sqs.us-east-1.amazonaws.com/123456789012/solventa-int-auditoria", name = "solventa-int-auditoria", dlq_arn = "arn:aws:sqs:us-east-1:123456789012:solventa-int-auditoria-dlq", dlq_name = "solventa-int-auditoria-dlq" }
          parametricos-eventos       = { arn = "arn:aws:sqs:us-east-1:123456789012:solventa-int-parametricos-eventos", url = "https://sqs.us-east-1.amazonaws.com/123456789012/solventa-int-parametricos-eventos", name = "solventa-int-parametricos-eventos", dlq_arn = "arn:aws:sqs:us-east-1:123456789012:solventa-int-parametricos-eventos-dlq", dlq_name = "solventa-int-parametricos-eventos-dlq" }
          parametricos-polizas       = { arn = "arn:aws:sqs:us-east-1:123456789012:solventa-int-parametricos-polizas", url = "https://sqs.us-east-1.amazonaws.com/123456789012/solventa-int-parametricos-polizas", name = "solventa-int-parametricos-polizas", dlq_arn = "arn:aws:sqs:us-east-1:123456789012:solventa-int-parametricos-polizas-dlq", dlq_name = "solventa-int-parametricos-polizas-dlq" }
          avisos                     = { arn = "arn:aws:sqs:us-east-1:123456789012:solventa-int-avisos", url = "https://sqs.us-east-1.amazonaws.com/123456789012/solventa-int-avisos", name = "solventa-int-avisos", dlq_arn = "arn:aws:sqs:us-east-1:123456789012:solventa-int-avisos-dlq", dlq_name = "solventa-int-avisos-dlq" }
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
    # HU-W25
    "pagos-parametricos" = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "avisos"             = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
  }
}

run "ambiente_int" {
  command = plan

  # --- cotizacion en int: dos réplicas siempre, una por zona (HU-W07); hasta 4 (HU-W08) -----
  assert {
    condition     = aws_appautoscaling_target.service["cotizacion"].min_capacity == 2 && aws_appautoscaling_target.service["cotizacion"].max_capacity == 4
    error_message = "HU-W07 y HU-W08: en int, cotizacion escala entre 2 y 4 tareas, al menos una por zona (min_capacity = ${aws_appautoscaling_target.service["cotizacion"].min_capacity}, max_capacity = ${aws_appautoscaling_target.service["cotizacion"].max_capacity})."
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

  # --- pagos-parametricos en int (HU-W25, §c.3): por tarea con T = 10 y antigüedad de 180 s -----
  assert {
    # El interruptor va en su override, sin línea global (§b-7): tareas es RunningTaskCount de
    # pagos-parametricos en solventa-int.
    condition = !var.queue_backlog_per_task && (
      one([for m in aws_appautoscaling_policy.backlog["pagos-parametricos"].target_tracking_scaling_policy_configuration[0].customized_metric_specification[0].metrics : m.expression if m.id == "backlog"]) == "(m0 + m1) / tareas" &&
      one([
        for m in aws_appautoscaling_policy.backlog["pagos-parametricos"].target_tracking_scaling_policy_configuration[0].customized_metric_specification[0].metrics :
        { for d in one(one(m.metric_stat).metric).dimensions : d.name => d.value } if m.id == "tareas"
      ]) == { ClusterName = "solventa-int", ServiceName = "pagos-parametricos" }
    )
    error_message = "HU-W25: en int, la backlog de pagos-parametricos es (m0 + m1) / tareas con RunningTaskCount de solventa-int/pagos-parametricos, encendida en su service_overrides y sin queue_backlog_per_task global."
  }
  assert {
    condition = (
      aws_appautoscaling_policy.backlog["pagos-parametricos"].target_tracking_scaling_policy_configuration[0].target_value == 10 &&
      aws_appautoscaling_policy.backlog["pagos-parametricos"].target_tracking_scaling_policy_configuration[0].scale_out_cooldown == 60 &&
      aws_appautoscaling_policy.backlog["pagos-parametricos"].target_tracking_scaling_policy_configuration[0].scale_in_cooldown == 300
    )
    error_message = "HU-W25 (§b-10): en int, la backlog de pagos-parametricos apunta a 10 mensajes por tarea, con 60/300 s."
  }
  assert {
    # Una política solo de salida por cola, con el Maximum de la antigüedad de esa cola.
    condition = toset([for key, policy in aws_appautoscaling_policy.backlog_age : policy.name if startswith(key, "pagos-parametricos/")]) == toset(["solventa-int-pagos-parametricos-antiguedad-parametricos-eventos", "solventa-int-pagos-parametricos-antiguedad-parametricos-polizas"]) && alltrue([
      for queue in ["parametricos-eventos", "parametricos-polizas"] :
      aws_appautoscaling_policy.backlog_age["pagos-parametricos/${queue}"].target_tracking_scaling_policy_configuration[0].target_value == 180 &&
      aws_appautoscaling_policy.backlog_age["pagos-parametricos/${queue}"].target_tracking_scaling_policy_configuration[0].disable_scale_in == true &&
      aws_appautoscaling_policy.backlog_age["pagos-parametricos/${queue}"].target_tracking_scaling_policy_configuration[0].scale_out_cooldown == 60 &&
      aws_appautoscaling_policy.backlog_age["pagos-parametricos/${queue}"].target_tracking_scaling_policy_configuration[0].customized_metric_specification[0].statistic == "Maximum" &&
      aws_appautoscaling_policy.backlog_age["pagos-parametricos/${queue}"].target_tracking_scaling_policy_configuration[0].customized_metric_specification[0].metric_name == "ApproximateAgeOfOldestMessage" &&
      { for d in aws_appautoscaling_policy.backlog_age["pagos-parametricos/${queue}"].target_tracking_scaling_policy_configuration[0].customized_metric_specification[0].dimensions : d.name => d.value } == { QueueName = "solventa-int-${queue}" }
    ])
    error_message = "HU-W25: en int, pagos-parametricos tiene una política por antigüedad de 180 s en cada una de sus dos colas, solo de salida (disable_scale_in), con Maximum y 60 s de enfriamiento."
  }
  assert {
    # §b-10: un mensaje a la vez por consumidor, para que la cola de la N2 dure lo bastante.
    condition     = one([for e in local.containers["pagos-parametricos"][0].environment : e.value if e.name == "SQS_CONCURRENCIA"]) == "1"
    error_message = "HU-W25 (§b-10): en int, el contenedor de pagos-parametricos recibe SQS_CONCURRENCIA = \"1\"."
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
  assert {
    # HU-W25: el objetivo y los enfriamientos de cada política backlog salen de service_overrides
    # o, sin él, de queue_backlog_target, queue_scale_out_cooldown_seconds y
    # queue_scale_in_cooldown_seconds (nunca de los de CPU). auditoria la tiene en el fixture.
    condition = length(aws_appautoscaling_policy.backlog) > 0 && alltrue([
      for name, policy in aws_appautoscaling_policy.backlog :
      policy.target_tracking_scaling_policy_configuration[0].target_value == coalesce(try(var.service_overrides[name].queue_backlog_target, null), var.queue_backlog_target) &&
      policy.target_tracking_scaling_policy_configuration[0].scale_out_cooldown == coalesce(try(var.service_overrides[name].queue_scale_out_cooldown, null), var.queue_scale_out_cooldown_seconds) &&
      policy.target_tracking_scaling_policy_configuration[0].scale_in_cooldown == coalesce(try(var.service_overrides[name].queue_scale_in_cooldown, null), var.queue_scale_in_cooldown_seconds)
    ])
    error_message = "El objetivo y los enfriamientos de cada política backlog deben salir de service_overrides o, sin él, de las variables de cola del ambiente."
  }
  assert {
    # HU-W25 (25.3b): backlog divide sus colas por las tareas si y solo si el servicio enciende
    # queue_backlog_per_task en service_overrides o, sin él, lo enciende el global.
    condition = alltrue([
      for name, policy in aws_appautoscaling_policy.backlog :
      one([for m in policy.target_tracking_scaling_policy_configuration[0].customized_metric_specification[0].metrics : m.expression if m.id == "backlog"]) == format(
        coalesce(try(var.service_overrides[name].queue_backlog_per_task, null), var.queue_backlog_per_task) ? "(%s) / tareas" : "%s",
        join(" + ", [for index, queue in local.deployed[name].consumes : "m${index}"])
      )
    ])
    error_message = "La expresión de cada política backlog debe ser por tarea si y solo si service_overrides o, sin él, queue_backlog_per_task lo encienden."
  }
  assert {
    # HU-W25 (§b-2): auditoria y avisos siguen sumando sus colas, sin la métrica tareas.
    condition = alltrue([
      for name, policy in aws_appautoscaling_policy.backlog :
      one([for m in policy.target_tracking_scaling_policy_configuration[0].customized_metric_specification[0].metrics : m.expression if m.id == "backlog"]) == join(" + ", [for index, queue in local.deployed[name].consumes : "m${index}"]) &&
      length([for m in policy.target_tracking_scaling_policy_configuration[0].customized_metric_specification[0].metrics : m if m.id == "tareas"]) == 0
      if contains(["auditoria", "avisos"], name)
    ])
    error_message = "HU-W25 (§b-2): la backlog de auditoria y la de avisos siguen sumando los mensajes visibles de sus colas, no por tarea."
  }
  assert {
    # HU-W25: el objetivo de antigüedad solo existe si el servicio lo define en service_overrides.
    condition = alltrue([
      for name, sizing in local.sizing :
      sizing.queue_age_target == try(var.service_overrides[name].queue_age_target, null)
    ])
    error_message = "queue_age_target debe salir de service_overrides; sin él, el servicio no tiene objetivo de antigüedad."
  }
  assert {
    # HU-W25: hay una política por antigüedad por cola si y solo si el servicio tiene backlog y
    # define queue_age_target (avisos no puede: lo rechaza la validación). Es solo de salida, con
    # su objetivo y el enfriamiento de salida de cola del servicio o del ambiente.
    condition = toset(keys(aws_appautoscaling_policy.backlog_age)) == toset(flatten([
      for name, policy in aws_appautoscaling_policy.backlog : [
        for queue in local.deployed[name].consumes : "${name}/${queue}"
      ] if try(var.service_overrides[name].queue_age_target, null) != null
      ])) && alltrue([
      for key, policy in aws_appautoscaling_policy.backlog_age :
      policy.target_tracking_scaling_policy_configuration[0].disable_scale_in == true &&
      policy.target_tracking_scaling_policy_configuration[0].target_value == var.service_overrides[split("/", key)[0]].queue_age_target &&
      policy.target_tracking_scaling_policy_configuration[0].scale_out_cooldown == coalesce(try(var.service_overrides[split("/", key)[0]].queue_scale_out_cooldown, null), var.queue_scale_out_cooldown_seconds)
    ])
    error_message = "La política por antigüedad debe existir por cola si y solo si el servicio tiene backlog y define queue_age_target, solo de salida y con sus valores."
  }

  # --- HU-W09: la tarea k6 del experimento de carga no existe en int -----
  assert {
    condition = !var.load_test_enabled && (
      length(aws_cloudwatch_log_group.load_test) + length(aws_iam_role.load_test_execution) + length(aws_iam_role_policy.load_test_execution) +
      length(aws_ecs_task_definition.load_test) + length(aws_security_group.load_test) + length(aws_vpc_security_group_egress_rule.load_test) +
      length(aws_vpc_security_group_ingress_rule.load_test_alb)
    ) == 0
    error_message = "HU-W09: en int no existe ningún recurso de carga.tf (load_test_enabled solo se enciende en envs/exp)."
  }
}
