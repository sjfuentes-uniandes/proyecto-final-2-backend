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

# Recorrido de perfilamiento (DEF-03): con 0,25 vCPU, Fargate limita la CPU en ráfagas y
# las cuatro fuentes en paralelo superan su presupuesto de 300 ms.
service_overrides = {
  simulador-aliados = { cpu = 1024, memory = 2048 }
  adaptador-datos   = { cpu = 512, memory = 1024 }
  cotizacion        = { cpu = 512, memory = 1024 }
}

enable_tracing = true

# Sin valor, cada adaptador apunta a simulador-aliados (WireMock).
# ally_endpoints = {
#   open-finance   = "https://api.proveedor-open-finance.example"
#   datos-abiertos = "https://datos.example/api"
#   kyc            = "https://api.proveedor-kyc.example"
# }

# DEF-01 (Sprint 1): clientes siembra C01–C19 al arrancar para demostrar el perfilamiento.
# La semilla de reglas de W11 AC4 (CATALOGO_SEMILLA_QA) NO va aquí: solo en la rama qa/s1-semilla-reglas.
service_environment = {
  clientes = { CLIENTES_SEMILLA_SINTETICA = "true" }
  # DEF-03: en int cada salto por Service Connect suma 60–100 ms y una fuente pasa por dos
  # (cotizacion → adaptador-datos → simulador). Presupuestos con margen, sin pasar el corte de 700 ms.
  cotizacion = {
    SOLVENTA_FUENTE_TIMEOUT_MS      = "600"
    SOLVENTA_DEPENDENCIA_TIMEOUT_MS = "300"
  }
}

ally_timeouts_ms = {
  open-finance   = 800
  datos-abiertos = 800
  kyc            = 3000
  pagos          = 5000 # HU-W16: recaudo idempotente; el reintento lo hace la cola dinero-aceptaciones
  firma          = 5000 # HU-W17
  push           = 2000 # HU-M11
}

# HU-W05: presupuesto de latencia por operación (LATENCY_BUDGET_MS en cotizacion).
# 700 ms es el corte del recorrido de DEF-03.
latency_budget_ms = {
  cotizacion = 700
}

# Umbrales de alertas (HU-W28). Cada meta abre una advertencia en warning_ratio
# de la meta y una alerta crítica en la meta. Guía: docs/hu-w28-alertas.md
profiling_p95_ms          = 400
warning_ratio             = 0.9
api_5xx_rate              = 0.05
alarm_evaluation_periods  = 5
alarm_datapoints_to_alarm = 3

# Operaciones de otras historias: sin datos, sus alarmas quedan en OK.
# Sprint 2: la emisión es de polizas (HU-W17) y la consulta de la proyección (HU-W18).
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

# Sprint 2 (HU-S02): proveedor degradado (HU-W06), Saga de contratación y avisos push.
# Las operaciones de cada proveedor (provider_operations) están en contracts/operaciones.md.
provider_error_percent         = 20
pending_collection_age_seconds = 900
push_failure_count             = 5

# Resumen del mes en Operación › Alertas.
alert_attention_minutes = 30
false_positive_minutes  = 5
