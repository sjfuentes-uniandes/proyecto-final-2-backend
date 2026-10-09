variable "state_bucket" {
  description = "Bucket del estado remoto (lo crea infra/bootstrap). Lo entrega lib.sh como TF_VAR_state_bucket."
  type        = string
}

variable "state_region" {
  type    = string
  default = "us-east-1"
}

variable "environment" {
  description = "Ambiente cuyo estado de platform se lee (<ambiente>/platform.tfstate)."
  type        = string
}

variable "image_digests" {
  description = "Digest publicado por servicio del catálogo. Los servicios ausentes no se despliegan."
  type        = map(string)
  validation {
    condition     = alltrue([for digest in values(var.image_digests) : can(regex("^sha256:[0-9a-f]{64}$", digest))])
    error_message = "Cada digest debe tener el formato sha256: seguido de 64 caracteres hexadecimales."
  }
}

variable "task_cpu" {
  type    = number
  default = 256
}

variable "task_memory" {
  description = "Memoria total de la tarea: aplicación, proxy de Service Connect y colector ADOT."
  type        = number
  default     = 1024
}

variable "min_replicas" {
  description = "1 = mínimo costo. 2 = una réplica activa por zona (redundancia activa, ARQ-001)."
  type        = number
  default     = 1
  validation {
    condition     = var.min_replicas >= 1 && floor(var.min_replicas) == var.min_replicas
    error_message = "Use un entero mayor o igual a 1."
  }
}

variable "paused" {
  description = "true lleva todos los servicios a 0 tareas (sin costo de Fargate) conservando su configuración."
  type        = bool
  default     = false
}

variable "max_replicas" {
  type    = number
  default = 3
}

variable "service_overrides" {
  description = "Ajustes por servicio: cpu, memory, min, max."
  type = map(object({
    cpu    = optional(number)
    memory = optional(number)
    min    = optional(number)
    max    = optional(number)
  }))
  default = {}
}

variable "use_fargate_spot" {
  description = "Ejecuta todas las tareas en FARGATE_SPOT (~70 % más barato, con interrupciones posibles). false = FARGATE."
  type        = bool
  default     = true
}

variable "cpu_target" {
  type    = number
  default = 60
}

variable "queue_backlog_target" {
  description = "Mensajes visibles por cola que disparan escalado de los workers."
  type        = number
  default     = 100
}

variable "health_check_command" {
  description = "Contrato de las imágenes: comando sin shell que termina en 0 si el servicio está sano."
  type        = list(string)
  default     = ["CMD", "/app/healthcheck"]
}

variable "db_pool_size" {
  type    = number
  default = 5
}

variable "enable_tracing" {
  description = "Agrega el colector ADOT (OTLP -> X-Ray y métricas EMF) a cada tarea."
  type        = bool
  default     = true
}

variable "otel_collector_image" {
  type    = string
  default = "public.ecr.aws/aws-observability/aws-otel-collector:v0.43.3"
}

variable "metrics_namespace" {
  type    = string
  default = "Solventa"
}

variable "cors_extra_origins" {
  description = "Orígenes adicionales del portal permitidos por CORS en el BFF web (la URL de CloudFront se agrega sola)."
  type        = list(string)
  default     = ["http://localhost:4200"]
}

variable "ally_endpoints" {
  description = "URL base por aliado. Sin valor, el adaptador apunta a simulador-aliados."
  type        = map(string)
  default     = {}
}

variable "ally_timeouts_ms" {
  description = "Timeout individual por aliado (HU-W05, HU-W06, HU-W12)."
  type        = map(number)
  default = {
    open-finance   = 800
    datos-abiertos = 800
    kyc            = 3000
  }
}

variable "log_retention_days" {
  type    = number
  default = 14
}

# --- Umbrales de alarmas (HU-W28): configurados aquí, no en los servicios -----
variable "profiling_p95_ms" {
  description = "Latencia p95 del perfilamiento que dispara la alerta (HU-W28 AC1)."
  type        = number
  default     = 400
}

variable "api_5xx_rate" {
  description = "Proporción de respuestas 5XX por API (0.05 = 5 %)."
  type        = number
  default     = 0.05
}

variable "api_p95_ms" {
  type    = number
  default = 1500
}

variable "alarm_period_seconds" {
  type    = number
  default = 60
}

variable "alarm_evaluation_periods" {
  type    = number
  default = 5
}

variable "alarm_datapoints_to_alarm" {
  description = "Periodos en incumplimiento, dentro de la ventana, antes de alertar."
  type        = number
  default     = 3
}

variable "cpu_alarm_percent" {
  type    = number
  default = 85
}

variable "memory_alarm_percent" {
  type    = number
  default = 85
}

variable "queue_age_alarm_seconds" {
  type    = number
  default = 300
}
