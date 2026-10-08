# Base de datos por servicio: una instancia PostgreSQL (Multi-AZ con
# high_availability) y, dentro, una base y un usuario por servicio. Cada servicio
# es el único escritor de su modelo; ningún usuario accede a la base de otro.
resource "aws_db_subnet_group" "main" {
  name       = local.prefix
  subnet_ids = aws_subnet.database[*].id
}

resource "aws_db_parameter_group" "main" {
  name_prefix = "${local.prefix}-"
  family      = "postgres${split(".", var.postgres_version)[0]}"
  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }
  parameter {
    name  = "log_min_duration_statement"
    value = "500"
  }
  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_db_instance" "main" {
  identifier     = local.prefix
  engine         = "postgres"
  engine_version = var.postgres_version
  instance_class = var.db_instance_class
  # 20 GB y sin autoescalado de almacenamiento: dentro de la capa gratuita de RDS.
  allocated_storage               = var.db_allocated_storage
  storage_type                    = "gp3"
  storage_encrypted               = true
  kms_key_id                      = local.kms_key_arn
  username                        = "solventa_admin"
  manage_master_user_password     = true
  master_user_secret_kms_key_id   = local.kms_key_arn
  db_subnet_group_name            = aws_db_subnet_group.main.name
  parameter_group_name            = aws_db_parameter_group.main.name
  vpc_security_group_ids          = [aws_security_group.database.id]
  multi_az                        = var.high_availability
  publicly_accessible             = false
  auto_minor_version_upgrade      = true
  backup_retention_period         = var.db_backup_retention_days
  copy_tags_to_snapshot           = true
  deletion_protection             = var.db_deletion_protection
  skip_final_snapshot             = !var.db_deletion_protection
  final_snapshot_identifier       = var.db_deletion_protection ? "${local.prefix}-final" : null
  enabled_cloudwatch_logs_exports = ["postgresql"]
}

locals {
  # Los identificadores de PostgreSQL no admiten guiones sin comillas.
  db_names = { for name in local.database_services : name => replace(name, "-", "_") }
}

resource "random_password" "service" {
  for_each = local.database_services
  length   = 32
  special  = false
}

resource "aws_secretsmanager_secret" "service_db" {
  for_each                = local.database_services
  name                    = "${local.prefix}/db/${each.key}"
  description             = "Credenciales de ${each.key} para su base privada"
  kms_key_id              = local.kms_key_arn
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "service_db" {
  for_each  = local.database_services
  secret_id = aws_secretsmanager_secret.service_db[each.key].id
  secret_string = jsonencode({
    engine   = "postgres"
    host     = aws_db_instance.main.address
    port     = aws_db_instance.main.port
    dbname   = local.db_names[each.key]
    username = local.db_names[each.key]
    password = random_password.service[each.key].result
  })
}
