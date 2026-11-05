# Configuración del ambiente "exp" (experimentos del Sprint 3, HU-S03) para infra/platform.
# Versionada y sin secretos. Se crea para un experimento y se destruye al terminar:
#   make infra-desplegar ENV=exp   ...   make infra-destruir ENV=exp
# Comparte la cuenta con int pero no sus recursos (prefijo solventa-exp-, VPC y estado propios).
aws_region  = "us-east-1"
name        = "solventa"
environment = "exp"
vpc_cidr    = "10.61.0.0/16"

# Dos zonas reales (D4, HU-W29): true = RDS Multi-AZ y un NAT Gateway por zona. Por ahora una
# sola zona activa para la base y la salida; las tareas ECS se reparten igual entre A y B.
# Cambiar a true antes del experimento de disponibilidad (AC4 de HU-S03).
high_availability        = false
egress_mode              = "nat_gateway" # administrado (1 con high_availability = false, 2 con true)
use_customer_managed_key = false

# Base de mayor capacidad que int (db.t3.micro) para la carga de HU-W26.
db_instance_class      = "db.t4g.small"
db_allocated_storage   = 20
postgres_version       = "16"
db_deletion_protection = false # el ambiente se destruye sin pasos manuales

# Ambiente efímero: auditoría en GOVERNANCE con retención corta y buckets que se destruyen con objetos.
audit_retention_days           = 1
audit_lock_mode                = "GOVERNANCE"
policy_documents_force_destroy = true
evidence_force_destroy         = true

cache_enabled   = true
cache_node_type = "cache.t3.micro"

# Límites amplios: la carga sale de pocas IP (generador de carga y simulador de telemetría).
api_throttle                = { rate = 1000, burst = 2000 }
telemetria_eventos_throttle = { rate = 500, burst = 1000 }
waf_rate_limit              = 50000
waf_telemetria_rate_limit   = 200000

# Plan "experimento" para las pruebas de carga del canal de socios.
partner_tiers = {
  basico      = { rate = 5, burst = 10, quota_limit = 1000, quota_period = "DAY" }
  estandar    = { rate = 50, burst = 100, quota_limit = 50000, quota_period = "DAY" }
  experimento = { rate = 500, burst = 1000, quota_limit = 5000000, quota_period = "DAY" }
}

partners = {
  socio-exp = { tier = "experimento", scopes = ["cotizar", "consultar"] }
}

# El rol de logs de API Gateway es un ajuste único de la cuenta. Con int apagado, exp lo
# administra (true). Si int está desplegado al mismo tiempo, poner false: exp usa el de int y
# destruir exp no lo afecta (infra/DESPLIEGUE.md, "Ambiente exp").
manage_apigateway_account = true

alert_emails = ["sj.fuentes@uniandes.edu.co"]
