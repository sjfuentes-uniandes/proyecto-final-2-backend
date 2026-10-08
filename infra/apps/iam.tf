locals {
  task_trust = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = local.p.account_id } }
    }]
  })

  # Capacidades que no se derivan del catálogo.
  audit_writers  = ["auditoria"]
  partner_admins = ["api-socios"]
}

# --- Rol de ejecución: descargar imagen, escribir logs y leer secretos ---------
resource "aws_iam_role" "execution" {
  for_each           = local.deployed
  name               = "${local.prefix}-${each.key}-exec"
  assume_role_policy = local.task_trust
}

resource "aws_iam_role_policy" "execution" {
  for_each = local.deployed
  role     = aws_iam_role.execution[each.key].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [
        { Effect = "Allow", Action = ["ecr:GetAuthorizationToken"], Resource = ["*"] },
        {
          Effect   = "Allow"
          Action   = ["ecr:BatchCheckLayerAvailability", "ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage"]
          Resource = [each.value.ecr.arn]
        },
        {
          Effect   = "Allow"
          Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
          Resource = ["${aws_cloudwatch_log_group.service[each.key].arn}:*", "${aws_cloudwatch_log_group.proxy.arn}:*"]
        },
      ],
      each.value.database || length(each.value.allies) > 0 ? [
        {
          Effect = "Allow"
          Action = ["secretsmanager:GetSecretValue"]
          Resource = concat(
            each.value.database ? [local.p.database.secrets[each.key]] : [],
            [for ally in each.value.allies : local.p.allies[ally].secret_arn]
          )
        },
      ] : [],
      (each.value.database || length(each.value.allies) > 0) && local.p.kms_key_arn != null ? [
        { Effect = "Allow", Action = ["kms:Decrypt"], Resource = [local.p.kms_key_arn] },
      ] : []
    )
  })
}

# --- Rol de tarea: permisos de la aplicación (mínimo privilegio por servicio) ---
resource "aws_iam_role" "task" {
  for_each           = local.deployed
  name               = "${local.prefix}-${each.key}-task"
  assume_role_policy = local.task_trust
}

locals {
  task_statements = {
    for name, service in local.deployed : name => concat(
      # Trazas y métricas EMF del colector ADOT (HU-W27).
      [
        {
          Effect   = "Allow"
          Action   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords", "xray:GetSamplingRules", "xray:GetSamplingTargets", "xray:GetSamplingStatisticSummaries"]
          Resource = ["*"]
        },
        {
          Effect   = "Allow"
          Action   = ["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"]
          Resource = ["arn:${local.partition}:logs:${local.p.region}:${local.p.account_id}:log-group:${local.p.metrics_log_group}:*"]
        },
      ],
      # Relay del Outbox.
      service.publishes ? [
        { Effect = "Allow", Action = ["sns:Publish"], Resource = [local.p.events_topic_arn] },
      ] : [],
      # Consumidor idempotente.
      length(service.consumes) > 0 ? [
        {
          Effect   = "Allow"
          Action   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility", "sqs:GetQueueAttributes", "sqs:GetQueueUrl"]
          Resource = [for queue in service.consumes : local.p.queues[queue].arn]
        },
      ] : [],
      # Registro inmutable de decisiones.
      contains(local.audit_writers, name) ? [
        { Effect = "Allow", Action = ["s3:PutObject", "s3:GetObject", "s3:GetObjectVersion"], Resource = ["${local.p.audit_bucket.arn}/*"] },
        { Effect = "Allow", Action = ["s3:ListBucket"], Resource = [local.p.audit_bucket.arn] },
      ] : [],
      # HU-W01/HU-W02: alta, desactivación y cuota de socios en tiempo de ejecución.
      contains(local.partner_admins, name) ? [
        {
          Effect   = "Allow"
          Action   = ["cognito-idp:CreateUserPoolClient", "cognito-idp:UpdateUserPoolClient", "cognito-idp:DeleteUserPoolClient", "cognito-idp:DescribeUserPoolClient", "cognito-idp:ListUserPoolClients"]
          Resource = [local.p.cognito.partners_pool_arn]
        },
        {
          Effect = "Allow"
          Action = ["apigateway:GET", "apigateway:POST", "apigateway:PATCH", "apigateway:DELETE"]
          Resource = concat(
            [
              "arn:${local.partition}:apigateway:${local.p.region}::/apikeys",
              "arn:${local.partition}:apigateway:${local.p.region}::/apikeys/*",
            ],
            flatten([for plan in values(local.p.usage_plans) : [
              "arn:${local.partition}:apigateway:${local.p.region}::/usageplans/${plan}/keys",
              "arn:${local.partition}:apigateway:${local.p.region}::/usageplans/${plan}/keys/*",
              "arn:${local.partition}:apigateway:${local.p.region}::/usageplans/${plan}/usage",
            ]])
          )
        },
      ] : [],
      # Clave de la plataforma para mensajería y auditoría cifradas.
      local.p.kms_key_arn != null && (service.publishes || length(service.consumes) > 0 || contains(local.audit_writers, name)) ? [
        { Effect = "Allow", Action = ["kms:Decrypt", "kms:GenerateDataKey"], Resource = [local.p.kms_key_arn] },
      ] : [],
    )
  }
}

resource "aws_iam_role_policy" "task" {
  for_each = local.deployed
  role     = aws_iam_role.task[each.key].id
  policy   = jsonencode({ Version = "2012-10-17", Statement = local.task_statements[each.key] })
}
