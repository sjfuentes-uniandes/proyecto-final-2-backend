#!/usr/bin/env bash
# Humo extendido de HU-S02 (make infra-probar-sprint2). No requiere lógica de negocio:
#   1. Los 6 servicios del Sprint 2: tareas corriendo = deseadas y GET /health sano
#      (healthcheck del contenedor, /app/healthcheck -> GET /health, visto por ECS).
#   2. Enrutamiento: un OfertaAceptada sintético publicado en el tópico de negocio
#      llega a dinero-aceptaciones (y a auditoria, que recibe todo) y a ninguna otra cola.
#   3. DLQ: un mensaje envenenado en DLQ_COLA (avisos por defecto) se recibe sin
#      borrarse hasta agotar maxReceiveCount, SQS lo mueve a la DLQ y su alarma
#      <prefijo>-cola-<cola>-dlq pasa a ALARM. Al final se borra de la DLQ.
# Variables: DLQ_COLA, ESPERA_RUTA_S (60), ESPERA_ALARMA_S (600), SIN_DLQ=1 omite el paso 3.
# Los mensajes sintéticos llevan "sintetico": true; la auditoría (Object Lock) los conserva.
# Sale con 1 si algún chequeo falla.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require terraform aws jq
require_env
export_apps_vars

SPRINT2_SERVICES="polizas dinero adaptador-pagos adaptador-firma consulta avisos"
DLQ_COLA="${DLQ_COLA:-avisos}"
ESPERA_RUTA_S="${ESPERA_RUTA_S:-60}"
ESPERA_ALARMA_S="${ESPERA_ALARMA_S:-600}"

pass=0
fail=0
ok() { printf '  \033[32mOK\033[0m   %s\n' "$*"; pass=$((pass + 1)); }
ko() { printf '  \033[31mFALLA\033[0m %s\n' "$*"; fail=$((fail + 1)); }

tf_init platform
platform="$(platform_output platform)"
cluster=$(jq -r '.cluster.name' <<<"${platform}")
prefix=$(jq -r '.prefix' <<<"${platform}")
topic=$(jq -r '.events_topic_arn' <<<"${platform}")
tf_init apps
deployed="$(terraform -chdir="${INFRA_DIR}/apps" output -json deployed_services 2>/dev/null || echo '{}')"

queue_url() { jq -r --arg q "$1" '.queues[$q].url' <<<"${platform}"; }
dlq_url() { jq -r --arg q "$1" '.queues[$q] as $x | $x.url | sub($x.name + "$"; $x.dlq_name)' <<<"${platform}"; }

# find_marker <url> <marca> <visibilidad>: imprime el ReceiptHandle del mensaje que
# contiene la marca, o nada. Con visibilidad 0 solo mira (no lo oculta al consumidor).
find_marker() {
  local url="$1" marker="$2" visibility="$3" batch handle
  for _ in 1 2 3; do
    batch=$(aws sqs receive-message --queue-url "${url}" --max-number-of-messages 10 \
      --visibility-timeout "${visibility}" --wait-time-seconds 2 --output json 2>/dev/null || echo '{}')
    handle=$(jq -r --arg m "${marker}" '[.Messages[]? | select(.Body | contains($m)) | .ReceiptHandle][0] // empty' <<<"${batch}")
    [ -n "${handle}" ] && { echo "${handle}"; return 0; }
  done
}

log "1. Servicios del Sprint 2 (${ENV})"
for service in ${SPRINT2_SERVICES}; do
  name=$(jq -r --arg s "${service}" '.[$s].service_name // empty' <<<"${deployed}")
  if [ -z "${name}" ]; then
    ko "${service}: no está desplegado (falta su imagen en ECR: make infra-microservicios SERVICES=${service})"
    continue
  fi
  read -r running desired < <(aws ecs describe-services --cluster "${cluster}" --services "${name}" \
    --query 'services[0].[runningCount,desiredCount]' --output text)
  tasks=$(aws ecs list-tasks --cluster "${cluster}" --service-name "${name}" --desired-status RUNNING --query 'taskArns' --output json)
  healthy=0
  if [ "$(jq 'length' <<<"${tasks}")" -gt 0 ]; then
    # SC2016: literal JMESPath. SC2046: un argumento por tarea.
    # shellcheck disable=SC2016,SC2046
    healthy=$(aws ecs describe-tasks --cluster "${cluster}" --tasks $(jq -r '.[]' <<<"${tasks}") \
      --query 'length(tasks[?healthStatus==`HEALTHY`])' --output text)
  fi
  if [ "${desired}" -gt 0 ] && [ "${running}" = "${desired}" ] && [ "${healthy}" = "${desired}" ]; then
    ok "${service}: ${running}/${desired} tareas, ${healthy} con /health sano"
  else
    ko "${service}: ${running}/${desired} tareas, ${healthy} con /health sano"
  fi
done

log "2. Enrutamiento de OfertaAceptada"
marker="humo-sprint2-$(date +%s)-${RANDOM}"
event=$(jq -nc --arg id "${marker}" --arg at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  '{eventId: $id, aggregateId: $id, type: "OfertaAceptada", version: 1, occurredAt: $at, correlationId: $id, sintetico: true, payload: {}}')
aws sns publish --topic-arn "${topic}" --message "${event}" \
  --message-attributes '{"eventType":{"DataType":"String","StringValue":"OfertaAceptada"}}' >/dev/null
echo "  publicado ${marker}"
handle=""
deadline=$((SECONDS + ESPERA_RUTA_S))
while [ -z "${handle}" ] && [ "${SECONDS}" -lt "${deadline}" ]; do
  # Visibilidad 60 s: el consumidor no lo procesa mientras se verifica; luego se borra.
  handle=$(find_marker "$(queue_url dinero-aceptaciones)" "${marker}" 60)
done
if [ -n "${handle}" ]; then
  ok "OfertaAceptada llegó a dinero-aceptaciones"
  aws sqs delete-message --queue-url "$(queue_url dinero-aceptaciones)" --receipt-handle "${handle}"
else
  ko "OfertaAceptada no llegó a dinero-aceptaciones en ${ESPERA_RUTA_S} s"
fi
for queue in $(jq -r '.queues | keys[]' <<<"${platform}"); do
  case "${queue}" in dinero-aceptaciones | auditoria) continue ;; esac
  stray=$(find_marker "$(queue_url "${queue}")" "${marker}" 0)
  if [ -z "${stray}" ]; then
    ok "${queue}: no recibió OfertaAceptada"
  else
    ko "${queue}: recibió OfertaAceptada (revisar su filtro en infra/platform/catalog.tf)"
  fi
done

if [ "${SIN_DLQ:-0}" = "1" ]; then
  log "3. DLQ omitida (SIN_DLQ=1)"
else
  log "3. Mensaje envenenado en ${DLQ_COLA} -> DLQ con alarma"
  url="$(queue_url "${DLQ_COLA}")"
  [ "${url}" != "null" ] || die "La cola ${DLQ_COLA} no existe en el catálogo."
  max=$(aws sqs get-queue-attributes --queue-url "${url}" --attribute-names RedrivePolicy \
    --query 'Attributes.RedrivePolicy' --output text | jq -r '.maxReceiveCount')
  poison="veneno-${marker}"
  aws sqs send-message --queue-url "${url}" --message-body "${poison} no-es-json" \
    --message-attributes '{"eventType":{"DataType":"String","StringValue":"Envenenado"}}' >/dev/null
  # Cada recepción sin borrar suma una; la siguiente a maxReceiveCount lo mueve a la DLQ.
  received=0
  deadline=$((SECONDS + 120))
  while [ "${received}" -lt "${max}" ] && [ "${SECONDS}" -lt "${deadline}" ]; do
    [ -n "$(find_marker "${url}" "${poison}" 0)" ] && received=$((received + 1))
  done
  dlq_handle=""
  deadline=$((SECONDS + 60))
  while [ -z "${dlq_handle}" ] && [ "${SECONDS}" -lt "${deadline}" ]; do
    # La recepción que supera maxReceiveCount es la que lo mueve a la DLQ.
    find_marker "${url}" "${poison}" 0 >/dev/null || true
    dlq_handle=$(find_marker "$(dlq_url "${DLQ_COLA}")" "${poison}" 0)
  done
  if [ -n "${dlq_handle}" ]; then
    ok "tras ${received} recepciones (maxReceiveCount ${max}) el mensaje quedó en la DLQ de ${DLQ_COLA}"
    alarm="${prefix}-cola-${DLQ_COLA}-dlq"
    state=""
    deadline=$((SECONDS + ESPERA_ALARMA_S))
    while [ "${state}" != "ALARM" ] && [ "${SECONDS}" -lt "${deadline}" ]; do
      sleep 30
      state=$(aws cloudwatch describe-alarms --alarm-names "${alarm}" --query 'MetricAlarms[0].StateValue' --output text)
    done
    if [ "${state}" = "ALARM" ]; then ok "${alarm} en ALARM"; else ko "${alarm} no pasó a ALARM en ${ESPERA_ALARMA_S} s (estado ${state})"; fi
    handle=$(find_marker "$(dlq_url "${DLQ_COLA}")" "${poison}" 30)
    [ -n "${handle}" ] && aws sqs delete-message --queue-url "$(dlq_url "${DLQ_COLA}")" --receipt-handle "${handle}"
    echo "  mensaje envenenado borrado de la DLQ; la alarma vuelve a OK en unos minutos"
  else
    ko "el mensaje envenenado no llegó a la DLQ de ${DLQ_COLA} (¿un consumidor lo borró?)"
  fi
fi

log "Resultado: ${pass} OK, ${fail} con falla"
[ "${fail}" -eq 0 ]
