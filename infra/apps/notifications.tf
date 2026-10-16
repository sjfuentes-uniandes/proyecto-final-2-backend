# HU-W28: correo en español por cada cambio de estado (reemplaza las acciones de
# las alarmas). Decisión en docs/hu-w28-alertas.md.
locals {
  alert_rules = {
    alarma = {
      estado = "ALARMA"
      detail = { state = { value = ["ALARM"] } }
    }
    recuperada = {
      estado = "RECUPERADA"
      detail = { state = { value = ["OK"] }, previousState = { value = ["ALARM"] } }
    }
  }

  # Sin cuenta, ARN ni dimensiones: solo lo que identifica la alerta.
  alert_input_paths = {
    alarma      = "$.detail.alarmName"
    descripcion = "$.detail.configuration.description"
    motivo      = "$.detail.state.reason"
    valor       = "$.detail.state.reasonData"
    hora        = "$.time"
  }

  portal_alerts_url = try("${trimsuffix(data.terraform_remote_state.platform.outputs.web.url, "/")}/operacion/alertas", null)

  alert_templates = {
    for key, rule in local.alert_rules : key => join("\n", compact([
      "\"${rule.estado} - Solventa ${local.p.environment}\"",
      "\"\"",
      "\"Estado: ${rule.estado}\"",
      "\"Ambiente: ${local.p.environment}\"",
      "\"Alarma: <alarma>\"",
      "\"Detalle (severidad, operación y umbral): <descripcion>\"",
      "\"Valor observado: <valor>\"",
      "\"Motivo: <motivo>\"",
      "\"Hora (UTC): <hora>\"",
      "\"Tablero: ${local.dashboard_url}\"",
      local.portal_alerts_url == null ? "" : "\"Alertas abiertas: ${local.portal_alerts_url}\"",
    ]))
  }
}

resource "aws_cloudwatch_event_rule" "alert" {
  for_each    = local.alert_rules
  name        = "${local.prefix}-alertas-${each.key}"
  description = "HU-W28: alarmas de ${local.prefix} que pasan a ${each.value.estado}"
  event_pattern = jsonencode({
    source        = ["aws.cloudwatch"]
    "detail-type" = ["CloudWatch Alarm State Change"]
    detail        = merge({ alarmName = [{ prefix = "${local.prefix}-" }] }, each.value.detail)
  })
}

resource "aws_cloudwatch_event_target" "alert" {
  for_each  = local.alert_rules
  rule      = aws_cloudwatch_event_rule.alert[each.key].name
  target_id = "sns-alertas"
  arn       = local.p.alerts_topic_arn
  input_transformer {
    input_paths    = local.alert_input_paths
    input_template = local.alert_templates[each.key]
  }
}
