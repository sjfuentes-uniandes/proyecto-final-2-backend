# Canal de alertas del equipo (HU-W28). Las alarmas de apps envían ALARM y OK a
# este tópico, de modo que también se notifica la recuperación.
resource "aws_sns_topic" "alerts" {
  name              = "${local.prefix}-alertas"
  kms_master_key_id = local.kms_key_arn # null: las alarmas no pueden usar aws/sns
}

resource "aws_sns_topic_policy" "alerts" {
  arn = aws_sns_topic.alerts.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "CloudWatchAlarms"
      Effect    = "Allow"
      Principal = { Service = "cloudwatch.amazonaws.com" }
      Action    = "sns:Publish"
      Resource  = aws_sns_topic.alerts.arn
      Condition = { StringEquals = { "aws:SourceAccount" = local.account_id } }
    }]
  })
}

resource "aws_sns_topic_subscription" "alert_email" {
  for_each  = toset(var.alert_emails)
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = each.value
}

# Métricas EMF emitidas por el colector ADOT de cada tarea (HU-W27).
resource "aws_cloudwatch_log_group" "metrics" {
  name              = "/ecs/${local.prefix}/metrics"
  retention_in_days = var.log_retention_days
  kms_key_id        = local.kms_key_arn
}

# Reemplazos de tareas y despliegues para la continuidad multizona.
resource "aws_cloudwatch_log_group" "ecs_events" {
  name              = "/ecs/${local.prefix}/events"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_event_rule" "ecs" {
  name = "${local.prefix}-ecs-events"
  event_pattern = jsonencode({
    source        = ["aws.ecs"]
    "detail-type" = ["ECS Task State Change", "ECS Service Action", "ECS Deployment State Change"]
    detail        = { clusterArn = [aws_ecs_cluster.main.arn] }
  })
}

resource "aws_cloudwatch_log_resource_policy" "ecs_events" {
  policy_name = "${local.prefix}-ecs-events"
  policy_document = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = ["events.amazonaws.com", "delivery.logs.amazonaws.com"] }
      Action    = ["logs:CreateLogStream", "logs:PutLogEvents"]
      Resource  = "${aws_cloudwatch_log_group.ecs_events.arn}:*"
      Condition = { ArnEquals = { "aws:SourceArn" = aws_cloudwatch_event_rule.ecs.arn } }
    }]
  })
}

resource "aws_cloudwatch_event_target" "ecs_events" {
  rule       = aws_cloudwatch_event_rule.ecs.name
  target_id  = "cloudwatch-logs"
  arn        = aws_cloudwatch_log_group.ecs_events.arn
  depends_on = [aws_cloudwatch_log_resource_policy.ecs_events]
}

# Limitaciones por socio (HU-W02 AC2): cada 429 del API de socios se cuenta con
# la API key del socio como dimensión, sin exponer datos de otros consumidores.
resource "aws_cloudwatch_log_metric_filter" "partner_throttled" {
  name           = "${local.prefix}-partner-throttled"
  log_group_name = aws_cloudwatch_log_group.api["socios"].name
  pattern        = "{ $.status = 429 }"
  metric_transformation {
    name      = "PartnerThrottled"
    namespace = "Solventa/Socios"
    value     = "1"
    unit      = "Count"
    dimensions = {
      ApiKeyId = "$.apiKeyId"
    }
  }
}

resource "aws_cloudwatch_log_metric_filter" "partner_rejected" {
  name           = "${local.prefix}-partner-rejected"
  log_group_name = aws_cloudwatch_log_group.api["socios"].name
  pattern        = "{ ($.status = 401) || ($.status = 403) }"
  metric_transformation {
    name      = "PartnerRejected"
    namespace = "Solventa/Socios"
    value     = "1"
    unit      = "Count"
  }
}
