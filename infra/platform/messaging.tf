# Transactional Outbox + SNS/SQS fan-out. Cada servicio guarda el evento en su
# tabla outbox dentro de la transacción del agregado y su relay lo publica aquí
# con el atributo de mensaje `eventType`. Cada consumidor tiene su cola, su DLQ
# y su propio ritmo; la entrega es al menos una vez, por lo que los
# consumidores aplican Inbox/idempotencia por eventId.
resource "aws_sns_topic" "business_events" {
  name              = "${local.prefix}-eventos-negocio"
  kms_master_key_id = coalesce(local.kms_key_arn, "alias/aws/sns")
}

resource "aws_sqs_queue" "dlq" {
  for_each                  = local.queues
  name                      = "${local.prefix}-${each.key}-dlq"
  kms_master_key_id         = local.kms_key_arn
  sqs_managed_sse_enabled   = var.use_customer_managed_key ? null : true
  message_retention_seconds = 1209600
}

resource "aws_sqs_queue" "main" {
  for_each                   = local.queues
  name                       = "${local.prefix}-${each.key}"
  kms_master_key_id          = local.kms_key_arn
  sqs_managed_sse_enabled    = var.use_customer_managed_key ? null : true
  visibility_timeout_seconds = each.value.visibility_timeout
  receive_wait_time_seconds  = 20
  message_retention_seconds  = 345600
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq[each.key].arn
    maxReceiveCount     = each.value.max_receive_count
  })
}

resource "aws_sqs_queue_redrive_allow_policy" "dlq" {
  for_each  = local.queues
  queue_url = aws_sqs_queue.dlq[each.key].id
  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.main[each.key].arn]
  })
}

resource "aws_sqs_queue_policy" "main" {
  for_each  = local.queues
  queue_url = aws_sqs_queue.main[each.key].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "sns.amazonaws.com" }
      Action    = "sqs:SendMessage"
      Resource  = aws_sqs_queue.main[each.key].arn
      Condition = { ArnEquals = { "aws:SourceArn" = aws_sns_topic.business_events.arn } }
    }]
  })
}

resource "aws_sns_topic_subscription" "queue" {
  for_each             = local.queues
  topic_arn            = aws_sns_topic.business_events.arn
  protocol             = "sqs"
  endpoint             = aws_sqs_queue.main[each.key].arn
  raw_message_delivery = true
  filter_policy        = each.value.filter == null ? null : jsonencode(each.value.filter)
  depends_on           = [aws_sqs_queue_policy.main]
}
