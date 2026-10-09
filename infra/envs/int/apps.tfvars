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

# Umbrales de alertas (HU-W28).
profiling_p95_ms          = 400
api_5xx_rate              = 0.05
alarm_evaluation_periods  = 5
alarm_datapoints_to_alarm = 3
