# Opcional (use_customer_managed_key): una clave administrada por el cliente para
# secretos, RDS, mensajería, logs y auditoría. Sin ella, cada servicio cifra con
# su clave administrada por AWS (aws/rds, aws/secretsmanager, aws/sns, SSE-SQS,
# SSE-S3), sin costo. Con clave propia, los roles de tarea reciben
# kms:Decrypt/GenerateDataKey por IAM y la política de clave solo autoriza a los
# servicios de AWS que cifran en su nombre.
locals {
  kms_key_arn = var.use_customer_managed_key ? aws_kms_key.platform[0].arn : null
}

resource "aws_kms_key" "platform" {
  count                   = var.use_customer_managed_key ? 1 : 0
  description             = "${local.prefix}: secretos, datos, mensajería y logs"
  enable_key_rotation     = true
  deletion_window_in_days = 7
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AccountAdministration"
        Effect    = "Allow"
        Principal = { AWS = "arn:${local.partition}:iam::${local.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
      {
        Sid       = "CloudWatchLogs"
        Effect    = "Allow"
        Principal = { Service = "logs.${var.aws_region}.amazonaws.com" }
        Action    = ["kms:Encrypt", "kms:Decrypt", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"]
        Resource  = "*"
        Condition = {
          ArnLike = { "kms:EncryptionContext:aws:logs:arn" = "arn:${local.partition}:logs:${var.aws_region}:${local.account_id}:log-group:*" }
        }
      },
      {
        # SNS entrega en colas cifradas y CloudWatch publica alarmas en el tópico cifrado.
        Sid       = "MessagingServices"
        Effect    = "Allow"
        Principal = { Service = ["sns.amazonaws.com", "cloudwatch.amazonaws.com", "events.amazonaws.com"] }
        Action    = ["kms:Decrypt", "kms:GenerateDataKey*"]
        Resource  = "*"
        Condition = { StringEquals = { "aws:SourceAccount" = local.account_id } }
      }
    ]
  })
}

resource "aws_kms_alias" "platform" {
  count         = var.use_customer_managed_key ? 1 : 0
  name          = "alias/${local.prefix}"
  target_key_id = aws_kms_key.platform[0].key_id
}

# Credenciales de aliados. Terraform crea un marcador; el valor real se carga
# fuera de Terraform (consola o CLI) y no queda en el estado.
resource "aws_secretsmanager_secret" "ally" {
  for_each                = local.allies
  name                    = "${local.prefix}/aliados/${each.key}"
  description             = each.value.description
  kms_key_id              = local.kms_key_arn
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "ally" {
  for_each      = local.allies
  secret_id     = aws_secretsmanager_secret.ally[each.key].id
  secret_string = jsonencode({ client_id = "CAMBIAR", client_secret = "CAMBIAR" })
  lifecycle {
    ignore_changes = [secret_string]
  }
}
