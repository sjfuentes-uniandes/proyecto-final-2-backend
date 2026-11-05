# Plan completo sin credenciales ni recursos reales: `terraform test` en infra/platform.
mock_provider "aws" {
  mock_data "aws_availability_zones" {
    defaults = { names = ["us-east-1a", "us-east-1b", "us-east-1c"] }
  }
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  mock_data "aws_ssm_parameter" {
    defaults = { value = "ami-0123456789abcdef0" }
  }
  mock_resource "aws_db_instance" {
    defaults = {
      address            = "solventa-int.abc.us-east-1.rds.amazonaws.com"
      port               = 5432
      master_user_secret = [{ secret_arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:rds-master", kms_key_id = "k", secret_status = "active" }]
    }
  }
  mock_resource "aws_kms_key" {
    defaults = { arn = "arn:aws:kms:us-east-1:123456789012:key/0000" }
  }
  mock_resource "aws_cognito_user_pool" {
    defaults = { arn = "arn:aws:cognito-idp:us-east-1:123456789012:userpool/us-east-1_x" }
  }
  mock_resource "aws_sqs_queue" {
    defaults = { arn = "arn:aws:sqs:us-east-1:123456789012:q" }
  }
  mock_resource "aws_sns_topic" {
    defaults = { arn = "arn:aws:sns:us-east-1:123456789012:t" }
  }
  mock_resource "aws_s3_bucket" {
    defaults = { arn = "arn:aws:s3:::b" }
  }
  mock_resource "aws_cloudwatch_log_group" {
    defaults = { arn = "arn:aws:logs:us-east-1:123456789012:log-group:g" }
  }
  mock_resource "aws_cloudfront_distribution" {
    defaults = { arn = "arn:aws:cloudfront::123456789012:distribution/E1", domain_name = "d111.cloudfront.net" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/r" }
  }
  mock_resource "aws_lb" {
    override_during = plan
    defaults        = { arn = "arn:aws:elasticloadbalancing:us-east-1:123456789012:loadbalancer/net/a/1", dns_name = "access.elb.amazonaws.com" }
  }
  mock_resource "aws_api_gateway_stage" {
    defaults = { arn = "arn:aws:apigateway:us-east-1::/restapis/a/stages/v1" }
  }
  mock_resource "aws_wafv2_web_acl" {
    defaults = { arn = "arn:aws:wafv2:us-east-1:123456789012:regional/webacl/a/1" }
  }
}

mock_provider "random" {}

run "minimo_costo" {
  command = plan

  assert {
    condition     = aws_db_instance.main.multi_az == false && length(aws_nat_gateway.main) == 0 && length(aws_instance.nat) == 1
    error_message = "El perfil por defecto debe ser Single-AZ con NAT instance y sin NAT Gateway."
  }
  assert {
    condition     = aws_instance.nat[0].instance_type == "t3.micro" && aws_instance.nat[0].source_dest_check == false
    error_message = "La NAT instance debe ser elegible para la capa gratuita y reenviar tráfico."
  }
  assert {
    condition     = length(aws_kms_key.platform) == 0 && aws_sqs_queue.main["auditoria"].sqs_managed_sse_enabled && aws_sns_topic.business_events.kms_master_key_id == "alias/aws/sns"
    error_message = "Sin clave propia se usan claves administradas por AWS."
  }
  assert {
    condition     = aws_lb.access.load_balancer_type == "application" && aws_lb.access.internal
    error_message = "La entrada privada debe ser un ALB interno."
  }
  assert {
    condition     = aws_api_gateway_integration.proxy["web"].connection_type == "VPC_LINK" && aws_api_gateway_integration.proxy["web"].uri == "http://access.elb.amazonaws.com:8081/{proxy}"
    error_message = "API Gateway debe integrarse con el ALB por VPC Link v2."
  }
  assert {
    condition     = aws_api_gateway_integration.proxy["web"].request_parameters["integration.request.header.X-Authenticated-Groups"] == "context.authorizer.claims.cognito:groups" && !contains(keys(aws_api_gateway_integration.proxy["movil"].request_parameters), "integration.request.header.X-Authenticated-Groups")
    error_message = "El BFF web recibe los grupos del token para exigir el grupo operacion (HU-W27)."
  }
  assert {
    condition     = contains(keys(aws_cognito_user_group.backoffice), "administradores") && aws_cognito_user_pool.backoffice.admin_create_user_config[0].allow_admin_create_user_only
    error_message = "Solo los administradores crean usuarios del back-office (grupo administradores)."
  }
  assert {
    condition     = toset(keys(aws_cognito_user_group.backoffice)) == toset(["administradores", "administradores-socios", "asesores", "operacion", "suscriptores", "analistas-siniestros"])
    error_message = "Los grupos del pool backoffice deben coincidir con los que asigna el portal (bff_web.domain.usuarios.GRUPOS)."
  }
  assert {
    condition     = length(aws_cognito_user_pool.backoffice.admin_create_user_config[0].invite_message_template) == 1 && aws_ssm_parameter.web_config.name == "/solventa/int/web/config" && aws_ssm_parameter.web_config.type == "String" && aws_cognito_user_pool_client.backoffice.auth_session_validity == 15
    error_message = "La invitación del back-office y el parámetro de configuración pública del portal deben existir."
  }
  assert {
    condition     = length(aws_ecr_repository.service) == length(local.catalog)
    error_message = "Debe existir un repositorio ECR por servicio del catálogo."
  }
  assert {
    # HU-S03: adaptador-telemetria también, para la entrada POST /telemetria/v1/eventos.
    condition     = toset(keys(aws_lb_listener.access)) == toset(["bff-web", "bff-movil", "api-socios", "adaptador-telemetria"])
    error_message = "Solo los servicios de acceso y la entrada de telemetría se publican en el ALB."
  }
  assert {
    condition     = toset(keys(aws_secretsmanager_secret.service_db)) == toset(["api-socios", "clientes", "catalogo", "cotizacion", "polizas", "dinero", "consulta", "avisos", "siniestros", "pagos-parametricos", "adaptador-telemetria"])
    error_message = "Cada servicio con base debe tener su propio secreto."
  }
  assert {
    condition     = strcontains(local.db_bootstrap_script, "role=api_socios") && strcontains(local.db_bootstrap_script, "PW_API_SOCIOS")
    error_message = "La tarea de preparación debe crear la base de cada servicio."
  }
  assert {
    condition     = aws_api_gateway_rest_api.main["socios"].disable_execute_api_endpoint == false
    error_message = "Sin dominio propio, el endpoint execute-api del API de socios debe seguir activo."
  }
  assert {
    condition     = aws_api_gateway_method.proxy["socios"].api_key_required && !aws_api_gateway_method.proxy["web"].api_key_required
    error_message = "Solo el API de socios exige API key (cuota por socio)."
  }
  assert {
    condition = alltrue([
      aws_api_gateway_integration.proxy["web"].request_parameters["integration.request.header.X-Authenticated-Groups"] == "context.authorizer.claims.cognito:groups",
      aws_api_gateway_integration.proxy["web"].request_parameters["integration.request.header.X-Authenticated-Issuer"] == "context.authorizer.claims.iss",
    ])
    error_message = "bff-web recibe del borde el emisor y los grupos del token (administradores-socios, HU-W01)."
  }
  assert {
    condition = alltrue([
      aws_api_gateway_integration.proxy["socios"].request_parameters["integration.request.header.X-Partner-Client-Id"] == "context.authorizer.claims.client_id",
      aws_api_gateway_integration.proxy["socios"].request_parameters["integration.request.header.X-Partner-Key-Id"] == "context.identity.apiKeyId",
    ])
    error_message = "api-socios distingue el tráfico de socios por X-Partner-*, que el borde siempre sobrescribe (HU-W01)."
  }
  assert {
    condition     = toset([for scope in aws_cognito_resource_server.partners.scope : scope.scope_name]) == toset(["cotizar", "emitir", "consultar", "siniestros"])
    error_message = "Los permisos de socios son los del mockup de HU-W01."
  }
  assert {
    condition     = aws_api_gateway_gateway_response.main["socios-QUOTA_EXCEEDED"].status_code == "429"
    error_message = "El exceso de cuota debe responder 429."
  }
  assert {
    condition = alltrue([
      aws_api_gateway_gateway_response.main["socios-THROTTLED"].status_code == "429",
      aws_api_gateway_gateway_response.main["socios-THROTTLED"].response_parameters["gatewayresponse.header.Retry-After"] == "'1'",
      aws_api_gateway_gateway_response.main["socios-QUOTA_EXCEEDED"].response_parameters["gatewayresponse.header.Retry-After"] == "'3600'",
      aws_api_gateway_gateway_response.main["socios-QUOTA_EXCEEDED"].response_parameters["gatewayresponse.header.X-Correlation-Id"] == "context.requestId",
    ])
    error_message = "Retry-After separado (HU-W02): 1 s por límite por segundo y 3600 s por cuota agotada, con X-Correlation-Id."
  }
  assert {
    condition = alltrue([for name, response in aws_api_gateway_gateway_response.main :
      jsondecode(replace(replace(response.response_templates["application/json"], "$context.error.messageString", "\"Limit Exceeded\""), "$context.requestId", "id")) ==
      { error = split("-", name)[1], message = "Limit Exceeded", correlationId = "id" }
    ])
    error_message = "El cuerpo de las respuestas del borde debe ser JSON válido con error, message y correlationId."
  }
  assert {
    condition     = !contains(keys(aws_api_gateway_gateway_response.main["socios-UNAUTHORIZED"].response_parameters), "gatewayresponse.header.Retry-After")
    error_message = "Solo los 429 indican cuándo reintentar."
  }
  assert {
    condition = alltrue([
      aws_cloudwatch_log_metric_filter.partner_throttled.pattern == "{ $.status = 429 }",
      aws_cloudwatch_log_metric_filter.partner_throttled.metric_transformation[0].dimensions["ApiKeyId"] == "$.apiKeyId",
    ])
    error_message = "Cada 429 se atribuye a la API key del socio en Solventa/Socios PartnerThrottled (HU-W02 AC2)."
  }
  assert {
    condition     = length(aws_wafv2_web_acl_association.api) == 2 && length(aws_wafv2_web_acl.api.rule) == 3
    error_message = "WAF mínimo (2 reglas más el límite de la telemetría, HU-S03) debe proteger ambos APIs."
  }
  assert {
    condition     = contains(local.discoverable, "simulador-aliados") && !contains(local.discoverable, "bff-web")
    error_message = "Solo los servicios invocados se anuncian en Service Connect."
  }
  assert {
    condition = alltrue([
      aws_lambda_function.pre_sign_up.runtime == "python3.12",
      aws_lambda_function.pre_sign_up.handler == "handler.handler",
      aws_lambda_function.pre_sign_up.timeout == 5,
      aws_lambda_permission.pre_sign_up.principal == "cognito-idp.amazonaws.com",
      aws_lambda_permission.pre_sign_up.action == "lambda:InvokeFunction",
      length(aws_cognito_user_pool.customers.lambda_config) == 1,
    ])
    error_message = "El pool clientes debe invocar al Lambda pre sign-up (HU-M06)."
  }
  assert {
    condition     = toset(aws_cognito_user_pool_client.mobile.write_attributes) == toset(["email", "name"]) && !contains(aws_cognito_user_pool_client.mobile.write_attributes, "custom:customer_id")
    error_message = "La app no puede escribir custom:customer_id en el SignUp (suplantación de identidad)."
  }
  assert {
    condition = alltrue([
      aws_api_gateway_method.registro_verificaciones.http_method == "POST",
      aws_api_gateway_method.registro_verificaciones.authorization == "NONE",
      aws_api_gateway_integration.registro_verificaciones.uri == "http://access.elb.amazonaws.com:8082/v1/registro/verificaciones",
      aws_api_gateway_integration.registro_verificaciones.connection_type == "VPC_LINK",
      aws_api_gateway_method_settings.registro_verificaciones.method_path == "movil/v1/registro/verificaciones/POST",
      aws_api_gateway_method_settings.registro_verificaciones.settings[0].throttling_rate_limit == 5,
      aws_api_gateway_method_settings.registro_verificaciones.settings[0].throttling_burst_limit == 10,
    ])
    error_message = "La verificación del registro es la única ruta pública de /movil y tiene su propio tope."
  }
  assert {
    condition     = aws_api_gateway_method.proxy["movil"].authorization == "COGNITO_USER_POOLS"
    error_message = "El resto de /movil sigue exigiendo token de Cognito."
  }
}

# HU-S02: servicios, colas, aliados, documentos de póliza y caché del Sprint 2.
run "sprint2" {
  command = plan

  override_resource {
    target          = aws_s3_bucket.policy_documents
    override_during = plan
    values          = { id = "solventa-int-polizas-123456789012", arn = "arn:aws:s3:::solventa-int-polizas-123456789012" }
  }
  override_resource {
    target          = aws_elasticache_replication_group.cache
    override_during = plan
    values          = { primary_endpoint_address = "solventa-int-cache.abc.cache.amazonaws.com" }
  }
  override_resource {
    target          = aws_sqs_queue.dlq
    override_during = plan
    values          = { arn = "arn:aws:sqs:us-east-1:123456789012:dlq" }
  }

  assert {
    condition = alltrue([
      for name in ["polizas", "dinero", "adaptador-pagos", "adaptador-firma", "consulta", "avisos"] :
      contains(keys(aws_ecr_repository.service), name) && aws_ecr_repository.service[name].name == "solventa-int/${name}"
    ])
    error_message = "AC1: cada servicio nuevo tiene su repositorio ECR."
  }
  assert {
    condition = alltrue([
      for name in ["polizas", "dinero", "consulta", "avisos"] :
      strcontains(local.db_bootstrap_script, "role=${name}") && contains(keys(aws_secretsmanager_secret.service_db), name)
    ]) && !contains(keys(aws_secretsmanager_secret.service_db), "adaptador-pagos") && !contains(keys(aws_secretsmanager_secret.service_db), "adaptador-firma")
    error_message = "AC1: polizas, dinero, consulta y avisos tienen base y usuario propios; los adaptadores no."
  }
  assert {
    condition = alltrue([
      for event, queues in {
        OfertaAceptada         = ["auditoria", "dinero-aceptaciones"]
        RecaudoConfirmado      = ["auditoria", "polizas-recaudos", "avisos"]
        RecaudoRechazado       = ["auditoria", "polizas-recaudos"]
        PolizaEmitida          = ["auditoria", "consulta-proyeccion", "avisos", "parametricos-polizas"] # HU-S03: coberturas paramétricas
        ConsentimientoRevocado = ["auditoria", "consentimientos-cotizacion"]
        } : toset(queues) == toset([
          for name, subscription in aws_sns_topic_subscription.queue : name
          if subscription.filter_policy == null || anytrue([
            for rule in try(jsondecode(subscription.filter_policy).eventType, []) : try(startswith(event, rule.prefix), rule == event)
          ])
      ])
    ])
    error_message = "AC2: cada evento llega solo a las colas cuyo filtro de eventType lo admite (OfertaAceptada solo a dinero-aceptaciones y a la auditoría)."
  }
  assert {
    condition = alltrue([
      for name in ["dinero-aceptaciones", "polizas-recaudos", "consulta-proyeccion", "avisos"] :
      aws_sqs_queue.dlq[name].name == "solventa-int-${name}-dlq" &&
      aws_sqs_queue.dlq[name].message_retention_seconds == 1209600 &&
      jsondecode(aws_sqs_queue.main[name].redrive_policy).maxReceiveCount == 5 &&
      aws_sqs_queue.main[name].sqs_managed_sse_enabled &&
      aws_sns_topic_subscription.queue[name].raw_message_delivery
    ])
    error_message = "AC2: cada cola nueva tiene su DLQ con redrive tras 5 recepciones y está cifrada."
  }
  assert {
    condition     = toset(keys(aws_secretsmanager_secret.ally)) == toset(["open-finance", "datos-abiertos", "kyc", "pagos", "firma", "push", "telemetria"]) && aws_secretsmanager_secret.ally["pagos"].name == "solventa-int/aliados/pagos"
    error_message = "Secretos de aliados pagos, firma y push (con valor de marcador, fuera del estado)."
  }
  assert {
    condition = (
      local.catalog["adaptador-pagos"].allies == ["pagos"] &&
      local.catalog["adaptador-firma"].allies == ["firma", "push"] &&
      length([for name, service in local.catalog : name if contains(service.allies, "pagos")]) == 1 &&
      length([for name, service in local.catalog : name if contains(service.allies, "push")]) == 1
    )
    error_message = "AC3: solo adaptador-pagos usa el secreto pagos y solo adaptador-firma los de firma y push."
  }
  assert {
    condition = (
      # HU-S03 agrega siniestros al final de las llamadas de ambos BFF.
      local.catalog["bff-web"].calls == ["clientes", "cotizacion", "api-socios", "polizas", "consulta", "siniestros"] &&
      local.catalog["bff-movil"].calls == ["clientes", "consulta", "avisos", "siniestros"] && local.catalog["consulta"].calls == ["catalogo"] &&
      local.catalog["dinero"].calls == ["adaptador-pagos"] && local.catalog["polizas"].calls == ["adaptador-firma", "cotizacion", "catalogo"] &&
      alltrue([for name in ["polizas", "consulta", "avisos", "adaptador-pagos", "adaptador-firma"] : contains(local.discoverable, name)]) &&
      !contains(local.discoverable, "dinero")
    )
    error_message = "Llamadas del Sprint 2 y anuncio en Service Connect solo de los servicios invocados."
  }
  assert {
    condition = (
      aws_s3_bucket_versioning.policy_documents.versioning_configuration[0].status == "Enabled" &&
      one(one(aws_s3_bucket_server_side_encryption_configuration.policy_documents.rule).apply_server_side_encryption_by_default).sse_algorithm == "AES256" &&
      aws_s3_bucket_public_access_block.policy_documents.block_public_acls && aws_s3_bucket_public_access_block.policy_documents.block_public_policy &&
      aws_s3_bucket_public_access_block.policy_documents.ignore_public_acls && aws_s3_bucket_public_access_block.policy_documents.restrict_public_buckets &&
      aws_s3_bucket_ownership_controls.policy_documents.rule[0].object_ownership == "BucketOwnerEnforced"
    )
    error_message = "AC3: el bucket de pólizas es versionado, cifrado y sin acceso público."
  }
  assert {
    condition = (
      contains(jsondecode(aws_s3_bucket_policy.policy_documents.policy).Statement, {
        Sid       = "DenyInsecureTransport", Effect = "Deny", Principal = "*", Action = "s3:*"
        Resource  = ["arn:aws:s3:::solventa-int-polizas-123456789012", "arn:aws:s3:::solventa-int-polizas-123456789012/*"]
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      }) &&
      contains(jsondecode(aws_s3_bucket_policy.policy_documents.policy).Statement, {
        Sid       = "DenyUnencryptedWrites", Effect = "Deny", Principal = "*", Action = "s3:PutObject"
        Resource  = "arn:aws:s3:::solventa-int-polizas-123456789012/*"
        Condition = { Null = { "s3:x-amz-server-side-encryption" = "true" } }
      }) &&
      contains(jsondecode(aws_s3_bucket_policy.policy_documents.policy).Statement, {
        Sid       = "DenyOtherEncryption", Effect = "Deny", Principal = "*", Action = "s3:PutObject"
        Resource  = "arn:aws:s3:::solventa-int-polizas-123456789012/*"
        Condition = { StringNotEquals = { "s3:x-amz-server-side-encryption" = "AES256" }, Null = { "s3:x-amz-server-side-encryption" = "false" } }
      }) &&
      alltrue([for statement in jsondecode(aws_s3_bucket_policy.policy_documents.policy).Statement : statement.Effect == "Deny"])
    )
    error_message = "AC3: la política del bucket niega transporte sin TLS y escrituras sin cifrar, y no concede acceso."
  }
  assert {
    condition = (
      length(aws_elasticache_replication_group.cache) == 1 &&
      aws_elasticache_replication_group.cache[0].engine == "valkey" &&
      aws_elasticache_replication_group.cache[0].node_type == "cache.t3.micro" &&
      aws_elasticache_replication_group.cache[0].num_cache_clusters == 1 &&
      aws_elasticache_replication_group.cache[0].transit_encryption_enabled &&
      aws_elasticache_replication_group.cache[0].transit_encryption_mode == "required" &&
      aws_elasticache_replication_group.cache[0].at_rest_encryption_enabled == "true" &&
      aws_elasticache_replication_group.cache[0].auth_token == null &&
      local.cache_url == "rediss://solventa-int-cache.abc.cache.amazonaws.com:6379" &&
      local.cache_services == toset(["catalogo", "cotizacion", "consulta"])
    )
    error_message = "Caché privada Valkey cifrada en tránsito y en reposo para catalogo, cotizacion y consulta."
  }
  assert {
    condition     = output.platform.cache.enabled && output.platform.cache.url == "rediss://solventa-int-cache.abc.cache.amazonaws.com:6379" && output.platform.policy_documents_bucket.sse == "AES256"
    error_message = "El contrato hacia apps incluye la caché y el bucket de pólizas."
  }
}

run "sprint3" {
  command = plan

  override_resource {
    target          = aws_s3_bucket.evidence
    override_during = plan
    values          = { id = "solventa-int-evidencias-123456789012", arn = "arn:aws:s3:::solventa-int-evidencias-123456789012" }
  }
  override_resource {
    target          = aws_sqs_queue.dlq
    override_during = plan
    values          = { arn = "arn:aws:sqs:us-east-1:123456789012:dlq" }
  }

  assert {
    condition = alltrue([
      for name in ["siniestros", "pagos-parametricos", "adaptador-telemetria"] :
      aws_ecr_repository.service[name].name == "solventa-int/${name}" &&
      strcontains(local.db_bootstrap_script, "role=${replace(name, "-", "_")}") && contains(keys(aws_secretsmanager_secret.service_db), name) &&
      local.catalog[name].publishes
    ])
    error_message = "AC1: siniestros, pagos-parametricos y adaptador-telemetria tienen repositorio, base propia y publican eventos (Outbox)."
  }
  assert {
    condition = (
      local.catalog["siniestros"].tier == "nucleo" && local.catalog["siniestros"].calls == [] && local.catalog["siniestros"].consumes == ["siniestros-indemnizaciones"] &&
      local.catalog["pagos-parametricos"].tier == "worker" && local.catalog["pagos-parametricos"].calls == ["siniestros"] &&
      local.catalog["pagos-parametricos"].consumes == ["parametricos-eventos", "parametricos-polizas"] &&
      local.catalog["adaptador-telemetria"].tier == "adaptador" && local.catalog["adaptador-telemetria"].allies == ["telemetria"] &&
      local.catalog["dinero"].consumes == ["dinero-aceptaciones", "dinero-indemnizaciones"] &&
      contains(local.catalog["bff-web"].calls, "siniestros") && contains(local.catalog["bff-movil"].calls, "siniestros") &&
      contains(local.discoverable, "siniestros") && !contains(local.discoverable, "pagos-parametricos") && !contains(local.discoverable, "adaptador-telemetria")
    )
    error_message = "Servicios, llamadas y colas del Sprint 3 como los declara HU-S03."
  }
  assert {
    condition = alltrue([
      for event, queues in {
        EventoParametricoObservado = ["auditoria", "parametricos-eventos"]
        PolizaEmitida              = ["auditoria", "consulta-proyeccion", "avisos", "parametricos-polizas"]
        PolizaCancelada            = ["auditoria", "consulta-proyeccion", "parametricos-polizas"]
        SiniestroAprobado          = ["auditoria", "consulta-proyeccion", "dinero-indemnizaciones"]
        SiniestroRegistrado        = ["auditoria", "consulta-proyeccion"]
        IndemnizacionConfirmada    = ["auditoria", "consulta-proyeccion", "siniestros-indemnizaciones"]
        IndemnizacionRechazada     = ["auditoria", "consulta-proyeccion", "siniestros-indemnizaciones"]
        } : toset(queues) == toset([
          for name, subscription in aws_sns_topic_subscription.queue : name
          if subscription.filter_policy == null || anytrue([
            for rule in try(jsondecode(subscription.filter_policy).eventType, []) : try(startswith(event, rule.prefix), rule == event)
          ])
      ])
    ])
    error_message = "AC2: cada evento del Sprint 3 llega solo a las colas cuyo filtro lo admite; auditoria recibe todo (D3)."
  }
  assert {
    condition = alltrue([
      for name, timeout in { parametricos-eventos = 30, parametricos-polizas = 30, dinero-indemnizaciones = 60, siniestros-indemnizaciones = 30 } :
      aws_sqs_queue.main[name].visibility_timeout_seconds == timeout &&
      aws_sqs_queue.dlq[name].name == "solventa-int-${name}-dlq" && aws_sqs_queue.dlq[name].message_retention_seconds == 1209600 &&
      jsondecode(aws_sqs_queue.main[name].redrive_policy).maxReceiveCount == 5 &&
      aws_sqs_queue.main[name].sqs_managed_sse_enabled && aws_sns_topic_subscription.queue[name].raw_message_delivery
    ])
    error_message = "AC2: cada cola nueva tiene su visibilidad, su DLQ con redrive tras 5 recepciones y cifrado."
  }
  assert {
    condition = (
      aws_sqs_queue.main["parametricos-eventos"].message_retention_seconds == 345600 &&
      alltrue([for name, queue in aws_sqs_queue.main : queue.message_retention_seconds == 345600])
    )
    error_message = "HU-W24: parametricos-eventos retiene 4 días; las colas sin message_retention_seconds conservan los 4 días de siempre."
  }
  assert {
    condition     = aws_sns_topic_subscription.queue["auditoria"].filter_policy == null
    error_message = "D3: la auditoría sigue sin filtro."
  }
  assert {
    condition = (
      jsondecode(aws_secretsmanager_secret_version.ally["telemetria"].secret_string) == { hmac_secret = "CAMBIAR" } &&
      jsondecode(aws_secretsmanager_secret_version.ally["pagos"].secret_string) == { client_id = "CAMBIAR", client_secret = "CAMBIAR" } &&
      length([for name, service in local.catalog : name if contains(service.allies, "telemetria")]) == 1
    )
    error_message = "AC3: el secreto telemetria es un marcador HMAC que solo usa adaptador-telemetria; los demás aliados no cambian."
  }
  assert {
    condition = (
      aws_s3_bucket.evidence.bucket == "solventa-int-evidencias-123456789012" && !aws_s3_bucket.evidence.force_destroy &&
      aws_s3_bucket_versioning.evidence.versioning_configuration[0].status == "Enabled" &&
      one(one(aws_s3_bucket_server_side_encryption_configuration.evidence.rule).apply_server_side_encryption_by_default).sse_algorithm == "AES256" &&
      aws_s3_bucket_public_access_block.evidence.block_public_acls && aws_s3_bucket_public_access_block.evidence.block_public_policy &&
      aws_s3_bucket_public_access_block.evidence.ignore_public_acls && aws_s3_bucket_public_access_block.evidence.restrict_public_buckets &&
      aws_s3_bucket_ownership_controls.evidence.rule[0].object_ownership == "BucketOwnerEnforced"
    )
    error_message = "AC3: el bucket de evidencias es versionado, cifrado, sin acceso público y no se destruye con objetos por omisión."
  }
  assert {
    condition = anytrue([
      for rule in aws_s3_bucket_lifecycle_configuration.evidence.rule :
      rule.id == "cargas-temporales" && rule.status == "Enabled" && one(rule.filter).prefix == "tmp/" && one(rule.expiration).days == 1
    ])
    error_message = "Las cargas de tmp/ expiran a 1 día (24 h hasta la medianoche UTC siguiente)."
  }
  assert {
    condition = (
      contains(jsondecode(aws_s3_bucket_policy.evidence.policy).Statement, {
        Sid       = "DenyInsecureTransport", Effect = "Deny", Principal = "*", Action = "s3:*"
        Resource  = ["arn:aws:s3:::solventa-int-evidencias-123456789012", "arn:aws:s3:::solventa-int-evidencias-123456789012/*"]
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      }) &&
      contains(jsondecode(aws_s3_bucket_policy.evidence.policy).Statement, {
        Sid       = "DenyOutdatedTls", Effect = "Deny", Principal = "*", Action = "s3:*"
        Resource  = ["arn:aws:s3:::solventa-int-evidencias-123456789012", "arn:aws:s3:::solventa-int-evidencias-123456789012/*"]
        Condition = { NumericLessThan = { "s3:TlsVersion" = "1.2" } }
      }) &&
      contains(jsondecode(aws_s3_bucket_policy.evidence.policy).Statement, {
        Sid       = "DenyUnencryptedWrites", Effect = "Deny", Principal = "*", Action = "s3:PutObject"
        Resource  = "arn:aws:s3:::solventa-int-evidencias-123456789012/*"
        Condition = { Null = { "s3:x-amz-server-side-encryption" = "true" } }
      }) &&
      alltrue([for statement in jsondecode(aws_s3_bucket_policy.evidence.policy).Statement : statement.Effect == "Deny"])
    )
    error_message = "AC3: el bucket de evidencias rechaza transporte sin TLS 1.2 y objetos sin cifrar, y su política no concede acceso."
  }
  assert {
    condition     = output.platform.evidence_bucket.name == "solventa-int-evidencias-123456789012" && output.platform.evidence_bucket.sse == "AES256" && output.platform.evidence_bucket.telemetry_prefix == "telemetria/"
    error_message = "El contrato hacia apps incluye el bucket de evidencias y el prefijo del archivo de telemetría."
  }
  assert {
    condition     = aws_cognito_user_group.backoffice["analistas-siniestros"].name == "analistas-siniestros"
    error_message = "AC3: grupo analistas-siniestros en el pool backoffice."
  }
  assert {
    condition = (
      aws_lb_target_group.access["adaptador-telemetria"].name == "solventa-int-telemetria" &&
      aws_lb_target_group.access["bff-web"].name == "solventa-int-bff-web" &&
      aws_lb_listener.access["adaptador-telemetria"].port == 8084
    )
    error_message = "La entrada de telemetría tiene su listener 8084; el destino se acorta a 32 caracteres sin renombrar los previos."
  }
  assert {
    condition = (
      aws_api_gateway_method.telemetria_eventos.authorization == "NONE" && aws_api_gateway_method.telemetria_eventos.http_method == "POST" &&
      aws_api_gateway_resource.telemetria.path_part == "telemetria" && aws_api_gateway_resource.telemetria_eventos.path_part == "eventos" &&
      aws_api_gateway_integration.telemetria_eventos.type == "HTTP_PROXY" &&
      aws_api_gateway_integration.telemetria_eventos.uri == "http://access.elb.amazonaws.com:8084/v1/eventos" &&
      aws_api_gateway_method_settings.telemetria_eventos.method_path == "telemetria/v1/eventos/POST" &&
      one(aws_api_gateway_method_settings.telemetria_eventos.settings).throttling_rate_limit == 50 &&
      one(aws_api_gateway_method_settings.telemetria_eventos.settings).throttling_burst_limit == 100
    )
    error_message = "POST /telemetria/v1/eventos llega sin JWT a adaptador-telemetria (D5) con tope propio en el stage."
  }
  assert {
    condition = anytrue([
      for rule in aws_wafv2_web_acl.api.rule :
      rule.name == "rate-limit-telemetria" && one(one(rule.statement).rate_based_statement).limit == 10000 &&
      one(one(one(one(rule.statement).rate_based_statement).scope_down_statement).byte_match_statement).search_string == "/telemetria/v1/eventos"
      ]) && anytrue([
      for rule in aws_wafv2_web_acl.api.rule :
      rule.name == "rate-limit-ip" && length(one(one(one(rule.statement).rate_based_statement).scope_down_statement).not_statement) == 1
    ])
    error_message = "El WAF limita la telemetría por IP con su propio umbral y la excluye del límite general."
  }
  assert {
    condition     = length(aws_api_gateway_account.main) == 1 && length(aws_iam_role.apigateway_logs) == 1
    error_message = "Por omisión el ambiente administra el rol de logs de API Gateway (int, como antes de HU-S03)."
  }
}

run "sin_cache" {
  command = plan

  variables {
    cache_enabled = false
  }

  assert {
    condition = (
      length(aws_elasticache_replication_group.cache) == 0 && length(aws_elasticache_subnet_group.cache) == 0 &&
      length(aws_security_group.cache) == 0 && local.cache_url == "" &&
      output.platform.cache.url == "" && output.platform.cache.security_group_id == null && !output.platform.cache.enabled
    )
    error_message = "cache_enabled = false: sin recursos de ElastiCache y con la URL vacía."
  }
}

run "alta_disponibilidad_con_socios_y_mtls" {
  command = plan

  variables {
    high_availability        = true
    egress_mode              = "nat_gateway"
    use_customer_managed_key = true
    partners = {
      socio-a = { tier = "basico", scopes = ["cotizar"] }
      socio-b = { tier = "estandar", scopes = ["consultar"], enabled = false }
    }
    custom_domain = {
      domain_name     = "socios.example.co"
      certificate_arn = "arn:aws:acm:us-east-1:123456789012:certificate/abc"
      truststore_pem  = "tests/truststore.pem"
    }
    interface_endpoints = ["ecr.api", "logs"]
    alert_emails        = ["operacion@solventa.example", "guardia@solventa.example"]
  }

  override_resource {
    target          = aws_sns_topic.alerts
    override_during = plan
    values          = { arn = "arn:aws:sns:us-east-1:123456789012:solventa-int-alertas" }
  }

  assert {
    condition     = aws_db_instance.main.multi_az && length(aws_nat_gateway.main) == 2 && length(aws_instance.nat) == 0
    error_message = "Alta disponibilidad: RDS Multi-AZ y un NAT Gateway por zona."
  }
  assert {
    condition     = length(aws_kms_key.platform) == 1
    error_message = "Perfil completo: clave KMS propia."
  }
  assert {
    condition     = length(aws_cognito_user_pool_client.partner) == 1 && length(aws_api_gateway_api_key.partner) == 2
    error_message = "Un socio desactivado conserva su API key deshabilitada y pierde su cliente OAuth."
  }
  assert {
    condition     = aws_api_gateway_api_key.partner["socio-b"].enabled == false
    error_message = "La API key del socio desactivado debe quedar deshabilitada."
  }
  assert {
    condition     = aws_api_gateway_rest_api.main["socios"].disable_execute_api_endpoint && !aws_api_gateway_rest_api.main["canales"].disable_execute_api_endpoint
    error_message = "Con mTLS, el API de socios solo debe responder por el dominio propio."
  }
  # HU-W04: el contrato v1 se publica en /v1 también en el dominio propio.
  assert {
    condition     = aws_api_gateway_base_path_mapping.partners[0].base_path == "v1" && output.api_urls["socios_mtls"] == "https://socios.example.co/v1"
    error_message = "El dominio propio de socios debe exponer el contrato v1 en /v1."
  }
  assert {
    condition     = length(aws_vpc_endpoint.interface) == 2
    error_message = "Deben crearse los endpoints de interfaz solicitados."
  }
  # HU-W28
  assert {
    condition = (
      jsondecode(aws_sns_topic_policy.alerts.policy).Statement == [{
        Sid       = "EventBridgeAlertas"
        Effect    = "Allow"
        Principal = { Service = "events.amazonaws.com" }
        Action    = "sns:Publish"
        Resource  = "arn:aws:sns:us-east-1:123456789012:solventa-int-alertas"
        Condition = { ArnLike = { "aws:SourceArn" = "arn:aws:events:us-east-1:123456789012:rule/solventa-int-alertas-*" } }
      }] &&
      sort([for s in aws_sns_topic_subscription.alert_email : s.endpoint]) == tolist(["guardia@solventa.example", "operacion@solventa.example"]) &&
      alltrue([for s in aws_sns_topic_subscription.alert_email : s.protocol == "email"]) &&
      strcontains(aws_kms_key.platform[0].policy, "events.amazonaws.com")
    )
    error_message = "Solo las reglas de alertas del ambiente publican en el tópico (también cifrado con KMS propia), y solo para los correos de alert_emails."
  }
}

run "plan_de_prueba_y_retry_after_configurables" {
  command = plan

  variables {
    partner_tiers = {
      basico = { rate = 5, burst = 10, quota_limit = 1000, quota_period = "DAY" }
      prueba = { rate = 1, burst = 2, quota_limit = 5, quota_period = "DAY" }
    }
    partners = {
      socio-a = { tier = "prueba", scopes = ["cotizar"] }
      socio-b = { tier = "prueba", scopes = ["cotizar"] }
    }
    partner_throttle_retry_after_seconds = 2
    partner_quota_retry_after_seconds    = 86400
  }

  assert {
    condition = alltrue([
      aws_api_gateway_usage_plan.partner["prueba"].quota_settings[0].limit == 5,
      aws_api_gateway_usage_plan.partner["prueba"].quota_settings[0].period == "DAY",
      aws_api_gateway_usage_plan.partner["prueba"].throttle_settings[0].rate_limit == 1,
      aws_api_gateway_usage_plan.partner["prueba"].throttle_settings[0].burst_limit == 2,
    ])
    error_message = "El plan prueba debe ser determinista: 1 solicitud/s, ráfaga 2 y 5 al día."
  }
  assert {
    condition     = length(aws_api_gateway_usage_plan_key.partner) == 2 && length(aws_api_gateway_api_key.partner) == 2
    error_message = "Cada socio tiene su propia API key en el plan: la cuota no es un límite global compartido."
  }
  assert {
    condition = alltrue([
      aws_api_gateway_gateway_response.main["socios-THROTTLED"].response_parameters["gatewayresponse.header.Retry-After"] == "'2'",
      aws_api_gateway_gateway_response.main["socios-QUOTA_EXCEEDED"].response_parameters["gatewayresponse.header.Retry-After"] == "'86400'",
    ])
    error_message = "Retry-After debe salir de las variables."
  }
}

run "retry_after_fuera_de_rango" {
  command = plan

  variables {
    partner_throttle_retry_after_seconds = 0
    partner_quota_retry_after_seconds    = 30
  }

  expect_failures = [var.partner_throttle_retry_after_seconds, var.partner_quota_retry_after_seconds]
}

run "socio_con_plan_inexistente" {
  command = plan

  variables {
    partners = { socio-x = { tier = "premium", scopes = ["cotizar"] } }
  }

  expect_failures = [aws_api_gateway_api_key.partner]
}
