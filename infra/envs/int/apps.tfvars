# Configuración del ambiente "int" para infra/apps. Los digests de imágenes los
# genera scripts/plataforma/digests.sh en envs/int/images.tfvars.json (ignorado).

task_cpu     = 256
task_memory  = 1024
min_replicas = 1 # 2 = una réplica activa por zona (redundancia activa)
max_replicas = 3

# Todas las tareas en FARGATE_SPOT (~70 % más barato). false para producción.
use_fargate_spot = true

# true = 0 tareas en todos los servicios (sin costo de Fargate) cuando no se use el ambiente.
paused = false

cpu_target = 60

# service_overrides = {
#   cotizacion = { cpu = 512, memory = 1024, max = 5 }
# }

enable_tracing = true

# Sin valor, cada adaptador apunta a simulador-aliados (WireMock).
# ally_endpoints = {
#   open-finance   = "https://api.proveedor-open-finance.example"
#   datos-abiertos = "https://datos.example/api"
#   kyc            = "https://api.proveedor-kyc.example"
# }

ally_timeouts_ms = {
  open-finance   = 800
  datos-abiertos = 800
  kyc            = 3000
}

# Umbrales de alertas (HU-W28). Cada meta abre una advertencia en warning_ratio
# de la meta y una alerta crítica en la meta. Guía: docs/hu-w28-alertas.md
profiling_p95_ms          = 400
warning_ratio             = 0.9
api_5xx_rate              = 0.05
alarm_evaluation_periods  = 5
alarm_datapoints_to_alarm = 3

# Operaciones de otras historias: sin datos, sus alarmas quedan en OK.
latency_targets = [
  { id = "cotizacion", label = "Cotización p95", service = "cotizacion", operation = "cotizacion", target_ms = 250 },
  { id = "emision", label = "Emisión p95", service = "cotizacion", operation = "emision", target_ms = 1500 },
  { id = "consulta", label = "Consulta p95", service = "cotizacion", operation = "consulta", target_ms = 150 },
]

error_rate_percent           = 1
error_rate_warning_percent   = 0.7
partner_quota_percent        = 100
availability_percent         = 99.9
availability_warning_percent = 99.93

# Resumen del mes en Operación › Alertas.
alert_attention_minutes = 30
false_positive_minutes  = 5
