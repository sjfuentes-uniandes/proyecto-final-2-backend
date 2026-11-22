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
  description = "Ajustes por servicio: cpu, memory, min, max; para la política de CPU (HU-W08), cpu_target (%), scale_out_cooldown y scale_in_cooldown (s); para la de cola (HU-W25), queue_backlog_target, queue_scale_out_cooldown y queue_scale_in_cooldown (s), y queue_age_target (s; sin valor, no hay política por antigüedad). Un campo sin valor toma el global del ambiente."
  type = map(object({
    cpu                = optional(number)
    memory             = optional(number)
    min                = optional(number)
    max                = optional(number)
    cpu_target         = optional(number)
    scale_out_cooldown = optional(number)
    scale_in_cooldown  = optional(number)
    # Política backlog y por antigüedad (HU-W25).
    queue_backlog_target     = optional(number)
    queue_age_target         = optional(number)
    queue_scale_out_cooldown = optional(number)
    queue_scale_in_cooldown  = optional(number)
  }))
  default = {}
  validation {
    condition = alltrue([
      for override in values(var.service_overrides) :
      (override.cpu_target == null ? true : override.cpu_target >= 1 && override.cpu_target <= 100) &&
      (override.scale_out_cooldown == null ? true : override.scale_out_cooldown >= 0 && floor(override.scale_out_cooldown) == override.scale_out_cooldown) &&
      (override.scale_in_cooldown == null ? true : override.scale_in_cooldown >= 0 && floor(override.scale_in_cooldown) == override.scale_in_cooldown)
    ])
    error_message = "En service_overrides, cpu_target va de 1 a 100 y los enfriamientos son enteros mayores o iguales a 0."
  }
  validation {
    condition = alltrue([
      for override in values(var.service_overrides) :
      (override.queue_backlog_target == null ? true : override.queue_backlog_target > 0) &&
      (override.queue_age_target == null ? true : override.queue_age_target >= 1 && floor(override.queue_age_target) == override.queue_age_target) &&
      (override.queue_scale_out_cooldown == null ? true : override.queue_scale_out_cooldown >= 0 && floor(override.queue_scale_out_cooldown) == override.queue_scale_out_cooldown) &&
      (override.queue_scale_in_cooldown == null ? true : override.queue_scale_in_cooldown >= 0 && floor(override.queue_scale_in_cooldown) == override.queue_scale_in_cooldown)
    ])
    error_message = "En service_overrides, queue_backlog_target es mayor que 0, queue_age_target es un entero mayor o igual a 1 y los enfriamientos de cola son enteros mayores o iguales a 0."
  }
}

variable "use_fargate_spot" {
  description = "Ejecuta todas las tareas en FARGATE_SPOT (~70 % más barato, con interrupciones posibles). false = FARGATE."
  type        = bool
  default     = true
}

variable "cpu_target" {
  description = "Objetivo de CPU promedio (%) de la política de autoescalado de los servicios que no son workers. service_overrides lo ajusta por servicio."
  type        = number
  default     = 60
  validation {
    condition     = var.cpu_target >= 1 && var.cpu_target <= 100
    error_message = "Use un porcentaje entre 1 y 100."
  }
}

variable "scale_out_cooldown_seconds" {
  description = "Enfriamiento (s) tras escalar hacia fuera por CPU, antes de volver a escalar hacia fuera. service_overrides lo ajusta por servicio."
  type        = number
  default     = 30
  validation {
    condition     = var.scale_out_cooldown_seconds >= 0 && floor(var.scale_out_cooldown_seconds) == var.scale_out_cooldown_seconds
    error_message = "Use un entero mayor o igual a 0."
  }
}

variable "scale_in_cooldown_seconds" {
  description = "Enfriamiento (s) tras escalar hacia dentro por CPU, antes de volver a retirar tareas. service_overrides lo ajusta por servicio."
  type        = number
  default     = 300
  validation {
    condition     = var.scale_in_cooldown_seconds >= 0 && floor(var.scale_in_cooldown_seconds) == var.scale_in_cooldown_seconds
    error_message = "Use un entero mayor o igual a 0."
  }
}

variable "queue_backlog_target" {
  description = "Objetivo de la política backlog de los workers: mensajes visibles por tarea con queue_backlog_per_task; si no, la suma de las colas del servicio. service_overrides lo ajusta por servicio."
  type        = number
  default     = 100
  validation {
    condition     = var.queue_backlog_target > 0
    error_message = "Use un número mayor que 0."
  }
}

variable "queue_backlog_per_task" {
  description = "HU-W25: divide los mensajes visibles de la política backlog por las tareas en ejecución (ECS/ContainerInsights RunningTaskCount). Requiere Container Insights en platform. false = la suma de las colas, como antes."
  type        = bool
  default     = false
}

variable "queue_scale_out_cooldown_seconds" {
  description = "Enfriamiento (s) tras escalar hacia fuera por cola (backlog y antigüedad), antes de volver a escalar hacia fuera. service_overrides lo ajusta por servicio."
  type        = number
  default     = 60
  validation {
    condition     = var.queue_scale_out_cooldown_seconds >= 0 && floor(var.queue_scale_out_cooldown_seconds) == var.queue_scale_out_cooldown_seconds
    error_message = "Use un entero mayor o igual a 0."
  }
}

variable "queue_scale_in_cooldown_seconds" {
  description = "Enfriamiento (s) tras escalar hacia dentro por cola (backlog), antes de volver a retirar tareas. service_overrides lo ajusta por servicio."
  type        = number
  default     = 300
  validation {
    condition     = var.queue_scale_in_cooldown_seconds >= 0 && floor(var.queue_scale_in_cooldown_seconds) == var.queue_scale_in_cooldown_seconds
    error_message = "Use un entero mayor o igual a 0."
  }
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
  description = "Timeout individual por aliado (HU-W05, HU-W06, HU-W12, HU-W16, HU-W17, HU-M11)."
  type        = map(number)
  default = {
    open-finance   = 800
    datos-abiertos = 800
    kyc            = 3000
    pagos          = 5000
    firma          = 5000
    push           = 2000
  }
}

variable "latency_budget_ms" {
  description = "Presupuesto de latencia por operación (ms). Llega en JSON como LATENCY_BUDGET_MS a los servicios de latency_budget_readers (HU-W05): cotizacion (deadline del recorrido), socio.cotizar (api-socios) y portal.cotizar (bff-web). Cada timeout de dependencia debe ser menor que su presupuesto o el servicio no arranca."
  type        = map(number)
  default = {
    cotizacion       = 700
    "socio.cotizar"  = 1600
    "portal.cotizar" = 1600
  }
  validation {
    condition     = contains(keys(var.latency_budget_ms), "cotizacion") && alltrue([for ms in values(var.latency_budget_ms) : ms > 0])
    error_message = "Incluya al menos la operación cotizacion, con presupuestos mayores que 0 ms."
  }
}

variable "service_environment" {
  description = "Variables de entorno adicionales por servicio (sin secretos). Se aplican al final y tienen prioridad; uso temporal de QA, p. ej. la semilla de clientes sintéticos (DEF-01) o la de reglas (D-04)."
  type        = map(map(string))
  default     = {}
}

variable "log_retention_days" {
  type    = number
  default = 14
}

# --- Umbrales de alarmas (HU-W28): configurados aquí, no en los servicios -----
variable "profiling_p95_ms" {
  description = "Meta p95 del perfilamiento: alerta crítica (HU-W28 AC1). Con warning_alarms, la advertencia es warning_ratio de la meta."
  type        = number
  default     = 400
}

variable "warning_alarms" {
  description = "true = además de la alarma crítica de cada meta (p95, tasa de error, disponibilidad y cuota de socio) crea su advertencia en warning_ratio. false (por omisión) deja solo la crítica: los AC de HU-W28 piden la meta y cada advertencia duplica las métricas de alarma que se cobran."
  type        = bool
  default     = false
}

variable "warning_ratio" {
  description = "Fracción de la meta que abre la alerta de advertencia (0.9 = 90 %). Solo con warning_alarms = true."
  type        = number
  default     = 0.9
  validation {
    condition     = var.warning_ratio > 0 && var.warning_ratio < 1
    error_message = "warning_ratio debe estar entre 0 y 1."
  }
}

variable "latency_targets" {
  description = "Metas p95 (ms) de otras operaciones prioritarias. Sin datos de la operación, sus alarmas quedan en OK."
  type = list(object({
    id         = string
    label      = string
    service    = string
    operation  = string
    target_ms  = number
    warning_ms = optional(number)
  }))
  default = []
  validation {
    condition     = alltrue([for t in var.latency_targets : can(regex("^[a-z][a-z0-9-]*$", t.id)) && t.id != "perfilamiento"])
    error_message = "Cada id es único, en minúsculas con guiones, y distinto de perfilamiento (definido por profiling_p95_ms)."
  }
}

variable "error_rate_percent" {
  description = "Meta de la tasa de error técnico por servicio: OperationErrors / SampleCount(OperationDuration), en %."
  type        = number
  default     = 1
}

variable "error_rate_warning_percent" {
  description = "Advertencia de la tasa de error (%). Sin valor: warning_ratio de la meta."
  type        = number
  default     = null
}

variable "availability_percent" {
  description = "Meta de disponibilidad por API (1 - 5XX/solicitudes), en %."
  type        = number
  default     = 99.9
}

variable "availability_warning_percent" {
  description = "Advertencia de disponibilidad (%). Sin valor: se consume warning_ratio del margen de error."
  type        = number
  default     = null
}

variable "availability_period_seconds" {
  description = "Ventana en la que se calcula la disponibilidad."
  type        = number
  default     = 3600
}

variable "partner_quota_percent" {
  description = "Consumo de la cuota de un socio (PartnerQuotaUsage) que abre la alerta crítica, en %."
  type        = number
  default     = 100
}

variable "partner_quota_interval_seconds" {
  description = "Cada cuánto api-socios publica PartnerQuotaUsage (0 = no publica)."
  type        = number
  default     = 300
  validation {
    condition     = var.partner_quota_interval_seconds >= 0 && var.partner_quota_interval_seconds % 60 == 0
    error_message = "partner_quota_interval_seconds debe ser 0 o múltiplo de 60 (periodo de Metrics Insights)."
  }
}

variable "alert_attention_minutes" {
  description = "Una alerta crítica que dura más que esto cuenta como meta incumplida en el resumen del mes."
  type        = number
  default     = 30
}

variable "false_positive_minutes" {
  description = "Una alerta que se recupera antes de esto cuenta como falso positivo en el resumen del mes."
  type        = number
  default     = 5
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

# --- Sprint 2 (HU-S02): proveedores, Saga de contratación y avisos --------------
variable "provider_operations" {
  description = "Operaciones (contracts/operaciones.md) que miden la llamada a cada proveedor. La alarma de degradación usa OperationErrors / SampleCount(OperationDuration) de todas ellas (HU-W06)."
  type = map(object({
    service    = string
    operations = list(string)
  }))
  default = {
    open-finance   = { service = "adaptador-datos", operations = ["consulta.FA_PRODUCTOS_VIGENTES", "consulta.FA_HISTORIAL_PAGOS_12M", "consulta.FA_INGRESOS_AGREGADOS"] }
    datos-abiertos = { service = "adaptador-datos", operations = ["consulta.DA_REGISTROS_PUBLICOS"] }
    kyc            = { service = "adaptador-identidad", operations = ["verificacion.kyc"] }
    pagos          = { service = "adaptador-pagos", operations = ["cobro.pagos"] }
    firma          = { service = "adaptador-firma", operations = ["firma.documento"] }
    push           = { service = "adaptador-firma", operations = ["notificacion.push"] }
  }
  validation {
    condition     = alltrue([for provider in values(var.provider_operations) : length(provider.operations) >= 1 && length(provider.operations) <= 5])
    error_message = "Cada proveedor tiene entre 1 y 5 operaciones (una alarma admite hasta 10 métricas)."
  }
}

variable "provider_error_percent" {
  description = "Proporción de llamadas fallidas a un proveedor (%) que lo marca como degradado."
  type        = number
  default     = 20
}

variable "pending_collection_age_seconds" {
  description = "Antigüedad máxima (s) de una aceptación sin recaudar o de un recaudo sin emitir (dinero-aceptaciones, polizas-recaudos)."
  type        = number
  default     = 900
}

variable "push_failure_count" {
  description = "Envíos push fallidos de avisos (operación aviso.push) en 5 minutos que abren la alerta."
  type        = number
  default     = 5
}

# --- Experimento de carga (HU-W09): tarea k6 de carga.tf, apagada fuera de exp ---------
variable "load_test_enabled" {
  description = "true crea la tarea k6 del experimento de carga (carga.tf): definición, rol de ejecución, log group, SG y la entrada al ALB en el puerto de api-socios. Solo se enciende en envs/exp/apps.tfvars."
  type        = bool
  default     = false
}

variable "load_test_image" {
  description = "Imagen pública de k6 con versión fija (nunca latest) y el digest de su variante linux/amd64. Se descarga por el NAT, sin permisos de ECR."
  type        = string
  default     = "grafana/k6:1.8.2@sha256:cbdf083a01e5245b2fdb3e4bb0cbcea5d52519b9aa66abd1f63b3f0249d18af9"
  validation {
    condition     = can(regex(":v?[0-9]+\\.[0-9]+\\.[0-9]+(@sha256:[0-9a-f]{64})?$", var.load_test_image))
    error_message = "Use una etiqueta de versión fija (por ejemplo, grafana/k6:1.8.2), opcionalmente con su digest; latest no se admite."
  }
}

variable "load_test_cpu" {
  description = "CPU de la tarea k6 (unidades de Fargate). Holgada para que k6 no sea el cuello de botella (HU-W09, decisión 15)."
  type        = number
  default     = 2048
  validation {
    condition     = contains([1024, 2048, 4096], var.load_test_cpu)
    error_message = "Use 1024, 2048 o 4096."
  }
}

variable "load_test_memory" {
  description = "Memoria de la tarea k6 (MiB). Fargate admite de 2 a 8 veces la CPU, en múltiplos de 1024."
  type        = number
  default     = 4096
  validation {
    condition     = var.load_test_memory % 1024 == 0 && var.load_test_memory >= 2 * var.load_test_cpu && var.load_test_memory <= 8 * var.load_test_cpu
    error_message = "La memoria es múltiplo de 1024 y está entre 2 y 8 veces load_test_cpu."
  }
}

variable "load_test_log_retention_days" {
  description = "Retención (días) del log de k6. Corta: los resultados se copian del log del job de E2E (N3) al recogerlos."
  type        = number
  default     = 7
  validation {
    condition     = contains([1, 3, 5, 7, 14], var.load_test_log_retention_days)
    error_message = "Use una retención corta admitida por CloudWatch Logs: 1, 3, 5, 7 o 14 días."
  }
}
