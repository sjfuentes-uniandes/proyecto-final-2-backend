# Experimento multizona (HU-W29, ARQ-001). Solo con experiments_enabled (exp): el
# ambiente se crea para el experimento y se destruye al terminar (D4).
#
# - Rol de AWS FIS: interrumpe la red de la zona A (subredes privadas y de datos) y
#   fuerza el failover de RDS al standby de la zona B. Respaldo:
#   diagrama_estrategia_replicacion_recuperacion.puml ("El standby atiende fallas de
#   instancia o zona") y modelo_despliegue.puml ("operación activa-activa entre dos
#   zonas de una región"). La plantilla es experiments/hu-w29/fis.json.
# - Bucket de informes: el informe JSON de reconciliar.py, que bff-web lee en
#   Operación › Conciliación. Propio de exp: ningún bucket de la plataforma se abre a los BFF.
# - Tarea puntual de reconciliación: lee, en solo lectura y con las credenciales de cada
#   servicio, las claves de las operaciones confirmadas. Como db-bootstrap, no se ejecuta
#   sola: ver el output experiment y experiments/hu-w29/extraer.sh.
locals {
  experiment_zone = local.azs[0] # zona A: la que se interrumpe
  # Bases donde queda cada operación de R1 (cotizacion), R3 (polizas y el cobro en dinero)
  # y R5 (adaptador-telemetria, pagos-parametricos y siniestros).
  reconciliation_services = [
    for name in ["cotizacion", "polizas", "dinero", "adaptador-telemetria", "pagos-parametricos", "siniestros"] : name if contains(keys(local.db_names), name)
  ]
  zone_a_subnets = var.experiments_enabled ? [aws_subnet.private[0].arn, aws_subnet.database[0].arn] : []
}

resource "aws_s3_bucket" "experiments" {
  count         = var.experiments_enabled ? 1 : 0
  bucket        = "${local.prefix}-experimentos-${local.account_id}"
  force_destroy = true # se destruye con el ambiente; el informe también queda en experiments/hu-w29/resultados/
}

resource "aws_s3_bucket_server_side_encryption_configuration" "experiments" {
  count  = var.experiments_enabled ? 1 : 0
  bucket = aws_s3_bucket.experiments[0].id
  rule {
    bucket_key_enabled = true
    apply_server_side_encryption_by_default {
      sse_algorithm     = local.audit_sse
      kms_master_key_id = local.kms_key_arn
    }
  }
}

resource "aws_s3_bucket_public_access_block" "experiments" {
  count                   = var.experiments_enabled ? 1 : 0
  bucket                  = aws_s3_bucket.experiments[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "experiments" {
  count  = var.experiments_enabled ? 1 : 0
  bucket = aws_s3_bucket.experiments[0].id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_policy" "experiments" {
  count  = var.experiments_enabled ? 1 : 0
  bucket = aws_s3_bucket.experiments[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource  = [aws_s3_bucket.experiments[0].arn, "${aws_s3_bucket.experiments[0].arn}/*"]
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      },
      {
        Sid       = "DenyUnencryptedWrites"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:PutObject"
        Resource  = "${aws_s3_bucket.experiments[0].arn}/*"
        Condition = { StringNotEquals = { "s3:x-amz-server-side-encryption" = local.audit_sse }, Null = { "s3:x-amz-server-side-encryption" = "false" } }
      }
    ]
  })
  depends_on = [aws_s3_bucket_public_access_block.experiments]
}

# ---------------------------------------------------------------------------
# Rol de AWS FIS (mínimo privilegio). aws:network:disrupt-connectivity reemplaza la
# ACL de red de cada subred por una ACL propia (etiqueta managedByFIS) y restaura la
# original al terminar; aws:rds:reboot-db-instances reinicia con forceFailover.
# Las acciones Describe* y GetManagedPrefixListEntries no admiten ARN de recurso.
# ---------------------------------------------------------------------------
resource "aws_iam_role" "fis" {
  count = var.experiments_enabled ? 1 : 0
  name  = "${local.prefix}-fis"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "fis.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = local.account_id }
        ArnLike      = { "aws:SourceArn" = "arn:${local.partition}:fis:${var.aws_region}:${local.account_id}:experiment/*" }
      }
    }]
  })
}

locals {
  ec2_arn = "arn:${local.partition}:ec2:${var.aws_region}:${local.account_id}"
}

resource "aws_iam_role_policy" "fis" {
  count = var.experiments_enabled ? 1 : 0
  role  = aws_iam_role.fis[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "LeerRed"
        Effect   = "Allow"
        Action   = ["ec2:DescribeSubnets", "ec2:DescribeNetworkAcls", "ec2:DescribeVpcs", "ec2:DescribeManagedPrefixLists", "ec2:GetManagedPrefixListEntries"]
        Resource = ["*"]
      },
      {
        Sid       = "CrearAclDeFalla"
        Effect    = "Allow"
        Action    = ["ec2:CreateNetworkAcl"]
        Resource  = ["${local.ec2_arn}:network-acl/*"]
        Condition = { StringEquals = { "aws:RequestTag/managedByFIS" = "true" } }
      },
      {
        Sid      = "CrearAclEnLaVpc"
        Effect   = "Allow"
        Action   = ["ec2:CreateNetworkAcl"]
        Resource = [aws_vpc.main.arn]
      },
      {
        Sid       = "EtiquetarAclDeFalla"
        Effect    = "Allow"
        Action    = ["ec2:CreateTags"]
        Resource  = ["${local.ec2_arn}:network-acl/*"]
        Condition = { StringEquals = { "ec2:CreateAction" = "CreateNetworkAcl", "aws:RequestTag/managedByFIS" = "true" } }
      },
      {
        Sid       = "AdministrarSoloAclDeFalla"
        Effect    = "Allow"
        Action    = ["ec2:CreateNetworkAclEntry", "ec2:DeleteNetworkAcl"]
        Resource  = ["${local.ec2_arn}:network-acl/*"]
        Condition = { StringEquals = { "ec2:ResourceTag/managedByFIS" = "true" } }
      },
      {
        # Solo las subredes de la zona A; la ACL original se restaura con la misma acción.
        Sid      = "CambiarAclDeLaZonaA"
        Effect   = "Allow"
        Action   = ["ec2:ReplaceNetworkAclAssociation"]
        Resource = concat(local.zone_a_subnets, ["${local.ec2_arn}:network-acl/*"])
      },
      {
        Sid      = "FailoverDeLaBase"
        Effect   = "Allow"
        Action   = ["rds:RebootDBInstance"]
        Resource = [aws_db_instance.main.arn]
      },
      {
        Sid      = "LeerBases"
        Effect   = "Allow"
        Action   = ["rds:DescribeDBInstances"]
        Resource = ["*"]
      }
    ]
  })
}

# ---------------------------------------------------------------------------
# Tarea puntual de reconciliación: psql con default_transaction_read_only y el
# usuario de cada servicio (no el maestro). El comando lo pasa extraer.sh con
# --overrides; sin él, la tarea termina sin consultar nada.
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_log_group" "reconciliation" {
  count             = var.experiments_enabled ? 1 : 0
  name              = "/ecs/${local.prefix}/reconciliacion"
  retention_in_days = var.log_retention_days
  kms_key_id        = local.kms_key_arn
}

resource "aws_iam_role" "reconciliation" {
  count              = var.experiments_enabled ? 1 : 0
  name               = "${local.prefix}-reconciliacion"
  assume_role_policy = local.task_trust
}

resource "aws_iam_role_policy" "reconciliation" {
  count = var.experiments_enabled ? 1 : 0
  role  = aws_iam_role.reconciliation[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = ["${aws_cloudwatch_log_group.reconciliation[0].arn}:*"]
      },
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = [for name in local.reconciliation_services : aws_secretsmanager_secret.service_db[name].arn]
      },
      ], var.use_customer_managed_key ? [{
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = [local.kms_key_arn]
    }] : [])
  })
}

resource "aws_security_group" "reconciliation" {
  count       = var.experiments_enabled ? 1 : 0
  name        = "${local.prefix}-reconciliacion"
  description = "Tarea puntual de reconciliacion del experimento multizona"
  vpc_id      = aws_vpc.main.id
}

resource "aws_vpc_security_group_egress_rule" "reconciliation" {
  count             = var.experiments_enabled ? 1 : 0
  security_group_id = aws_security_group.reconciliation[0].id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

resource "aws_vpc_security_group_ingress_rule" "reconciliation" {
  count                        = var.experiments_enabled ? 1 : 0
  security_group_id            = aws_security_group.database.id
  referenced_security_group_id = aws_security_group.reconciliation[0].id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

resource "aws_ecs_task_definition" "reconciliation" {
  count                    = var.experiments_enabled ? 1 : 0
  family                   = "${local.prefix}-reconciliacion"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "256"
  memory                   = "512"
  execution_role_arn       = aws_iam_role.reconciliation[0].arn
  container_definitions = jsonencode([{
    name      = "reconciliacion"
    image     = var.postgres_client_image
    essential = true
    command   = ["sh", "-c", "echo 'Sin comando: ejecutar experiments/hu-w29/extraer.sh'; exit 64"]
    environment = [
      { name = "PGHOST", value = aws_db_instance.main.address },
      { name = "PGPORT", value = tostring(aws_db_instance.main.port) },
      { name = "PGSSLMODE", value = "require" },
      { name = "PGOPTIONS", value = "-c default_transaction_read_only=on" }
    ]
    secrets = flatten([
      for name in local.reconciliation_services : [
        { name = "USER_${upper(local.db_names[name])}", valueFrom = "${aws_secretsmanager_secret.service_db[name].arn}:username::" },
        { name = "PW_${upper(local.db_names[name])}", valueFrom = "${aws_secretsmanager_secret.service_db[name].arn}:password::" }
      ]
    ])
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.reconciliation[0].name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "reconciliacion"
      }
    }
  }])
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }
  depends_on = [aws_secretsmanager_secret_version.service_db]
}
