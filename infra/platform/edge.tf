# Entrada segura: WAF -> API Gateway (REST) -> VPC Link v2 -> ALB interno ->
# BFF/API en ECS. Dos APIs separados:
#   canales -> /web/* (bff-web) y /movil/* (bff-movil), tokens de clientes/back-office
#   socios  -> /*     (api-socios), token client_credentials + API key con plan de
#              uso por socio (cuota independiente, HU-W02) y mTLS opcional.
# Se usa REST y no HTTP API porque solo REST ofrece planes de uso, API keys y WAF.
# REST llega al ALB privado con un VPC Link v2 y el ARN del ALB como destino.

# --- ALB interno y destinos ----------------------------------------------------
# El ALB entra en la capa gratuita de Elastic Load Balancing (750 h/mes).
resource "aws_security_group" "vpc_link" {
  name        = "${local.prefix}-vpc-link"
  description = "Interfaces del VPC Link de API Gateway hacia el ALB"
  vpc_id      = aws_vpc.main.id
}

resource "aws_security_group" "alb" {
  name        = "${local.prefix}-alb"
  description = "ALB interno: solo recibe trafico del VPC Link"
  vpc_id      = aws_vpc.main.id
}

resource "aws_vpc_security_group_egress_rule" "vpc_link" {
  for_each                     = local.access_services
  security_group_id            = aws_security_group.vpc_link.id
  referenced_security_group_id = aws_security_group.alb.id
  ip_protocol                  = "tcp"
  from_port                    = each.value.listener_port
  to_port                      = each.value.listener_port
}

resource "aws_vpc_security_group_ingress_rule" "alb" {
  for_each                     = local.access_services
  security_group_id            = aws_security_group.alb.id
  referenced_security_group_id = aws_security_group.vpc_link.id
  ip_protocol                  = "tcp"
  from_port                    = each.value.listener_port
  to_port                      = each.value.listener_port
  description                  = each.key
}

resource "aws_lb" "access" {
  name                       = "${local.prefix}-access"
  load_balancer_type         = "application"
  internal                   = true
  subnets                    = aws_subnet.private[*].id
  security_groups            = [aws_security_group.alb.id]
  drop_invalid_header_fields = true
  idle_timeout               = 30
}

# Destinos IP registrados por los servicios ECS de apps (retiro de no saludables).
resource "aws_lb_target_group" "access" {
  for_each             = local.access_services
  name                 = "${local.prefix}-${each.key}"
  port                 = 8080
  protocol             = "HTTP"
  target_type          = "ip"
  vpc_id               = aws_vpc.main.id
  deregistration_delay = 15
  health_check {
    path                = "/health"
    matcher             = "200"
    interval            = 10
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 2
  }
}

# Un listener por servicio de acceso: API Gateway ya separó la ruta y reenvía
# solo {proxy}, por lo que el ALB no necesita reglas de ruta.
resource "aws_lb_listener" "access" {
  for_each          = local.access_services
  load_balancer_arn = aws_lb.access.arn
  port              = each.value.listener_port
  protocol          = "HTTP"
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.access[each.key].arn
  }
}

resource "aws_apigatewayv2_vpc_link" "access" {
  name               = "${local.prefix}-access"
  subnet_ids         = aws_subnet.private[*].id
  security_group_ids = [aws_security_group.vpc_link.id]
}

# --- Registro de API Gateway en CloudWatch (configuración de la cuenta/región) ---
resource "aws_iam_role" "apigateway_logs" {
  name = "${local.prefix}-apigateway-logs"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "apigateway.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "apigateway_logs" {
  role       = aws_iam_role.apigateway_logs.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/service-role/AmazonAPIGatewayPushToCloudWatchLogs"
}

resource "aws_api_gateway_account" "main" {
  cloudwatch_role_arn = aws_iam_role.apigateway_logs.arn
  depends_on          = [aws_iam_role_policy_attachment.apigateway_logs]
}

# --- APIs y rutas -------------------------------------------------------------------
locals {
  apis = {
    canales = { description = "Portal web y aplicación móvil", mtls = false }
    socios  = { description = "Canal de socios de distribución", mtls = var.custom_domain != null }
  }

  # Encabezados que API Gateway sobrescribe con datos del token validado; un
  # cliente no puede suplantarlos. X-Request-Id sirve como correlationId cuando
  # el llamador no envía X-Correlation-Id (HU-W27 AC1/AC2).
  routes = {
    web = {
      api = "canales", prefix = "web", service = "bff-web", authorizer = "usuarios", api_key = false, cors = true
      identity = {
        "X-Authenticated-Sub"    = "context.authorizer.claims.sub"
        "X-Authenticated-Issuer" = "context.authorizer.claims.iss"
        # Grupos de Cognito: bff-web exige "operacion" en Operación › Trazas (HU-W27).
        "X-Authenticated-Groups" = "context.authorizer.claims.cognito:groups"
      }
    }
    movil = {
      api = "canales", prefix = "movil", service = "bff-movil", authorizer = "usuarios", api_key = false, cors = false
      identity = {
        "X-Authenticated-Sub" = "context.authorizer.claims.sub"
      }
    }
    socios = {
      api = "socios", prefix = null, service = "api-socios", authorizer = "socios", api_key = true, cors = false
      identity = {
        "X-Partner-Client-Id" = "context.authorizer.claims.client_id"
        "X-Partner-Scopes"    = "context.authorizer.claims.scope"
        "X-Partner-Key-Id"    = "context.identity.apiKeyId"
      }
    }
  }

  authorizers = {
    usuarios = { api = "canales", providers = [aws_cognito_user_pool.customers.arn, aws_cognito_user_pool.backoffice.arn] }
    socios   = { api = "socios", providers = [aws_cognito_user_pool.partners.arn] }
  }
}

resource "aws_api_gateway_rest_api" "main" {
  for_each                     = local.apis
  name                         = "${local.prefix}-${each.key}"
  description                  = each.value.description
  disable_execute_api_endpoint = each.value.mtls
  api_key_source               = "HEADER"
  endpoint_configuration {
    types = ["REGIONAL"]
  }
}

resource "aws_api_gateway_authorizer" "main" {
  for_each        = local.authorizers
  name            = each.key
  rest_api_id     = aws_api_gateway_rest_api.main[each.value.api].id
  type            = "COGNITO_USER_POOLS"
  provider_arns   = each.value.providers
  identity_source = "method.request.header.Authorization"
}

resource "aws_api_gateway_resource" "prefix" {
  for_each    = { for name, route in local.routes : name => route if route.prefix != null }
  rest_api_id = aws_api_gateway_rest_api.main[each.value.api].id
  parent_id   = aws_api_gateway_rest_api.main[each.value.api].root_resource_id
  path_part   = each.value.prefix
}

resource "aws_api_gateway_resource" "proxy" {
  for_each    = local.routes
  rest_api_id = aws_api_gateway_rest_api.main[each.value.api].id
  parent_id   = each.value.prefix == null ? aws_api_gateway_rest_api.main[each.value.api].root_resource_id : aws_api_gateway_resource.prefix[each.key].id
  path_part   = "{proxy+}"
}

resource "aws_api_gateway_method" "proxy" {
  for_each             = local.routes
  rest_api_id          = aws_api_gateway_rest_api.main[each.value.api].id
  resource_id          = aws_api_gateway_resource.proxy[each.key].id
  http_method          = "ANY"
  authorization        = "COGNITO_USER_POOLS"
  authorizer_id        = aws_api_gateway_authorizer.main[each.value.authorizer].id
  authorization_scopes = each.value.authorizer == "socios" ? local.partner_scope_ids : null
  api_key_required     = each.value.api_key
  request_parameters = {
    "method.request.path.proxy" = true
  }
}

resource "aws_api_gateway_integration" "proxy" {
  for_each                = local.routes
  rest_api_id             = aws_api_gateway_rest_api.main[each.value.api].id
  resource_id             = aws_api_gateway_resource.proxy[each.key].id
  http_method             = aws_api_gateway_method.proxy[each.key].http_method
  type                    = "HTTP_PROXY"
  integration_http_method = "ANY"
  connection_type         = "VPC_LINK"
  connection_id           = aws_apigatewayv2_vpc_link.access.id
  integration_target      = aws_lb.access.arn
  uri                     = "http://${aws_lb.access.dns_name}:${local.catalog[each.value.service].listener_port}/{proxy}"
  timeout_milliseconds    = 29000
  request_parameters = merge(
    {
      "integration.request.path.proxy"          = "method.request.path.proxy"
      "integration.request.header.X-Request-Id" = "context.requestId"
    },
    { for header, source in each.value.identity : "integration.request.header.${header}" => source }
  )
}

# Preflight CORS del navegador sin token; el BFF responde los encabezados permitidos.
resource "aws_api_gateway_method" "preflight" {
  for_each      = { for name, route in local.routes : name => route if route.cors }
  rest_api_id   = aws_api_gateway_rest_api.main[each.value.api].id
  resource_id   = aws_api_gateway_resource.proxy[each.key].id
  http_method   = "OPTIONS"
  authorization = "NONE"
  request_parameters = {
    "method.request.path.proxy" = true
  }
}

resource "aws_api_gateway_integration" "preflight" {
  for_each                = { for name, route in local.routes : name => route if route.cors }
  rest_api_id             = aws_api_gateway_rest_api.main[each.value.api].id
  resource_id             = aws_api_gateway_resource.proxy[each.key].id
  http_method             = aws_api_gateway_method.preflight[each.key].http_method
  type                    = "HTTP_PROXY"
  integration_http_method = "OPTIONS"
  connection_type         = "VPC_LINK"
  connection_id           = aws_apigatewayv2_vpc_link.access.id
  integration_target      = aws_lb.access.arn
  uri                     = "http://${aws_lb.access.dns_name}:${local.catalog[each.value.service].listener_port}/{proxy}"
  timeout_milliseconds    = 5000
  request_parameters = {
    "integration.request.path.proxy" = "method.request.path.proxy"
  }
}

# Respuestas del borde: 401 para credenciales inválidas (HU-W01 AC2) y 429 con
# Retry-After cuando un socio agota su cuota o límite (HU-W02 AC2).
locals {
  gateway_responses = {
    for pair in setproduct(keys(local.apis), ["UNAUTHORIZED", "INVALID_API_KEY", "ACCESS_DENIED", "QUOTA_EXCEEDED", "THROTTLED"]) :
    "${pair[0]}-${pair[1]}" => { api = pair[0], type = pair[1] }
  }
  gateway_status = {
    UNAUTHORIZED    = "401"
    INVALID_API_KEY = "401"
    ACCESS_DENIED   = "403"
    QUOTA_EXCEEDED  = "429"
    THROTTLED       = "429"
  }
}

resource "aws_api_gateway_gateway_response" "main" {
  for_each      = local.gateway_responses
  rest_api_id   = aws_api_gateway_rest_api.main[each.value.api].id
  response_type = each.value.type
  status_code   = local.gateway_status[each.value.type]
  response_parameters = merge(
    { "gatewayresponse.header.X-Correlation-Id" = "context.requestId" },
    # El portal (CloudFront) debe poder leer el 401/403 del borde para volver a /ingresar.
    each.value.api == "canales" ? {
      "gatewayresponse.header.Access-Control-Allow-Origin"   = "'${local.portal_url}'"
      "gatewayresponse.header.Access-Control-Expose-Headers" = "'X-Correlation-Id'"
    } : {},
    contains(["QUOTA_EXCEEDED", "THROTTLED"], each.value.type) ? { "gatewayresponse.header.Retry-After" = "'${var.partner_retry_after_seconds}'" } : {}
  )
  response_templates = {
    "application/json" = jsonencode({
      error         = each.value.type
      message       = "$context.error.messageString"
      correlationId = "$context.requestId"
    })
  }
}

# --- Despliegue y stage --------------------------------------------------------
resource "aws_api_gateway_deployment" "main" {
  for_each    = local.apis
  rest_api_id = aws_api_gateway_rest_api.main[each.key].id
  triggers = {
    redeployment = sha1(jsonencode([
      [for name, route in local.routes : [
        aws_api_gateway_resource.proxy[name].id,
        aws_api_gateway_method.proxy[name],
        aws_api_gateway_integration.proxy[name],
        try(aws_api_gateway_integration.preflight[name], null),
      ] if route.api == each.key],
      [for name, response in aws_api_gateway_gateway_response.main : response if local.gateway_responses[name].api == each.key],
      [for name, authorizer in aws_api_gateway_authorizer.main : authorizer if local.authorizers[name].api == each.key],
    ]))
  }
  lifecycle {
    create_before_destroy = true
  }
  depends_on = [aws_api_gateway_integration.proxy, aws_api_gateway_integration.preflight]
}

resource "aws_cloudwatch_log_group" "api" {
  for_each          = local.apis
  name              = "/aws/apigateway/${local.prefix}-${each.key}"
  retention_in_days = var.log_retention_days
  kms_key_id        = local.kms_key_arn
}

resource "aws_api_gateway_stage" "main" {
  for_each             = local.apis
  rest_api_id          = aws_api_gateway_rest_api.main[each.key].id
  deployment_id        = aws_api_gateway_deployment.main[each.key].id
  stage_name           = var.api_stage
  xray_tracing_enabled = true
  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api[each.key].arn
    format = jsonencode({
      requestId          = "$context.requestId"
      xrayTraceId        = "$context.xrayTraceId"
      requestTime        = "$context.requestTimeEpoch"
      httpMethod         = "$context.httpMethod"
      resourcePath       = "$context.resourcePath"
      path               = "$context.path"
      status             = "$context.status"
      responseLatency    = "$context.responseLatency"
      integrationLatency = "$context.integrationLatency"
      integrationStatus  = "$context.integrationStatus"
      errorType          = "$context.error.responseType"
      apiKeyId           = "$context.identity.apiKeyId"
      clientId           = "$context.authorizer.claims.client_id"
      sourceIp           = "$context.identity.sourceIp"
    })
  }
  depends_on = [aws_api_gateway_account.main]
}

resource "aws_api_gateway_method_settings" "main" {
  for_each    = local.apis
  rest_api_id = aws_api_gateway_rest_api.main[each.key].id
  stage_name  = aws_api_gateway_stage.main[each.key].stage_name
  method_path = "*/*"
  settings {
    metrics_enabled        = true
    logging_level          = "ERROR"
    data_trace_enabled     = false
    throttling_rate_limit  = var.api_throttle.rate
    throttling_burst_limit = var.api_throttle.burst
  }
}

# --- Planes de uso y socios ----------------------------------------------------
resource "aws_api_gateway_usage_plan" "partner" {
  for_each    = var.partner_tiers
  name        = "${local.prefix}-socios-${each.key}"
  description = "Cuota y límite aplicados a cada API key de socio de forma independiente"
  api_stages {
    api_id = aws_api_gateway_rest_api.main["socios"].id
    stage  = aws_api_gateway_stage.main["socios"].stage_name
  }
  throttle_settings {
    rate_limit  = each.value.rate
    burst_limit = each.value.burst
  }
  quota_settings {
    limit  = each.value.quota_limit
    period = each.value.quota_period
  }
}

resource "aws_api_gateway_api_key" "partner" {
  for_each    = var.partners
  name        = "${local.prefix}-socio-${each.key}"
  description = "Consumo atribuido al socio ${each.key}"
  enabled     = each.value.enabled
  lifecycle {
    precondition {
      condition     = contains(keys(var.partner_tiers), each.value.tier)
      error_message = "El socio ${each.key} usa un plan inexistente en partner_tiers."
    }
  }
}

resource "aws_api_gateway_usage_plan_key" "partner" {
  for_each      = var.partners
  key_id        = aws_api_gateway_api_key.partner[each.key].id
  key_type      = "API_KEY"
  usage_plan_id = aws_api_gateway_usage_plan.partner[each.value.tier].id
}

# --- Dominio propio con mTLS para socios (opcional) ----------------------------
resource "aws_s3_bucket" "truststore" {
  count         = var.custom_domain == null ? 0 : 1
  bucket        = "${local.prefix}-truststore-${local.account_id}"
  force_destroy = true
}

resource "aws_s3_bucket_versioning" "truststore" {
  count  = var.custom_domain == null ? 0 : 1
  bucket = aws_s3_bucket.truststore[0].id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "truststore" {
  count                   = var.custom_domain == null ? 0 : 1
  bucket                  = aws_s3_bucket.truststore[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_object" "truststore" {
  count      = var.custom_domain == null ? 0 : 1
  bucket     = aws_s3_bucket.truststore[0].id
  key        = "truststore.pem"
  source     = var.custom_domain.truststore_pem
  etag       = filemd5(var.custom_domain.truststore_pem)
  depends_on = [aws_s3_bucket_versioning.truststore]
}

resource "aws_api_gateway_domain_name" "partners" {
  count                    = var.custom_domain == null ? 0 : 1
  domain_name              = var.custom_domain.domain_name
  regional_certificate_arn = var.custom_domain.certificate_arn
  security_policy          = "TLS_1_2"
  endpoint_configuration {
    types = ["REGIONAL"]
  }
  mutual_tls_authentication {
    truststore_uri     = "s3://${aws_s3_bucket.truststore[0].id}/${aws_s3_object.truststore[0].key}"
    truststore_version = aws_s3_object.truststore[0].version_id
  }
}

resource "aws_api_gateway_base_path_mapping" "partners" {
  count       = var.custom_domain == null ? 0 : 1
  api_id      = aws_api_gateway_rest_api.main["socios"].id
  stage_name  = aws_api_gateway_stage.main["socios"].stage_name
  domain_name = aws_api_gateway_domain_name.partners[0].domain_name
}
