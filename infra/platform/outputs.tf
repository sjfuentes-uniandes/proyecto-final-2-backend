output "platform" {
  description = "Contrato consumido por infra/apps. No contiene contraseñas ni secretos."
  value = {
    name                 = var.name
    environment          = var.environment
    prefix               = local.prefix
    region               = var.aws_region
    account_id           = local.account_id
    vpc_id               = aws_vpc.main.id
    private_subnet_ids   = aws_subnet.private[*].id
    private_subnet_cidrs = aws_subnet.private[*].cidr_block
    kms_key_arn          = local.kms_key_arn
    cluster              = { id = aws_ecs_cluster.main.id, arn = aws_ecs_cluster.main.arn, name = aws_ecs_cluster.main.name }
    namespace_arn        = aws_service_discovery_http_namespace.main.arn
    catalog = {
      for name, service in local.catalog : name => merge(service, {
        discoverable = contains(local.discoverable, name)
        ecr          = { arn = aws_ecr_repository.service[name].arn, url = aws_ecr_repository.service[name].repository_url, name = aws_ecr_repository.service[name].name }
      })
    }
    database = {
      host              = aws_db_instance.main.address
      port              = aws_db_instance.main.port
      identifier        = aws_db_instance.main.identifier
      instance_class    = var.db_instance_class
      multi_az          = aws_db_instance.main.multi_az
      security_group_id = aws_security_group.database.id
      names             = local.db_names
      secrets           = { for name, secret in aws_secretsmanager_secret.service_db : name => secret.arn }
    }
    target_groups = { for name, group in aws_lb_target_group.access : name => { arn = group.arn, arn_suffix = group.arn_suffix } }
    alb           = { arn_suffix = aws_lb.access.arn_suffix, security_group_id = aws_security_group.alb.id }
    queues = {
      for name, queue in aws_sqs_queue.main : name => {
        arn      = queue.arn
        url      = queue.url
        name     = queue.name
        dlq_arn  = aws_sqs_queue.dlq[name].arn
        dlq_name = aws_sqs_queue.dlq[name].name
      }
    }
    events_topic_arn = aws_sns_topic.business_events.arn
    alerts_topic_arn = aws_sns_topic.alerts.arn
    audit_bucket     = { name = aws_s3_bucket.audit.id, arn = aws_s3_bucket.audit.arn }
    allies           = { for name, secret in aws_secretsmanager_secret.ally : name => { secret_arn = secret.arn } }
    cognito = {
      partners_pool_id    = aws_cognito_user_pool.partners.id
      partners_pool_arn   = aws_cognito_user_pool.partners.arn
      partners_token_url  = "https://${aws_cognito_user_pool_domain.partners.domain}.auth.${var.aws_region}.amazoncognito.com/oauth2/token"
      partners_resource   = aws_cognito_resource_server.partners.identifier
      customers_pool_id   = aws_cognito_user_pool.customers.id
      customers_pool_arn  = aws_cognito_user_pool.customers.arn
      backoffice_pool_id  = aws_cognito_user_pool.backoffice.id
      backoffice_pool_arn = aws_cognito_user_pool.backoffice.arn
      issuers = {
        customers  = "https://cognito-idp.${var.aws_region}.amazonaws.com/${aws_cognito_user_pool.customers.id}"
        backoffice = "https://cognito-idp.${var.aws_region}.amazonaws.com/${aws_cognito_user_pool.backoffice.id}"
        partners   = "https://cognito-idp.${var.aws_region}.amazonaws.com/${aws_cognito_user_pool.partners.id}"
      }
    }
    apis = {
      for name, api in aws_api_gateway_rest_api.main : name => {
        id        = api.id
        name      = api.name
        arn       = api.arn
        stage     = aws_api_gateway_stage.main[name].stage_name
        url       = aws_api_gateway_stage.main[name].invoke_url
        log_group = aws_cloudwatch_log_group.api[name].name
      }
    }
    usage_plans       = { for name, plan in aws_api_gateway_usage_plan.partner : name => plan.id }
    metrics_log_group = aws_cloudwatch_log_group.metrics.name
    events_log_group  = aws_cloudwatch_log_group.ecs_events.name
  }
}

output "ecr_repositories" {
  value = { for name, repo in aws_ecr_repository.service : name => repo.repository_url }
}

output "api_urls" {
  description = "Canales: <url>/web/... y <url>/movil/...; socios: <url>/... (o el dominio mTLS)."
  value = merge(
    { for name, stage in aws_api_gateway_stage.main : name => stage.invoke_url },
    var.custom_domain == null ? {} : { socios_mtls = "https://${var.custom_domain.domain_name}" }
  )
}

output "partner_domain_target" {
  description = "Registro DNS (CNAME/alias) del dominio mTLS de socios."
  value       = try(aws_api_gateway_domain_name.partners[0].regional_domain_name, null)
}

output "web" {
  value = {
    bucket          = aws_s3_bucket.web.id
    distribution_id = aws_cloudfront_distribution.web.id
    url             = local.portal_url
    config_param    = aws_ssm_parameter.web_config.name
  }
}

output "cognito" {
  description = "Configuración pública de los clientes OAuth (sin secretos)."
  value = {
    customers = {
      pool_id       = aws_cognito_user_pool.customers.id
      domain        = "https://${aws_cognito_user_pool_domain.customers.domain}.auth.${var.aws_region}.amazoncognito.com"
      web_client_id = aws_cognito_user_pool_client.web.id
      app_client_id = aws_cognito_user_pool_client.mobile.id
    }
    backoffice = {
      pool_id   = aws_cognito_user_pool.backoffice.id
      domain    = "https://${aws_cognito_user_pool_domain.backoffice.domain}.auth.${var.aws_region}.amazoncognito.com"
      client_id = aws_cognito_user_pool_client.backoffice.id
    }
    partners = {
      pool_id   = aws_cognito_user_pool.partners.id
      token_url = "https://${aws_cognito_user_pool_domain.partners.domain}.auth.${var.aws_region}.amazoncognito.com/oauth2/token"
      scopes    = local.partner_scope_ids
    }
  }
}

output "partners" {
  description = "Credenciales de los socios de prueba. Leer con terraform output -json partners."
  sensitive   = true
  value = {
    for name, partner in var.partners : name => {
      client_id     = try(aws_cognito_user_pool_client.partner[name].id, null)
      client_secret = try(aws_cognito_user_pool_client.partner[name].client_secret, null)
      api_key       = aws_api_gateway_api_key.partner[name].value
      tier          = partner.tier
      enabled       = partner.enabled
    }
  }
}

output "db_bootstrap_task" {
  description = "Ejecutar una vez (y tras agregar servicios con base) con aws ecs run-task."
  value = {
    cluster         = aws_ecs_cluster.main.arn
    task_definition = aws_ecs_task_definition.db_bootstrap.arn
    subnets         = aws_subnet.private[*].id
    security_group  = aws_security_group.db_bootstrap.id
    log_group       = aws_cloudwatch_log_group.db_bootstrap.name
  }
}

output "alerts_topic_arn" {
  value = aws_sns_topic.alerts.arn
}
