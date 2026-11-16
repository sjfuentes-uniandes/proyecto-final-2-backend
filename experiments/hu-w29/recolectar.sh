#!/usr/bin/env bash
# Recolecta la evidencia del experimento en resultados/ (AC2 y AC3):
#   DESDE=... HASTA=... ./recolectar.sh
# - dlq.ndjson: mensajes de cada DLQ del ambiente (se leen con visibilidad de 5 min; no se borran);
# - alarmas.json: transiciones de las alarmas del ambiente en la ventana (HU-W28);
# - cronologia.ndjson: eventos de ECS, RDS y FIS de la ventana (infra/platform/observability.tf);
# - fis-experimento.json: estado y tiempos de cada acción, si existe resultados/experimento.txt.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require terraform aws jq
require_env
: "${DESDE:?Indique DESDE (ISO 8601 UTC)}"
: "${HASTA:?Indique HASTA (ISO 8601 UTC)}"
tf_init platform

exp="$(experimento)"
prefijo="${NAME}-${ENV}"
ms() { echo $(($(date -u -j -f %Y-%m-%dT%H:%M:%SZ "$1" +%s 2>/dev/null || date -u -d "$1" +%s) * 1000)); }

log "DLQ"
: >"${RESULTADOS}/dlq.ndjson"
for dlq in $(platform_output platform | jq -r '.queues[].dlq_name'); do
  url=$(aws sqs get-queue-url --queue-name "${dlq}" --query QueueUrl --output text)
  vistos=0
  while :; do
    lote=$(aws sqs receive-message --queue-url "${url}" --max-number-of-messages 10 --visibility-timeout 300 \
      --attribute-names All --wait-time-seconds 1 --output json)
    n=$(jq '.Messages // [] | length' <<<"${lote}")
    [ "${n}" -gt 0 ] || break
    jq -c --arg cola "${dlq}" '.Messages[] | {cola: $cola, messageId: .MessageId, cuerpo: .Body,
      enviado: .Attributes.SentTimestamp, recibido: .Attributes.ApproximateReceiveCount}' <<<"${lote}" >>"${RESULTADOS}/dlq.ndjson"
    vistos=$((vistos + n))
  done
  printf '  %-45s %s mensajes\n' "${dlq}" "${vistos}"
done

log "Alarmas (HU-W28)"
aws cloudwatch describe-alarm-history --history-item-type StateUpdate --start-date "${DESDE}" --end-date "${HASTA}" \
  --max-records 100 --output json |
  jq --arg p "${prefijo}-" '[.AlarmHistoryItems[] | select(.AlarmName | startswith($p))
    | {alarma: .AlarmName, instante: .Timestamp, resumen: .HistorySummary,
       de: (.HistoryData | fromjson | .oldState.stateValue), a: (.HistoryData | fromjson | .newState.stateValue)}]
    | sort_by(.instante)' >"${RESULTADOS}/alarmas.json"
echo "  $(jq length "${RESULTADOS}/alarmas.json") transiciones"

log "Cronología (ECS, RDS y FIS)"
aws logs filter-log-events --log-group-name "$(jq -r .timeline_log_group <<<"${exp}")" \
  --start-time "$(ms "${DESDE}")" --end-time "$(ms "${HASTA}")" --output json |
  jq -c '.events[] | .message | fromjson' >"${RESULTADOS}/cronologia.ndjson"
echo "  $(wc -l <"${RESULTADOS}/cronologia.ndjson" | tr -d ' ') eventos"

if [ -f "${RESULTADOS}/experimento.txt" ]; then
  log "Experimento FIS"
  aws fis get-experiment --id "$(cat "${RESULTADOS}/experimento.txt")" --output json >"${RESULTADOS}/fis-experimento.json"
  jq -r '.experiment | "  estado: \(.state.status) · inicio \(.startTime) · fin \(.endTime // "-")"' "${RESULTADOS}/fis-experimento.json"
fi
