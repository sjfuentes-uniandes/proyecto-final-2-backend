variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "name" {
  type    = string
  default = "solventa"
  validation {
    condition     = can(regex("^[a-z][a-z0-9]{2,11}$", var.name))
    error_message = "Use de 3 a 12 caracteres: letras minúsculas y números."
  }
}

variable "environment" {
  description = "Ambiente corto (int, qa, prod); forma parte de los nombres de recursos."
  type        = string
  default     = "int"
  validation {
    condition     = can(regex("^[a-z][a-z0-9]{1,5}$", var.environment))
    error_message = "Use de 2 a 6 caracteres: letras minúsculas y números."
  }
}

variable "vpc_cidr" {
  type    = string
  default = "10.60.0.0/16"
  validation {
    condition     = can(cidrsubnet(var.vpc_cidr, 8, 41)) && can(cidrnetmask(var.vpc_cidr))
    error_message = "Indique una red IPv4 que permita crear subredes /24."
  }
}

variable "high_availability" {
  description = "true: RDS Multi-AZ, un NAT Gateway por zona y endpoints en ambas zonas. false: perfil de mínimo costo."
  type        = bool
  default     = false
}

variable "egress_mode" {
  description = "Salida a Internet de las subredes privadas. nat_instance: una EC2 pequeña (capa gratuita). nat_gateway: servicio administrado (~33 USD/mes por zona)."
  type        = string
  default     = "nat_instance"
  validation {
    condition     = contains(["nat_instance", "nat_gateway"], var.egress_mode)
    error_message = "Use nat_instance o nat_gateway."
  }
}

variable "nat_instance_type" {
  description = "t3.micro es elegible para la capa gratuita de EC2."
  type        = string
  default     = "t3.micro"
}

variable "use_customer_managed_key" {
  description = "true: clave KMS propia (~1 USD/mes) para secretos, datos, mensajería y logs. false: claves administradas por AWS sin costo."
  type        = bool
  default     = false
}

variable "interface_endpoints" {
  description = "Endpoints de interfaz opcionales (sufijo del servicio, p. ej. ecr.api). Cada uno cuesta por hora y por zona; el NAT ya cubre el acceso."
  type        = list(string)
  default     = []
}

variable "db_instance_class" {
  type    = string
  default = "db.t4g.micro"
}

variable "db_allocated_storage" {
  type    = number
  default = 20
}

variable "postgres_version" {
  type    = string
  default = "16"
}

variable "db_backup_retention_days" {
  type    = number
  default = 1
}

variable "db_deletion_protection" {
  type    = bool
  default = false
}

variable "postgres_client_image" {
  description = "Imagen con psql para la tarea de preparación de bases por servicio."
  type        = string
  default     = "public.ecr.aws/docker/library/postgres:16-alpine"
}

variable "container_insights" {
  type    = bool
  default = false
}

variable "log_retention_days" {
  type    = number
  default = 14
}

variable "audit_retention_days" {
  description = "Retención Object Lock de la auditoría de consentimientos e identidad."
  type        = number
  default     = 30
}

variable "audit_lock_mode" {
  description = "GOVERNANCE permite destruir el ambiente con permisos especiales; COMPLIANCE impide borrar antes del vencimiento."
  type        = string
  default     = "GOVERNANCE"
  validation {
    condition     = contains(["GOVERNANCE", "COMPLIANCE"], var.audit_lock_mode)
    error_message = "Use GOVERNANCE o COMPLIANCE."
  }
}

variable "api_stage" {
  type    = string
  default = "v1"
}

variable "api_throttle" {
  description = "Límite por defecto del stage de canales (web y móvil)."
  type        = object({ rate = number, burst = number })
  default     = { rate = 100, burst = 200 }
}

variable "registro_verificaciones_throttle" {
  description = "Tope de la ruta pública de verificación del registro móvil (HU-M06). Cada solicitud consulta al proveedor KYC, así que es menor que el de canales; el WAF limita además por IP."
  type        = object({ rate = number, burst = number })
  default     = { rate = 5, burst = 10 }
  validation {
    condition     = var.registro_verificaciones_throttle.rate >= 1 && var.registro_verificaciones_throttle.burst >= var.registro_verificaciones_throttle.rate
    error_message = "registro_verificaciones_throttle: rate debe ser >= 1 y burst >= rate."
  }
}

variable "partner_tiers" {
  description = "Planes de uso para socios. La cuota y el límite se aplican a cada API key por separado (HU-W02)."
  type = map(object({
    rate         = number
    burst        = number
    quota_limit  = number
    quota_period = string
  }))
  default = {
    basico   = { rate = 5, burst = 10, quota_limit = 1000, quota_period = "DAY" }
    estandar = { rate = 50, burst = 100, quota_limit = 50000, quota_period = "DAY" }
  }
  validation {
    condition     = alltrue([for tier in values(var.partner_tiers) : contains(["DAY", "WEEK", "MONTH"], tier.quota_period)])
    error_message = "quota_period debe ser DAY, WEEK o MONTH."
  }
}

variable "partner_throttle_retry_after_seconds" {
  description = "Retry-After del 429 por límite de solicitudes por segundo (THROTTLED): el balde se repone en segundos."
  type        = number
  default     = 1
  validation {
    condition     = var.partner_throttle_retry_after_seconds >= 1 && var.partner_throttle_retry_after_seconds <= 60
    error_message = "partner_throttle_retry_after_seconds debe estar entre 1 y 60."
  }
}

variable "partner_quota_retry_after_seconds" {
  description = "Retry-After del 429 por cuota agotada (QUOTA_EXCEEDED). La cuota se repone al iniciar el periodo (00:00 UTC en DAY) y API Gateway solo admite un valor fijo: se sugiere reintentar cada hora."
  type        = number
  default     = 3600
  validation {
    condition     = var.partner_quota_retry_after_seconds >= 60 && var.partner_quota_retry_after_seconds <= 86400
    error_message = "partner_quota_retry_after_seconds debe estar entre 60 y 86400 (un día)."
  }
}

variable "partners" {
  description = "Socios creados por Terraform (pruebas N2/N3). En operación, api-socios los crea en tiempo de ejecución."
  type = map(object({
    tier    = string
    scopes  = list(string)
    enabled = optional(bool, true)
  }))
  default = {}
}

variable "partner_scopes" {
  description = "Permisos asignables a socios en el servidor de recursos de Cognito."
  type        = map(string)
  default = {
    cotizar    = "Solicitar y consultar cotizaciones embebidas"
    emitir     = "Aceptar la oferta y emitir la póliza"
    consultar  = "Ver el estado de sus propias pólizas"
    siniestros = "Registrar avisos de siniestro en nombre del cliente"
  }
}

variable "custom_domain" {
  description = "Dominio opcional del API de socios con mTLS. truststore_pem es la ruta al bundle de CAs de los socios."
  type = object({
    domain_name     = string
    certificate_arn = string
    truststore_pem  = string
  })
  default = null
}

variable "waf_rate_limit" {
  description = "Solicitudes por IP en 5 minutos antes de bloquear."
  type        = number
  default     = 2000
}

variable "web_callback_urls" {
  description = "URLs adicionales de retorno OAuth del portal (desarrollo local). La URL de CloudFront se agrega sola."
  type        = list(string)
  default     = ["http://localhost:4200/auth/callback"]
}

variable "mobile_callback_urls" {
  type    = list(string)
  default = ["solventa://auth/callback"]
}

variable "alert_emails" {
  description = "Correos suscritos al tópico de alertas (HU-W28). Cada uno debe confirmar la suscripción."
  type        = list(string)
  default     = []
}
