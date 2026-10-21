# Configuración del ambiente "int" para infra/platform (versionada: sin secretos).
# El nombre del ambiente sale de la carpeta; environment debe coincidir con ella.
aws_region  = "us-east-1"
name        = "solventa"
environment = "int"
vpc_cidr    = "10.60.0.0/16"

# Perfil por defecto orientado a la capa gratuita: RDS db.t4g.micro Single-AZ,
# ALB, NAT instance t3.micro, WAF mínimo y claves administradas por AWS. Las
# tareas ECS se reparten igualmente entre las zonas A y B.
high_availability        = false          # true = RDS Multi-AZ (y NAT Gateway por zona si egress_mode = "nat_gateway")
egress_mode              = "nat_instance" # "nat_gateway" = administrado, ~33 USD/mes por zona
nat_instance_type        = "t3.micro"
use_customer_managed_key = false # true = clave KMS propia (~1 USD/mes)

# Opcional: endpoints de interfaz (cada uno ~7 USD/mes por zona). El endpoint
# gateway de S3 siempre se crea y es gratuito.
# interface_endpoints = ["ecr.api", "ecr.dkr", "logs", "secretsmanager", "sqs", "sns", "xray", "kms"]

db_instance_class = "db.t3.micro"
postgres_version  = "16"

audit_retention_days = 30
audit_lock_mode      = "GOVERNANCE"

# Documentos de póliza (HU-W17): en int se pueden destruir con objetos.
policy_documents_force_destroy   = true
policy_documents_noncurrent_days = 90

# Caché Valkey de catalogo, cotizacion y consulta. false = sin ElastiCache (lecturas a RDS).
cache_enabled   = true
cache_node_type = "cache.t3.micro" # capa gratuita de ElastiCache (750 h/mes)

# WAF siempre activo (~7 USD/mes: Web ACL + 2 reglas). Solicitudes por IP en 5 min.
waf_rate_limit = 2000

# "prueba" es una cuota pequeña y determinista para las pruebas N2/N3 de HU-W02:
# la sexta solicitud del día responde 429. tests/e2e/test_hu_w02_cuotas.py repone
# la cuota de los socios de prueba al empezar y al terminar.
partner_tiers = {
  basico   = { rate = 5, burst = 10, quota_limit = 1000, quota_period = "DAY" }
  estandar = { rate = 50, burst = 100, quota_limit = 50000, quota_period = "DAY" }
  prueba   = { rate = 1, burst = 2, quota_limit = 5, quota_period = "DAY" }
}

# Dos socios de prueba: make infra-probar usa socio-a, y ambos sirven para las
# pruebas N3 de HU-W01 y HU-W02. Para desactivar uno, poner enabled = false y
# aplicar. Credenciales: terraform -chdir=infra/platform output -json partners
partners = {
  socio-a = { tier = "prueba", scopes = ["cotizar", "consultar"] }
  socio-b = { tier = "prueba", scopes = ["cotizar"] }
}

# Dominio propio con mTLS para socios (requiere certificado ACM en la región).
# custom_domain = {
#   domain_name     = "socios.ejemplo.co"
#   certificate_arn = "arn:aws:acm:us-east-1:123456789012:certificate/..."
#   truststore_pem  = "/ruta/absoluta/truststore.pem"
# }

alert_emails = ["sj.fuentes@uniandes.edu.co"]
