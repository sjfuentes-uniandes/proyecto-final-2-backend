# Configuración del ambiente "exp" para infra/apps (HU-S03). Los digests de imágenes los
# genera scripts/plataforma/digests.sh en envs/exp/images.tfvars.json (ignorado).

task_cpu     = 256
task_memory  = 1024
min_replicas = 2 # una réplica activa por zona (redundancia activa, HU-W29)
max_replicas = 6

# Sin Spot: una interrupción de AWS no debe confundirse con la falla inyectada del experimento.
use_fargate_spot = false

paused = false

cpu_target = 60

# Como en int (DEF-03), más capacidad para los servicios del recorrido paramétrico (HU-W26).
service_overrides = {
  simulador-aliados    = { cpu = 1024, memory = 2048 }
  adaptador-datos      = { cpu = 512, memory = 1024 }
  cotizacion           = { cpu = 512, memory = 1024 }
  adaptador-telemetria = { cpu = 512, memory = 1024 }
  pagos-parametricos   = { cpu = 512, memory = 1024, max = 10 }
  siniestros           = { cpu = 512, memory = 1024 }
}

enable_tracing = true

service_environment = {
  clientes = { CLIENTES_SEMILLA_SINTETICA = "true" }
  catalogo = { CATALOGO_PRODUCTOS_QA = "true" }
  consulta = { CONSULTA_SEMILLA_POLIZAS = "true" }
  cotizacion = {
    SOLVENTA_FUENTE_TIMEOUT_MS      = "600"
    SOLVENTA_DEPENDENCIA_TIMEOUT_MS = "300"
  }
}

ally_timeouts_ms = {
  open-finance   = 800
  datos-abiertos = 800
  kyc            = 3000
  pagos          = 5000
  firma          = 5000
  push           = 2000
}

latency_budget_ms = {
  cotizacion       = 700
  "socio.cotizar"  = 1600
  "portal.cotizar" = 1600
}

# Mismos umbrales que int (HU-W28): el experimento mide contra las mismas metas.
profiling_p95_ms = 400
# Solo la alarma crítica de cada meta; true agrega la advertencia en warning_ratio
# (el doble de métricas de alarma; útil para una demostración).
warning_alarms            = false
warning_ratio             = 0.9
api_5xx_rate              = 0.05
alarm_evaluation_periods  = 5
alarm_datapoints_to_alarm = 3

latency_targets = [
  { id = "cotizacion", label = "Cotización p95", service = "cotizacion", operation = "cotizacion", target_ms = 250 },
  { id = "emision", label = "Emisión p95", service = "polizas", operation = "emision", target_ms = 1500 },
  { id = "consulta", label = "Consulta p95", service = "consulta", operation = "consulta", target_ms = 150 },
]

error_rate_percent           = 1
error_rate_warning_percent   = 0.7
partner_quota_percent        = 100
availability_percent         = 99.9
availability_warning_percent = 99.93

provider_error_percent         = 20
pending_collection_age_seconds = 900
push_failure_count             = 5

alert_attention_minutes = 30
false_positive_minutes  = 5
