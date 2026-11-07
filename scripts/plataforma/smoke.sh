#!/usr/bin/env bash
# Prueba rápida del ambiente desplegado (no requiere lógica de negocio: usa
# GET /health de los servicios). Verifica de punta a punta:
#   1. Servicios ECS desplegados con todas sus tareas corriendo.
#   2. Destinos saludables en el ALB interno (servicios de acceso).
#   3. API de canales: sin token o con token inválido -> 401, con X-Correlation-Id.
#   4. API de socios con socio de prueba (client_credentials + API key):
#      token válido -> 200 desde api-socios; sin API key -> rechazo en el borde.
#   5. Opcional (SMOKE_ALERTA=1): mensaje de prueba al tópico de alertas.
#   Sprint 2 (HU-S02):
#   6. polizas, dinero, adaptador-pagos, adaptador-firma, consulta y avisos con
#      todas sus tareas HEALTHY (el healthcheck del contenedor llama a GET /health).
#      Los que no tienen imagen publicada se omiten.
#   7. Un OfertaAceptada sintético publicado en el tópico de negocio llega a
#      dinero-aceptaciones (y a auditoria, que recibe todo) y a ninguna otra cola.
#   8. Un mensaje envenenado en SMOKE_DLQ_COLA (avisos) se recibe sin borrarse hasta
#      agotar maxReceiveCount, SQS lo mueve a la DLQ y su alarma pasa a ALARM.
#      Tarda ~6-10 min; SMOKE_DLQ=0 lo omite.
#   HU-W03:
#   9. Caché de catálogo: el grupo de replicación de Valkey está "available" y la
#      definición de tarea de catalogo y cotizacion lleva CACHE_URL (catalogo la llena y
#      la invalida; cotizacion solo la lee). Con cache_enabled = false se omite.
#      La prima de punta a punta es N3: make test-e2e (tests/e2e/test_hu_w03_r1_prima.py).
#   Sprint 3 (HU-S03):
#  10. siniestros, pagos-parametricos y adaptador-telemetria con sus tareas HEALTHY.
#  11. Un SiniestroAprobado sintético llega solo a dinero-indemnizaciones,
#      consulta-proyeccion y auditoria.
#  12. POST /telemetria/v1/eventos llega a adaptador-telemetria sin JWT (el borde no
#      responde 401/403; la firma HMAC la valida el adaptador, D5).
#   HU-W24 (humo R5; SMOKE_W24=0 lo omite):
#  13. SMOKE_W24_EVENTOS (5) EventoParametricoObservado sintéticos en el tópico: auditoria los
#      escribe en objetos de lote cadena/*.json del bucket de auditoría (cada uno con su
#      eventId una sola vez y el previousHash del anterior) y la cola auditoria queda vacía.
#      Los eventos también llegan a parametricos-eventos; sin el consumidor de HU-W21 se quedan
#      ahí y vencen a los 4 días (llevan "sintetico": true).
#   El mensaje envenenado del paso 8 se prueba en una cola nueva con
#   SMOKE_DLQ_COLA=dinero-indemnizaciones.
# Variables: SMOKE_DLQ_COLA, SMOKE_ESPERA_RUTA_S (60), SMOKE_ESPERA_ALARMA_S (600).
# Los mensajes sintéticos llevan "sintetico": true; la auditoría (Object Lock) los conserva.
# Sale con 1 si algún chequeo falla.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require terraform aws jq curl
require_env
export_apps_vars

pass=0
fail=0
ok() { printf '  \033[32mOK\033[0m   %s\n' "$*"; pass=$((pass + 1)); }
ko() { printf '  \033[31mFALLA\033[0m %s\n' "$*"; fail=$((fail + 1)); }
skip() { printf '  \033[33mOMITE\033[0m %s\n' "$*"; }
# check <éxito> <mensaje si pasa> <mensaje si falla>; éxito: "true" o "false".
check() { if [ "$1" = "true" ]; then ok "$2"; else ko "$3"; fi; }
# is <valor> <regex>: "true" si el valor coincide con la expresión.
is() { if [[ "$1" =~ $2 ]]; then echo true; else echo false; fi; }

# http_code <args de curl...>: imprime el código HTTP (000 si no hubo respuesta).
http_code() { curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$@" || true; }

SPRINT2_SERVICES="polizas dinero adaptador-pagos adaptador-firma consulta avisos"
SPRINT3_SERVICES="siniestros pagos-parametricos adaptador-telemetria"
SMOKE_DLQ_COLA="${SMOKE_DLQ_COLA:-avisos}"
SMOKE_ESPERA_RUTA_S="${SMOKE_ESPERA_RUTA_S:-60}"
SMOKE_ESPERA_ALARMA_S="${SMOKE_ESPERA_ALARMA_S:-600}"

tf_init platform
platform="$(platform_output platform)"
prefix=$(jq -r '.prefix' <<<"${platform}")
api_urls="$(platform_output api_urls)"
canales=$(jq -r '.canales' <<<"${api_urls}")
socios=$(jq -r '.socios_mtls // .socios' <<<"${api_urls}")

tf_init apps
deployed="$(terraform -chdir="${INFRA_DIR}/apps" output -json deployed_services 2>/dev/null || echo '{}')"
cluster=$(jq -r '.cluster.name' <<<"${platform}")

queue_url() { jq -r --arg q "$1" '.queues[$q].url // empty' <<<"${platform}"; }
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
  # Sin marca no es error: con set -e, `handle=$(find_marker ...)` abortaría el script.
  return 0
}

log "1. Servicios ECS (${ENV})"
if [ "$(jq 'length' <<<"${deployed}")" = "0" ]; then
  skip "No hay servicios desplegados (make infra-aplicaciones)."
else
  names=()
  while IFS= read -r name; do names+=("${name}"); done < <(jq -r '.[].service_name' <<<"${deployed}")
  for ((i = 0; i < ${#names[@]}; i += 10)); do
    while read -r name running desired; do
      if [ "${running}" = "${desired}" ] && [ "${desired}" -gt 0 ]; then
        ok "${name}: ${running}/${desired} tareas"
      else
        ko "${name}: ${running}/${desired} tareas"
      fi
    done < <(aws ecs describe-services --cluster "${cluster}" --services "${names[@]:i:10}" \
      --query 'services[].[serviceName,runningCount,desiredCount]' --output text)
  done
fi

log "2. Destinos del ALB interno"
for service in $(jq -r --argjson d "${deployed}" '.target_groups | keys[] | select(. as $k | $d | has($k))' <<<"${platform}"); do
  target_group=$(jq -r --arg s "${service}" '.target_groups[$s].arn' <<<"${platform}")
  # shellcheck disable=SC2016 # comillas invertidas: literal JMESPath, no expansión.
  healthy=$(aws elbv2 describe-target-health --target-group-arn "${target_group}" \
    --query 'length(TargetHealthDescriptions[?TargetHealth.State==`healthy`])' --output text)
  check "$(is "${healthy}" '^[1-9][0-9]*$')" "${service}: ${healthy} destino(s) saludable(s)" "${service}: sin destinos saludables"
done

log "3. API de canales (${canales})"
headers_file=$(mktemp)
trap 'rm -f "${headers_file}"' EXIT
code=$(http_code -D "${headers_file}" "${canales}/web/health")
headers=$(tr -d '\r' <"${headers_file}")
check "$(is "${code}" '^401$')" "sin token -> 401" "sin token -> ${code:-sin respuesta} (esperado 401)"
check "$(is "$(tr '[:upper:]' '[:lower:]' <<<"${headers}")" 'x-correlation-id:')" "la respuesta del borde incluye X-Correlation-Id" "falta X-Correlation-Id en la respuesta del borde"
code=$(http_code -H "Authorization: Bearer token-invalido" "${canales}/movil/health")
check "$(is "${code}" '^401$')" "token inválido -> 401" "token inválido -> ${code} (esperado 401)"

log "4. API de socios (${socios})"
partner=$(terraform -chdir="${INFRA_DIR}/platform" output -json partners 2>/dev/null |
  jq -c 'to_entries | map(select(.value.enabled and .value.client_id != null)) | first // empty')
if [ -z "${partner}" ]; then
  skip "No hay socios de prueba habilitados (partners en infra/envs/${ENV}/platform.tfvars)."
else
  name=$(jq -r '.key' <<<"${partner}")
  token_url=$(platform_output cognito | jq -r '.partners.token_url')
  token=$(curl -s --max-time 20 -u "$(jq -r '.value.client_id' <<<"${partner}"):$(jq -r '.value.client_secret' <<<"${partner}")" \
    -d grant_type=client_credentials "${token_url}" | jq -r '.access_token // empty')
  if [ -z "${token}" ]; then
    ko "${name}: no se obtuvo token client_credentials de Cognito"
  else
    ok "${name}: token client_credentials obtenido"
    api_key=$(jq -r '.value.api_key' <<<"${partner}")
    code=$(http_code -H "Authorization: Bearer ${token}" "${socios}/health")
    check "$(is "${code}" '^40[13]$')" "token sin API key -> ${code} (rechazado en el borde)" "token sin API key -> ${code} (esperado 401/403)"
    if jq -e 'has("api-socios")' <<<"${deployed}" >/dev/null; then
      code=$(http_code -H "Authorization: Bearer ${token}" -H "x-api-key: ${api_key}" "${socios}/health")
      check "$(is "${code}" '^200$')" "token + API key -> 200 desde api-socios (WAF, VPC Link, ALB y ECS)" \
        "token + API key -> ${code} (esperado 200)"
    else
      skip "api-socios no está desplegado; no se prueba el recorrido completo."
    fi
  fi
fi

if [ "${SMOKE_ALERTA:-0}" = "1" ]; then
  log "5. Canal de alertas"
  topic=$(jq -r '.alerts_topic_arn' <<<"${platform}")
  if aws sns publish --topic-arn "${topic}" --subject "[${ENV}] Prueba del canal de alertas" \
    --message "Mensaje de prueba de make infra-probar. No requiere acción." >/dev/null; then
    ok "mensaje publicado en el tópico de alertas (verificar el correo)"
  else
    ko "no se pudo publicar en el tópico de alertas"
  fi
fi

# check_health <servicios...>: tareas RUNNING con el healthcheck del contenedor (GET /health) sano.
check_health() {
  local service name running desired tasks healthy
  for service in "$@"; do
  name=$(jq -r --arg s "${service}" '.[$s].service_name // empty' <<<"${deployed}")
  if [ -z "${name}" ]; then
    skip "${service}: sin imagen publicada (make infra-microservicios SERVICES=${service})."
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
    check "$([ "${desired}" -gt 0 ] && [ "${running}" = "${desired}" ] && [ "${healthy}" = "${desired}" ] && echo true || echo false)" \
      "${service}: ${running}/${desired} tareas con /health sano" "${service}: ${running}/${desired} tareas, ${healthy} con /health sano"
  done
}

# check_route <eventType> <colas esperadas...>: publica un evento sintético en el tópico de
# negocio y verifica que llega a las colas esperadas (salvo auditoria, que recibe todo y la
# consume el worker) y a ninguna otra. Las copias de las colas esperadas se borran.
check_route() {
  local type="$1" marker event queue handle deadline expected
  shift
  expected=" $* auditoria "
  for queue in "$@"; do
    [ -n "$(queue_url "${queue}")" ] || { skip "La cola ${queue} no existe (make infra-plataforma)."; return 0; }
  done
  marker="humo-$(date +%s)-${RANDOM}"
  # HU-W24: eventId uuid (auditoria descarta como envenenado un sobre sin él); la marca va en
  # correlationId, que es lo que busca find_marker en el cuerpo.
  event=$(jq -nc --arg id "${marker}" --arg uuid "$(uuidgen | tr '[:upper:]' '[:lower:]')" --arg type "${type}" \
    --arg at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    '{eventId: $uuid, aggregateId: $uuid, type: $type, version: 1, occurredAt: $at, correlationId: $id, sintetico: true, payload: {}}')
  aws sns publish --topic-arn "$(jq -r '.events_topic_arn' <<<"${platform}")" --message "${event}" \
    --message-attributes "$(jq -nc --arg t "${type}" '{eventType: {DataType: "String", StringValue: $t}}')" >/dev/null
  for queue in "$@"; do
    handle=""
    deadline=$((SECONDS + SMOKE_ESPERA_RUTA_S))
    while [ -z "${handle}" ] && [ "${SECONDS}" -lt "${deadline}" ]; do
      # Visibilidad 60 s: el consumidor no lo procesa mientras se verifica; luego se borra.
      handle=$(find_marker "$(queue_url "${queue}")" "${marker}" 60)
    done
    if [ -n "${handle}" ]; then
      ok "${type} llegó a ${queue}"
      aws sqs delete-message --queue-url "$(queue_url "${queue}")" --receipt-handle "${handle}"
    else
      ko "${type} no llegó a ${queue} en ${SMOKE_ESPERA_RUTA_S} s"
    fi
  done
  for queue in $(jq -r '.queues | keys[]' <<<"${platform}"); do
    case "${expected}" in *" ${queue} "*) continue ;; esac
    check "$([ -z "$(find_marker "$(queue_url "${queue}")" "${marker}" 0)" ] && echo true || echo false)" \
      "${queue}: no recibió ${type}" "${queue}: recibió ${type} (revisar su filtro en infra/platform/catalog.tf)"
  done
}

log "6. Servicios del Sprint 2: GET /health"
# shellcheck disable=SC2086 # lista separada por espacios.
check_health ${SPRINT2_SERVICES}

log "7. Enrutamiento de OfertaAceptada"
check_route OfertaAceptada dinero-aceptaciones

log "8. Mensaje envenenado en ${SMOKE_DLQ_COLA} -> DLQ con alarma"
url="$(queue_url "${SMOKE_DLQ_COLA}")"
if [ "${SMOKE_DLQ:-1}" = "0" ]; then
  skip "SMOKE_DLQ=0."
elif [ -z "${url}" ]; then
  skip "La cola ${SMOKE_DLQ_COLA} no existe (make infra-plataforma)."
else
  max=$(aws sqs get-queue-attributes --queue-url "${url}" --attribute-names RedrivePolicy \
    --query 'Attributes.RedrivePolicy' --output text | jq -r '.maxReceiveCount')
  poison="veneno-$(date +%s)-${RANDOM}"
  aws sqs send-message --queue-url "${url}" --message-body "${poison} no-es-json" \
    --message-attributes '{"eventType":{"DataType":"String","StringValue":"Envenenado"}}' >/dev/null
  # Cada recepción sin borrar suma una; la que supera maxReceiveCount lo mueve a la DLQ.
  received=0
  deadline=$((SECONDS + 120))
  while [ "${received}" -lt "${max}" ] && [ "${SECONDS}" -lt "${deadline}" ]; do
    [ -n "$(find_marker "${url}" "${poison}" 0)" ] && received=$((received + 1))
  done
  dlq_handle=""
  deadline=$((SECONDS + 60))
  while [ -z "${dlq_handle}" ] && [ "${SECONDS}" -lt "${deadline}" ]; do
    find_marker "${url}" "${poison}" 0 >/dev/null || true
    dlq_handle=$(find_marker "$(dlq_url "${SMOKE_DLQ_COLA}")" "${poison}" 0)
  done
  if [ -n "${dlq_handle}" ]; then
    ok "tras ${received} recepciones (maxReceiveCount ${max}) el mensaje quedó en la DLQ de ${SMOKE_DLQ_COLA}"
    alarm="${prefix}-cola-${SMOKE_DLQ_COLA}-dlq"
    state=""
    deadline=$((SECONDS + SMOKE_ESPERA_ALARMA_S))
    while [ "${state}" != "ALARM" ] && [ "${SECONDS}" -lt "${deadline}" ]; do
      sleep 30
      state=$(aws cloudwatch describe-alarms --alarm-names "${alarm}" --query 'MetricAlarms[0].StateValue' --output text)
    done
    check "$([ "${state}" = "ALARM" ] && echo true || echo false)" "${alarm} en ALARM" \
      "${alarm} no pasó a ALARM en ${SMOKE_ESPERA_ALARMA_S} s (estado ${state})"
    handle=$(find_marker "$(dlq_url "${SMOKE_DLQ_COLA}")" "${poison}" 30)
    [ -z "${handle}" ] || aws sqs delete-message --queue-url "$(dlq_url "${SMOKE_DLQ_COLA}")" --receipt-handle "${handle}"
    echo "  mensaje envenenado borrado de la DLQ; la alarma vuelve a OK en unos minutos"
  else
    ko "el mensaje envenenado no llegó a la DLQ de ${SMOKE_DLQ_COLA} (¿un consumidor lo borró?)"
  fi
fi

log "9. Caché de catálogo y reglas (HU-W03)"
cache=$(jq -c '.cache' <<<"${platform}")
if [ "$(jq -r '.enabled' <<<"${cache}")" != "true" ]; then
  skip "cache_enabled = false: catalogo y cotizacion leen sin caché."
else
  status=$(aws elasticache describe-replication-groups --replication-group-id "${prefix}-cache" \
    --query 'ReplicationGroups[0].Status' --output text 2>/dev/null || echo "inexistente")
  check "$(is "${status}" '^available$')" "${prefix}-cache: available" "${prefix}-cache: ${status} (esperado available)"
  for service in catalogo cotizacion; do
    task_definition=$(jq -r --arg s "${service}" '.[$s].task_definition // empty' <<<"${deployed}")
    if [ -z "${task_definition}" ]; then
      skip "${service}: no está desplegado."
      continue
    fi
    # SC2016: literal JMESPath.
    # shellcheck disable=SC2016
    url=$(aws ecs describe-task-definition --task-definition "${task_definition}" \
      --query 'taskDefinition.containerDefinitions[].environment[] | [?name==`CACHE_URL`].value | [0]' --output text)
    check "$(is "${url}" '^rediss://')" "${service}: CACHE_URL con TLS" "${service}: CACHE_URL '${url}' (esperado rediss://...)"
  done
fi

log "10. Servicios del Sprint 3: GET /health"
# shellcheck disable=SC2086 # lista separada por espacios.
check_health ${SPRINT3_SERVICES}

log "11. Enrutamiento de SiniestroAprobado"
check_route SiniestroAprobado dinero-indemnizaciones consulta-proyeccion

log "12. Entrada de telemetría (${canales}/telemetria/v1/eventos)"
if [ -z "$(jq -r '.["adaptador-telemetria"].service_name // empty' <<<"${deployed}")" ]; then
  skip "adaptador-telemetria: sin imagen publicada (make infra-microservicios SERVICES=adaptador-telemetria)."
else
  # Sin firma: el adaptador debe rechazarlo (HU-W21) o, con el esqueleto, responder 404. Un 401 o
  # 403 sería del borde (authorizer o WAF) y un 5xx, que el ALB no llega al adaptador.
  code=$(http_code -X POST -H "Content-Type: application/json" -d '{"sintetico": true}' "${canales}/telemetria/v1/eventos")
  check "$(is "${code}" '^(202|400|404|409|422)$')" "POST sin firma llegó a adaptador-telemetria (${code})" \
    "POST sin firma -> ${code:-sin respuesta} (401/403 = borde; 5xx = ALB o adaptador)"
fi

if [ "${SMOKE_W24:-1}" = "1" ]; then
  log "13. Auditoría por lotes (HU-W24, R5)"
  if [ -z "$(jq -r '.auditoria.service_name // empty' <<<"${deployed}")" ]; then
    skip "auditoria: sin imagen publicada (make infra-microservicios SERVICES=auditoria)."
  else
    bucket=$(jq -r '.audit_bucket.name' <<<"${platform}")
    desde=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    ids=()
    for _ in $(seq "${SMOKE_W24_EVENTOS:-5}"); do
      id=$(uuidgen | tr '[:upper:]' '[:lower:]')
      ids+=("${id}")
      event=$(jq -nc --arg id "${id}" --arg at "${desde}" \
        '{eventId: $id, aggregateId: $id, type: "EventoParametricoObservado", version: 1, occurredAt: $at, correlationId: ("humo-w24-" + $id), sintetico: true, payload: {}}')
      aws sns publish --topic-arn "$(jq -r '.events_topic_arn' <<<"${platform}")" --message "${event}" \
        --message-attributes '{"eventType": {"DataType": "String", "StringValue": "EventoParametricoObservado"}}' >/dev/null
    done
    deadline=$((SECONDS + SMOKE_ESPERA_RUTA_S))
    encontrados=""
    while [ "${SECONDS}" -lt "${deadline}" ]; do
      sleep 5
      claves=$(aws s3api list-objects-v2 --bucket "${bucket}" --prefix cadena/ \
        --query "Contents[?LastModified>='${desde}'].Key" --output text 2>/dev/null || true)
      encontrados=""
      for clave in ${claves}; do
        [ "${clave}" = "None" ] && continue
        encontrados+=$(aws s3 cp "s3://${bucket}/${clave}" - 2>/dev/null | jq -r '.registros[].eventId')$'\n'
      done
      todos=true
      for id in "${ids[@]}"; do grep -qx "${id}" <<<"${encontrados}" || todos=false; done
      [ "${todos}" = true ] && break
    done
    for id in "${ids[@]}"; do
      veces=$(grep -cx "${id}" <<<"${encontrados}" || true)
      check "$([ "${veces}" = "1" ] && echo true || echo false)" "evento ${id} en un lote de auditoría, una vez" \
        "evento ${id} aparece ${veces} veces en los lotes de auditoría (esperado 1)"
    done
    visibles=$(aws sqs get-queue-attributes --queue-url "$(queue_url auditoria)" \
      --attribute-names ApproximateNumberOfMessages --query 'Attributes.ApproximateNumberOfMessages' --output text)
    check "$([ "${visibles}" -lt 50 ] && echo true || echo false)" "cola auditoria al día (${visibles} visibles)" \
      "cola auditoria con ${visibles} mensajes visibles: auditoria no drena"
  fi
fi

log "Resultado: ${pass} OK, ${fail} con falla"
[ "${fail}" -eq 0 ]
