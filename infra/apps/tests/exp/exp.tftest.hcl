# Ambiente exp (HU-S03 AC4, HU-W29 N1) con su archivo versionado y el catálogo completo de
# apps.tftest.hcl (prefijo solventa-exp). Se ejecuta aparte, desde infra-validar:
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
        private_subnet_cidrs = ["10.61.10.0/24", "10.61.11.0/24"]
        kms_key_arn          = null
        cluster              = { id = "arn:aws:ecs:us-east-1:123456789012:cluster/solventa-exp", arn = "arn:aws:ecs:us-east-1:123456789012:cluster/solventa-exp", name = "solventa-exp" }
        namespace_arn        = "arn:aws:servicediscovery:us-east-1:123456789012:namespace/ns-1"
        catalog = {
          "bff-web"              = { tier = "acceso", listener_port = 8081, database = false, publishes = false, cache = false, calls = ["clientes", "cotizacion", "api-socios", "polizas", "consulta", "siniestros", "pagos-parametricos"], consumes = [], allies = [], stories = [], discoverable = false, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-exp/bff-web", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-exp/bff-web", name = "solventa-exp/bff-web" } }
          "bff-movil"            = { tier = "acceso", listener_port = 8082, database = false, publishes = false, cache = false, calls = ["clientes", "consulta", "avisos", "siniestros"], consumes = [], allies = [], stories = [], discoverable = false, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-exp/bff-movil", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-exp/bff-movil", name = "solventa-exp/bff-movil" } }
          "api-socios"           = { tier = "acceso", listener_port = 8083, database = true, publishes = true, cache = false, calls = ["cotizacion"], consumes = [], allies = [], stories = [], discoverable = true, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-exp/api-socios", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-exp/api-socios", name = "solventa-exp/api-socios" } }
          "clientes"             = { tier = "nucleo", listener_port = null, database = true, publishes = true, cache = false, calls = ["adaptador-identidad"], consumes = [], allies = [], stories = [], discoverable = true, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-exp/clientes", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-exp/clientes", name = "solventa-exp/clientes" } }
          "catalogo"             = { tier = "nucleo", listener_port = null, database = true, publishes = false, cache = true, calls = [], consumes = [], allies = [], stories = [], discoverable = true, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-exp/catalogo", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-exp/catalogo", name = "solventa-exp/catalogo" } }
          "cotizacion"           = { tier = "nucleo", listener_port = null, database = true, publishes = true, cache = true, calls = ["catalogo", "clientes", "adaptador-datos"], consumes = ["consentimientos-cotizacion"], allies = [], stories = [], discoverable = true, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-exp/cotizacion", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-exp/cotizacion", name = "solventa-exp/cotizacion" } }
          "adaptador-datos"      = { tier = "adaptador", listener_port = null, database = false, publishes = false, cache = false, calls = ["simulador-aliados"], consumes = [], allies = ["open-finance", "datos-abiertos"], stories = [], discoverable = true, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-exp/adaptador-datos", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-exp/adaptador-datos", name = "solventa-exp/adaptador-datos" } }
          "adaptador-identidad"  = { tier = "adaptador", listener_port = null, database = false, publishes = false, cache = false, calls = ["simulador-aliados"], consumes = [], allies = ["kyc"], stories = [], discoverable = true, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-exp/adaptador-identidad", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-exp/adaptador-identidad", name = "solventa-exp/adaptador-identidad" } }
          "polizas"              = { tier = "nucleo", listener_port = null, database = true, publishes = true, cache = false, calls = ["adaptador-firma"], consumes = ["polizas-recaudos"], allies = [], stories = [], discoverable = true, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-exp/polizas", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-exp/polizas", name = "solventa-exp/polizas" } }
          "dinero"               = { tier = "nucleo", listener_port = null, database = true, publishes = true, cache = false, calls = ["adaptador-pagos"], consumes = ["dinero-aceptaciones", "dinero-indemnizaciones"], allies = [], stories = [], discoverable = false, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-exp/dinero", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-exp/dinero", name = "solventa-exp/dinero" } }
          "adaptador-pagos"      = { tier = "adaptador", listener_port = null, database = false, publishes = false, cache = false, calls = ["simulador-aliados"], consumes = [], allies = ["pagos"], stories = [], discoverable = true, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-exp/adaptador-pagos", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-exp/adaptador-pagos", name = "solventa-exp/adaptador-pagos" } }
          "adaptador-firma"      = { tier = "adaptador", listener_port = null, database = false, publishes = false, cache = false, calls = ["simulador-aliados"], consumes = [], allies = ["firma", "push"], stories = [], discoverable = true, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-exp/adaptador-firma", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-exp/adaptador-firma", name = "solventa-exp/adaptador-firma" } }
          "consulta"             = { tier = "proyeccion", listener_port = null, database = true, publishes = false, cache = true, calls = ["catalogo"], consumes = ["consulta-proyeccion"], allies = [], stories = [], discoverable = true, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-exp/consulta", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-exp/consulta", name = "solventa-exp/consulta" } }
          "avisos"               = { tier = "worker", listener_port = null, database = true, publishes = false, cache = false, calls = ["adaptador-firma"], consumes = ["avisos"], allies = [], stories = [], discoverable = true, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-exp/avisos", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-exp/avisos", name = "solventa-exp/avisos" } }
          "auditoria"            = { tier = "worker", listener_port = null, database = true, publishes = false, cache = false, calls = [], consumes = ["auditoria"], allies = [], stories = [], discoverable = false, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-exp/auditoria", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-exp/auditoria", name = "solventa-exp/auditoria" } }
          "siniestros"           = { tier = "nucleo", listener_port = null, database = true, publishes = true, cache = false, calls = [], consumes = ["siniestros-indemnizaciones"], allies = [], stories = [], discoverable = true, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-exp/siniestros", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-exp/siniestros", name = "solventa-exp/siniestros" } }
          "pagos-parametricos"   = { tier = "worker", listener_port = null, database = true, publishes = true, cache = false, calls = ["siniestros"], consumes = ["parametricos-eventos", "parametricos-polizas"], allies = [], stories = [], discoverable = true, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-exp/pagos-parametricos", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-exp/pagos-parametricos", name = "solventa-exp/pagos-parametricos" } }
          "adaptador-telemetria" = { tier = "adaptador", listener_port = 8084, database = true, publishes = true, cache = false, calls = [], consumes = [], allies = ["telemetria"], stories = [], discoverable = false, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-exp/adaptador-telemetria", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-exp/adaptador-telemetria", name = "solventa-exp/adaptador-telemetria" } }
          "simulador-aliados"    = { tier = "soporte", listener_port = null, database = false, publishes = false, cache = false, calls = [], consumes = [], allies = [], stories = [], discoverable = true, ecr = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/solventa-exp/simulador-aliados", url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/solventa-exp/simulador-aliados", name = "solventa-exp/simulador-aliados" } }
        }
        database = {
          host              = "solventa-exp.abc.us-east-1.rds.amazonaws.com"
          port              = 5432
          identifier        = "solventa-exp"
          instance_class    = "db.t4g.micro"
          multi_az          = false
          security_group_id = "sg-db"
          names             = { "api-socios" = "api_socios", clientes = "clientes", catalogo = "catalogo", cotizacion = "cotizacion", polizas = "polizas", dinero = "dinero", consulta = "consulta", avisos = "avisos", siniestros = "siniestros", "pagos-parametricos" = "pagos_parametricos", "adaptador-telemetria" = "adaptador_telemetria", auditoria = "auditoria" }
          secrets = {
            "api-socios"           = "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-exp/db/api-socios"
            clientes               = "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-exp/db/clientes"
            catalogo               = "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-exp/db/catalogo"
            cotizacion             = "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-exp/db/cotizacion"
            polizas                = "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-exp/db/polizas"
            dinero                 = "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-exp/db/dinero"
            consulta               = "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-exp/db/consulta"
            avisos                 = "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-exp/db/avisos"
            siniestros             = "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-exp/db/siniestros"
            "pagos-parametricos"   = "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-exp/db/pagos-parametricos"
            "adaptador-telemetria" = "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-exp/db/adaptador-telemetria"
            auditoria              = "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-exp/db/auditoria"
          }
        }
        target_groups = {
          "bff-web"              = { arn = "arn:aws:elasticloadbalancing:us-east-1:123456789012:targetgroup/w/1", arn_suffix = "targetgroup/w/1" }
          "bff-movil"            = { arn = "arn:aws:elasticloadbalancing:us-east-1:123456789012:targetgroup/m/1", arn_suffix = "targetgroup/m/1" }
          "api-socios"           = { arn = "arn:aws:elasticloadbalancing:us-east-1:123456789012:targetgroup/s/1", arn_suffix = "targetgroup/s/1" }
          "adaptador-telemetria" = { arn = "arn:aws:elasticloadbalancing:us-east-1:123456789012:targetgroup/t/1", arn_suffix = "targetgroup/t/1" }
        }
        alb = { arn_suffix = "app/solventa-exp-access/1", security_group_id = "sg-alb" }
        queues = {
          auditoria                  = { arn = "arn:aws:sqs:us-east-1:123456789012:solventa-exp-auditoria", url = "https://sqs.us-east-1.amazonaws.com/123456789012/solventa-exp-auditoria", name = "solventa-exp-auditoria", dlq_arn = "arn:aws:sqs:us-east-1:123456789012:solventa-exp-auditoria-dlq", dlq_name = "solventa-exp-auditoria-dlq" }
          consentimientos-cotizacion = { arn = "arn:aws:sqs:us-east-1:123456789012:solventa-exp-consentimientos-cotizacion", url = "https://sqs.us-east-1.amazonaws.com/123456789012/solventa-exp-consentimientos-cotizacion", name = "solventa-exp-consentimientos-cotizacion", dlq_arn = "arn:aws:sqs:us-east-1:123456789012:solventa-exp-consentimientos-cotizacion-dlq", dlq_name = "solventa-exp-consentimientos-cotizacion-dlq" }
          dinero-aceptaciones        = { arn = "arn:aws:sqs:us-east-1:123456789012:solventa-exp-dinero-aceptaciones", url = "https://sqs.us-east-1.amazonaws.com/123456789012/solventa-exp-dinero-aceptaciones", name = "solventa-exp-dinero-aceptaciones", dlq_arn = "arn:aws:sqs:us-east-1:123456789012:solventa-exp-dinero-aceptaciones-dlq", dlq_name = "solventa-exp-dinero-aceptaciones-dlq" }
          polizas-recaudos           = { arn = "arn:aws:sqs:us-east-1:123456789012:solventa-exp-polizas-recaudos", url = "https://sqs.us-east-1.amazonaws.com/123456789012/solventa-exp-polizas-recaudos", name = "solventa-exp-polizas-recaudos", dlq_arn = "arn:aws:sqs:us-east-1:123456789012:solventa-exp-polizas-recaudos-dlq", dlq_name = "solventa-exp-polizas-recaudos-dlq" }
          consulta-proyeccion        = { arn = "arn:aws:sqs:us-east-1:123456789012:solventa-exp-consulta-proyeccion", url = "https://sqs.us-east-1.amazonaws.com/123456789012/solventa-exp-consulta-proyeccion", name = "solventa-exp-consulta-proyeccion", dlq_arn = "arn:aws:sqs:us-east-1:123456789012:solventa-exp-consulta-proyeccion-dlq", dlq_name = "solventa-exp-consulta-proyeccion-dlq" }
          avisos                     = { arn = "arn:aws:sqs:us-east-1:123456789012:solventa-exp-avisos", url = "https://sqs.us-east-1.amazonaws.com/123456789012/solventa-exp-avisos", name = "solventa-exp-avisos", dlq_arn = "arn:aws:sqs:us-east-1:123456789012:solventa-exp-avisos-dlq", dlq_name = "solventa-exp-avisos-dlq" }
          parametricos-eventos       = { arn = "arn:aws:sqs:us-east-1:123456789012:solventa-exp-parametricos-eventos", url = "https://sqs.us-east-1.amazonaws.com/123456789012/solventa-exp-parametricos-eventos", name = "solventa-exp-parametricos-eventos", dlq_arn = "arn:aws:sqs:us-east-1:123456789012:solventa-exp-parametricos-eventos-dlq", dlq_name = "solventa-exp-parametricos-eventos-dlq" }
          parametricos-polizas       = { arn = "arn:aws:sqs:us-east-1:123456789012:solventa-exp-parametricos-polizas", url = "https://sqs.us-east-1.amazonaws.com/123456789012/solventa-exp-parametricos-polizas", name = "solventa-exp-parametricos-polizas", dlq_arn = "arn:aws:sqs:us-east-1:123456789012:solventa-exp-parametricos-polizas-dlq", dlq_name = "solventa-exp-parametricos-polizas-dlq" }
          dinero-indemnizaciones     = { arn = "arn:aws:sqs:us-east-1:123456789012:solventa-exp-dinero-indemnizaciones", url = "https://sqs.us-east-1.amazonaws.com/123456789012/solventa-exp-dinero-indemnizaciones", name = "solventa-exp-dinero-indemnizaciones", dlq_arn = "arn:aws:sqs:us-east-1:123456789012:solventa-exp-dinero-indemnizaciones-dlq", dlq_name = "solventa-exp-dinero-indemnizaciones-dlq" }
          siniestros-indemnizaciones = { arn = "arn:aws:sqs:us-east-1:123456789012:solventa-exp-siniestros-indemnizaciones", url = "https://sqs.us-east-1.amazonaws.com/123456789012/solventa-exp-siniestros-indemnizaciones", name = "solventa-exp-siniestros-indemnizaciones", dlq_arn = "arn:aws:sqs:us-east-1:123456789012:solventa-exp-siniestros-indemnizaciones-dlq", dlq_name = "solventa-exp-siniestros-indemnizaciones-dlq" }
        }
        events_topic_arn        = "arn:aws:sns:us-east-1:123456789012:solventa-exp-eventos-negocio"
        alerts_topic_arn        = "arn:aws:sns:us-east-1:123456789012:solventa-exp-alertas"
        audit_bucket            = { name = "solventa-exp-auditoria-123456789012", arn = "arn:aws:s3:::solventa-exp-auditoria-123456789012" }
        policy_documents_bucket = { name = "solventa-exp-polizas-123456789012", arn = "arn:aws:s3:::solventa-exp-polizas-123456789012", sse = "AES256" }
        evidence_bucket         = { name = "solventa-exp-evidencias-123456789012", arn = "arn:aws:s3:::solventa-exp-evidencias-123456789012", sse = "AES256", telemetry_prefix = "telemetria/" }
        cache                   = { enabled = true, url = "rediss://solventa-exp-cache.abc.cache.amazonaws.com:6379", security_group_id = "sg-cache", cluster_ids = ["solventa-exp-cache-001"], services = ["catalogo", "consulta", "cotizacion"] }
        allies = {
          open-finance   = { secret_arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-exp/aliados/open-finance" }
          datos-abiertos = { secret_arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-exp/aliados/datos-abiertos" }
          kyc            = { secret_arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-exp/aliados/kyc" }
          pagos          = { secret_arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-exp/aliados/pagos" }
          firma          = { secret_arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-exp/aliados/firma" }
          push           = { secret_arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-exp/aliados/push" }
          telemetria     = { secret_arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-exp/aliados/telemetria" }
        }
        registro = { token_secret_arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-exp/registro/token" }
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
          socios  = { id = "a2", name = "solventa-exp-socios", arn = "arn:aws:apigateway:us-east-1::/restapis/a2", stage = "v1", url = "https://a2.execute-api.us-east-1.amazonaws.com/v1", log_group = "/aws/apigateway/solventa-exp-socios" }
        }
        usage_plans        = { basico = "up1", estandar = "up2" }
        metrics_log_group  = "/ecs/solventa-exp/metrics"
        events_log_group   = "/ecs/solventa-exp/events"
        experiments_bucket = { name = "solventa-exp-experimentos-123456789012", arn = "arn:aws:s3:::solventa-exp-experimentos-123456789012" }
      }
    }
  }
}

variables {
  state_bucket = "solventa-tfstate-123456789012"
  environment  = "exp"
  image_digests = {
    "bff-web"              = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "bff-movil"            = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "api-socios"           = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "clientes"             = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "catalogo"             = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "cotizacion"           = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "adaptador-datos"      = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "adaptador-identidad"  = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "auditoria"            = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "simulador-aliados"    = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "polizas"              = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "dinero"               = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "adaptador-pagos"      = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "adaptador-firma"      = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "consulta"             = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "avisos"               = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "siniestros"           = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "pagos-parametricos"   = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "adaptador-telemetria" = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
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
  assert {
    condition     = var.load_test_enabled && toset([for definicion in aws_ecs_task_definition.load_test : definicion.family]) == toset(["solventa-exp-carga", "solventa-exp-carga-alta"])
    error_message = "HU-W09: en exp existen las dos tareas k6 del experimento de carga (solventa-exp-carga y solventa-exp-carga-alta)."
  }
}

# HU-W29 (N1): los servicios críticos mantienen al menos una réplica activa en cada zona.
run "multizona_hu_w29" {
  command = plan

  variables {
    criticos = ["bff-web", "bff-movil", "api-socios", "cotizacion", "polizas", "dinero", "siniestros", "pagos-parametricos"]
  }

  assert {
    condition     = alltrue([for name in var.criticos : contains(keys(local.ecs_services), name) && local.sizing[name].min >= 2])
    error_message = "N1: bff-web, bff-movil, api-socios, cotizacion, polizas, dinero, siniestros y pagos-parametricos tienen 2 o más réplicas mínimas."
  }
  assert {
    condition = alltrue([
      for name in var.criticos :
      local.ecs_services[name].placement.availability_zone_rebalancing == "ENABLED" &&
      local.ecs_services[name].placement.subnet_ids == tolist(["subnet-a", "subnet-b"]) &&
      local.ecs_services[name].placement.capacity_provider == "FARGATE"
    ])
    error_message = "N1: cada servicio crítico se reparte entre las subredes de A y B, con rebalanceo por zona y sin Fargate Spot."
  }
  assert {
    condition = (
      jsondecode(local.environment["bff-web"]["CONTINUIDAD_RECORRIDOS"]).R5.alarmas == ["adaptador-telemetria-errores-critica", "pagos-parametricos-errores-critica", "siniestros-errores-critica", "cola-parametricos-eventos-atraso"] &&
      jsondecode(local.environment["bff-web"]["CONTINUIDAD_RECORRIDOS"]).R1.alarmas == ["cotizacion-p95-critica", "cotizacion-errores-critica", "bff-web-sin-destinos", "api-canales-5xx"] &&
      contains(jsondecode(local.environment["bff-web"]["CONTINUIDAD_RECORRIDOS"]).R3.alarmas, "recaudos-pendientes")
    )
    error_message = "AC1: en exp, I04 sigue R1, R3 y R5 con todas sus alarmas de HU-W28 (ninguna se pierde por nombre)."
  }
  assert {
    condition = (
      local.environment["bff-web"]["CONTINUIDAD_BUCKET"] == "solventa-exp-experimentos-123456789012" &&
      contains([for st in local.task_statements["bff-web"] : st if contains(st.Action, "s3:GetObject")][0].Resource, "arn:aws:s3:::solventa-exp-experimentos-123456789012/hu-w29/*") &&
      length([for st in local.task_statements["bff-web"] : st if contains(st.Action, "s3:GetObject")][0].Resource) == 1 &&
      alltrue([for name, statements in local.task_statements : !strcontains(jsonencode(statements), "experimentos") || name == "bff-web"])
    )
    error_message = "Conciliación (I05): solo bff-web lee, y solo el prefijo hu-w29/, del bucket de experimentos de exp."
  }
}
