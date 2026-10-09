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
    condition     = length(aws_cognito_user_pool.backoffice.admin_create_user_config[0].invite_message_template) == 1 && aws_ssm_parameter.web_config.name == "/solventa/int/web/config" && aws_ssm_parameter.web_config.type == "String" && aws_cognito_user_pool_client.backoffice.auth_session_validity == 15
    error_message = "La invitación del back-office y el parámetro de configuración pública del portal deben existir."
  }
  assert {
    condition     = length(aws_ecr_repository.service) == length(local.catalog)
    error_message = "Debe existir un repositorio ECR por servicio del catálogo."
  }
  assert {
    condition     = toset(keys(aws_lb_listener.access)) == toset(["bff-web", "bff-movil", "api-socios"])
    error_message = "Solo los servicios de acceso se publican en el ALB."
  }
  assert {
    condition     = toset(keys(aws_secretsmanager_secret.service_db)) == toset(["api-socios", "clientes", "catalogo", "cotizacion"])
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
    condition     = aws_api_gateway_gateway_response.main["socios-QUOTA_EXCEEDED"].status_code == "429"
    error_message = "El exceso de cuota debe responder 429."
  }
  assert {
    condition     = length(aws_wafv2_web_acl_association.api) == 2 && length(aws_wafv2_web_acl.api.rule) == 2
    error_message = "WAF mínimo (2 reglas) debe proteger ambos APIs."
  }
  assert {
    condition     = contains(local.discoverable, "simulador-aliados") && !contains(local.discoverable, "bff-web")
    error_message = "Solo los servicios invocados se anuncian en Service Connect."
  }
}

run "alta_disponibilidad_con_socios_y_mtls" {
  command = plan

  variables {
    high_availability        = true
    egress_mode              = "nat_gateway"
    use_customer_managed_key = true
    partners = {
      socio-a = { tier = "basico", scopes = ["cotizaciones.escribir"] }
      socio-b = { tier = "estandar", scopes = ["cotizaciones.leer"], enabled = false }
    }
    custom_domain = {
      domain_name     = "socios.example.co"
      certificate_arn = "arn:aws:acm:us-east-1:123456789012:certificate/abc"
      truststore_pem  = "tests/truststore.pem"
    }
    interface_endpoints = ["ecr.api", "logs"]
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
  assert {
    condition     = length(aws_vpc_endpoint.interface) == 2
    error_message = "Deben crearse los endpoints de interfaz solicitados."
  }
}

run "socio_con_plan_inexistente" {
  command = plan

  variables {
    partners = { socio-x = { tier = "premium", scopes = ["cotizaciones.leer"] } }
  }

  expect_failures = [aws_api_gateway_api_key.partner]
}
