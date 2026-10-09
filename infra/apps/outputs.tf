output "deployed_services" {
  description = "Servicios con imagen publicada y su capa de despliegue (Service Connect)."
  value = {
    for name, service in local.ecs_services : name => {
      service_name    = service.service_name
      task_definition = service.task_definition_arn
      layer           = local.depth[name]
      replicas        = { min = local.sizing[name].min, max = max(local.sizing[name].min, local.sizing[name].max) }
    }
  }
}

output "cluster_name" {
  value = local.p.cluster.name
}

output "pending_services" {
  description = "Servicios del catálogo sin imagen publicada todavía."
  value       = [for name in keys(local.p.catalog) : name if !contains(keys(local.deployed), name)]
}

output "api_urls" {
  value = { for name, api in local.p.apis : name => api.url }
}

output "dashboard_url" {
  value = local.dashboard_url
}

output "log_groups" {
  value = merge(
    { for name, group in aws_cloudwatch_log_group.service : name => group.name },
    { service_connect = aws_cloudwatch_log_group.proxy.name, metrics = local.p.metrics_log_group }
  )
}

output "metric_contract" {
  description = "Contrato de métricas de aplicación usado por alarmas y tablero."
  value = {
    namespace  = var.metrics_namespace
    dimensions = ["Environment", "Service", "Operation"]
    metrics    = { OperationDuration = "Milliseconds (histograma exponencial)", OperationErrors = "Count" }
    profiling  = { service = "cotizacion", operation = "perfilamiento", p95_threshold_ms = var.profiling_p95_ms }
  }
}
