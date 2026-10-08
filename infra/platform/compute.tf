resource "aws_ecs_cluster" "main" {
  name = local.prefix
  setting {
    name  = "containerInsights"
    value = var.container_insights ? "enabled" : "disabled"
  }
}

resource "aws_ecs_cluster_capacity_providers" "main" {
  cluster_name       = aws_ecs_cluster.main.name
  capacity_providers = ["FARGATE", "FARGATE_SPOT"]
  default_capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 1
    base              = 1
  }
}

# Descubrimiento ECS a ECS por nombre (Service Connect).
resource "aws_service_discovery_http_namespace" "main" {
  name        = local.prefix
  description = "Service Connect de ${local.prefix}"
}

# ---------------------------------------------------------------------------
# Tarea puntual que crea, de forma idempotente, un usuario y una base por
# servicio con las contraseñas guardadas en Secrets Manager. No se ejecuta sola:
# ver el output db_bootstrap_task.
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_log_group" "db_bootstrap" {
  name              = "/ecs/${local.prefix}/db-bootstrap"
  retention_in_days = var.log_retention_days
  kms_key_id        = local.kms_key_arn
}

locals {
  task_trust = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = local.account_id } }
    }]
  })

  db_bootstrap_script = <<-EOT
    set -eu
    export PGHOST="$DB_HOST" PGPORT="$DB_PORT" PGUSER="$MASTER_USER" PGPASSWORD="$MASTER_PASSWORD" PGSSLMODE=require PGDATABASE=postgres
    %{for service, db in local.db_names~}
    psql -v ON_ERROR_STOP=1 -v role=${db} -v pw="$PW_${upper(db)}" <<'SQL'
    SELECT format('CREATE ROLE %I LOGIN', :'role') WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = :'role')\gexec
    ALTER ROLE :"role" WITH LOGIN PASSWORD :'pw';
    GRANT :"role" TO CURRENT_USER;
    SELECT format('CREATE DATABASE %I OWNER %I', :'role', :'role') WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = :'role')\gexec
    REVOKE ALL ON DATABASE :"role" FROM PUBLIC;
    SQL
    echo "base lista: ${db}"
    %{endfor~}
  EOT
}

resource "aws_iam_role" "db_bootstrap" {
  name               = "${local.prefix}-db-bootstrap"
  assume_role_policy = local.task_trust
}

resource "aws_iam_role_policy" "db_bootstrap" {
  role = aws_iam_role.db_bootstrap.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = ["${aws_cloudwatch_log_group.db_bootstrap.arn}:*"]
      },
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = concat([aws_db_instance.main.master_user_secret[0].secret_arn], [for secret in aws_secretsmanager_secret.service_db : secret.arn])
      },
      ], var.use_customer_managed_key ? [{
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = [local.kms_key_arn]
    }] : [])
  })
}

resource "aws_security_group" "db_bootstrap" {
  name        = "${local.prefix}-db-bootstrap"
  description = "Tarea puntual de preparación de bases"
  vpc_id      = aws_vpc.main.id
}

resource "aws_vpc_security_group_egress_rule" "db_bootstrap" {
  security_group_id = aws_security_group.db_bootstrap.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

resource "aws_vpc_security_group_ingress_rule" "db_bootstrap" {
  security_group_id            = aws_security_group.database.id
  referenced_security_group_id = aws_security_group.db_bootstrap.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

resource "aws_ecs_task_definition" "db_bootstrap" {
  family                   = "${local.prefix}-db-bootstrap"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "256"
  memory                   = "512"
  execution_role_arn       = aws_iam_role.db_bootstrap.arn
  container_definitions = jsonencode([{
    name      = "db-bootstrap"
    image     = var.postgres_client_image
    essential = true
    command   = ["sh", "-c", local.db_bootstrap_script]
    environment = [
      { name = "DB_HOST", value = aws_db_instance.main.address },
      { name = "DB_PORT", value = tostring(aws_db_instance.main.port) }
    ]
    secrets = concat(
      [
        { name = "MASTER_USER", valueFrom = "${aws_db_instance.main.master_user_secret[0].secret_arn}:username::" },
        { name = "MASTER_PASSWORD", valueFrom = "${aws_db_instance.main.master_user_secret[0].secret_arn}:password::" }
      ],
      [for service, db in local.db_names : { name = "PW_${upper(db)}", valueFrom = "${aws_secretsmanager_secret.service_db[service].arn}:password::" }]
    )
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.db_bootstrap.name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "bootstrap"
      }
    }
  }])
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }
  depends_on = [aws_secretsmanager_secret_version.service_db]
}
