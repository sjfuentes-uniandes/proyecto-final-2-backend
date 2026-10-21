# Caché de catálogo, reglas vigentes y consultas (Cache-Aside) para los servicios
# con `cache = true` en el catálogo. Un nodo Valkey cache.t3.micro sin réplica:
# entra en la capa gratuita (750 h/mes) y cuesta ~10 USD/mes sin ella, frente a
# ~6 USD/mes mínimos de Valkey Serverless, que además exige clientes con cluster
# mode. Es reemplazable: si el nodo cae, los servicios leen de RDS.
#
# Privada: subredes de base de datos (sin ruta a Internet), cifrado en tránsito
# (TLS obligatorio) y en reposo, y un grupo de seguridad sin reglas de entrada
# aquí; apps solo admite a las tareas de los servicios con `cache = true`.
# Sin auth token: no hay secreto que guardar en el estado; el acceso lo limita la red.
resource "aws_security_group" "cache" {
  count       = var.cache_enabled ? 1 : 0
  name        = "${local.prefix}-cache"
  description = "ElastiCache; las reglas de entrada por servicio se crean en apps"
  vpc_id      = aws_vpc.main.id
}

resource "aws_elasticache_subnet_group" "cache" {
  count      = var.cache_enabled ? 1 : 0
  name       = "${local.prefix}-cache"
  subnet_ids = aws_subnet.database[*].id
}

resource "aws_elasticache_replication_group" "cache" {
  count                      = var.cache_enabled ? 1 : 0
  replication_group_id       = "${local.prefix}-cache"
  description                = "Cache-Aside de ${join(", ", sort(tolist(local.cache_services)))}"
  engine                     = "valkey"
  engine_version             = var.cache_engine_version
  parameter_group_name       = "default.valkey${split(".", var.cache_engine_version)[0]}"
  node_type                  = var.cache_node_type
  num_cache_clusters         = 1
  port                       = 6379
  subnet_group_name          = aws_elasticache_subnet_group.cache[0].name
  security_group_ids         = [aws_security_group.cache[0].id]
  at_rest_encryption_enabled = "true"
  kms_key_id                 = local.kms_key_arn
  transit_encryption_enabled = true
  transit_encryption_mode    = "required"
  automatic_failover_enabled = false
  multi_az_enabled           = false
  snapshot_retention_limit   = 0
  auto_minor_version_upgrade = true
  apply_immediately          = true
}

locals {
  cache_url = var.cache_enabled ? "rediss://${aws_elasticache_replication_group.cache[0].primary_endpoint_address}:6379" : ""
}
