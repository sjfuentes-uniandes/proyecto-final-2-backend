#!/bin/bash
# Recursos locales de mensajería y almacenamiento (LocalStack). LocalStack lo ejecuta una vez, al
# quedar listo (/etc/localstack/init/ready.d/); es idempotente, así que repetirlo no rompe nada.
#
# Archivo compartido (plan del Sprint 3, §1.8): cada HU agrega sus colas al final. Los nombres y los
# filtros son los de infra/platform/catalog.tf con el prefijo del ambiente `solventa-local`. Las
# pruebas de módulo no dependen de este script: crean sus recursos con Testcontainers.
set -euo pipefail

REGION="${AWS_DEFAULT_REGION:-us-east-1}"
PREFIJO="solventa-local"
MAX_RECEPCIONES=5

# --- Tópico de eventos de negocio (messaging.tf: <prefijo>-eventos-negocio) ---
TOPICO_ARN="$(awslocal sns create-topic --name "${PREFIJO}-eventos-negocio" --region "$REGION" \
  --query TopicArn --output text)"

# cola <nombre> <visibilidad_s> [filtro_json]
#   Crea <prefijo>-<nombre> con su DLQ (maxReceiveCount = 5) y la suscribe al tópico con entrega
#   cruda. Sin filtro recibe todos los eventos (como `auditoria`).
cola() {
  local nombre="$1" visibilidad="$2" filtro="${3:-}"
  local dlq_url dlq_arn url arn
  dlq_url="$(awslocal sqs create-queue --queue-name "${PREFIJO}-${nombre}-dlq" --region "$REGION" \
    --query QueueUrl --output text)"
  dlq_arn="$(awslocal sqs get-queue-attributes --queue-url "$dlq_url" --attribute-names QueueArn \
    --region "$REGION" --query Attributes.QueueArn --output text)"
  url="$(awslocal sqs create-queue --queue-name "${PREFIJO}-${nombre}" --region "$REGION" \
    --attributes "{\"VisibilityTimeout\":\"${visibilidad}\",\"RedrivePolicy\":\"{\\\"deadLetterTargetArn\\\":\\\"${dlq_arn}\\\",\\\"maxReceiveCount\\\":\\\"${MAX_RECEPCIONES}\\\"}\"}" \
    --query QueueUrl --output text)"
  arn="$(awslocal sqs get-queue-attributes --queue-url "$url" --attribute-names QueueArn \
    --region "$REGION" --query Attributes.QueueArn --output text)"
  # Atributos en JSON (la forma abreviada no admite comas dentro del filtro).
  local atributos='{"RawMessageDelivery":"true"}'
  if [[ -n "$filtro" ]]; then
    atributos="{\"RawMessageDelivery\":\"true\",\"FilterPolicy\":\"${filtro//\"/\\\"}\"}"
  fi
  awslocal sns subscribe --topic-arn "$TOPICO_ARN" --protocol sqs --notification-endpoint "$arn" \
    --attributes "$atributos" --region "$REGION" >/dev/null
  echo "cola ${PREFIJO}-${nombre} lista"
}

# --- HU-W19: bucket de evidencias de siniestros (solo `siniestros` lo usa) ---
awslocal s3api head-bucket --bucket "${PREFIJO}-evidencias" 2>/dev/null \
  || awslocal s3api create-bucket --bucket "${PREFIJO}-evidencias" --region "$REGION" >/dev/null
echo "bucket ${PREFIJO}-evidencias listo"

# --- HU-W19: auditoría de accesos (sin filtro: recibe SiniestroConsultado, EvidenciaConsultada y
# AccesoSiniestroDenegado) y resultado de la indemnización (W23) ---
cola "auditoria" 60
cola "siniestros-indemnizaciones" 30 '{"eventType":[{"prefix":"Indemnizacion"}]}'
