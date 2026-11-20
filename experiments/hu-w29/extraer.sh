#!/usr/bin/env bash
# Extrae de las bases, en solo lectura, las operaciones observadas en la ventana del
# experimento (AC2) con la tarea puntual de reconciliación (infra/platform/experiments.tf):
#   DESDE=2026-11-20T14:00:00Z HASTA=2026-11-20T14:30:00Z ./extraer.sh
# Escribe resultados/observadas.ndjson: una línea JSON por fila. Ejecuta una tarea ECS:
# pedir confirmación antes (docs/hu-w29-multizona.md, paso 7).
#
# R1, R3 y R5: cotizaciones, aceptaciones y cobros; eventos de telemetría recibidos y evaluados,
# activaciones, siniestros paramétricos y sus pagos. Cada base con el usuario de su servicio.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require terraform aws jq
require_env
: "${DESDE:?Indique DESDE (inicio de la carga, ISO 8601 UTC)}"
: "${HASTA:?Indique HASTA (fin de la carga, ISO 8601 UTC)}"
# Van dentro del SQL: solo se admite el formato ISO 8601 UTC.
for instante in "${DESDE}" "${HASTA}"; do
  [[ "${instante}" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]] ||
    die "${instante} no tiene el formato AAAA-MM-DDTHH:MM:SSZ."
done
tf_init platform

exp="$(experimento)"
tarea="$(jq .reconciliation_task <<<"${exp}")"

# Cada consulta imprime filas JSON con el prefijo "@@ " para separarlas del resto del log.
# Las operaciones del recorrido se filtran por la ventana; las que derivan de ellas (el cobro)
# y el Outbox pendiente, no: pueden completarse después de HASTA.
read -r -d '' script <<'SH' || true
set -eu
q() {
  PGDATABASE="$1" PGUSER="$2" PGPASSWORD="$3" psql -v ON_ERROR_STOP=1 -At -c "$4" | sed 's/^/@@ /'
}
pendientes() {
  q "$1" "$2" "$3" "SELECT json_build_object('base', '$1', 'tipo', 'outbox_pendiente', 'total', count(*),
    'masAntiguo', min(occurred_at)) FROM outbox WHERE published_at IS NULL"
}
# R1: cotizaciones creadas por el portal en la ventana.
q cotizacion "$USER_COTIZACION" "$PW_COTIZACION" "SELECT json_build_object('base', 'cotizacion',
  'tipo', 'cotizacion', 'id', id, 'clave', idempotency_key, 'instante', calculado_en)
  FROM cotizaciones WHERE calculado_en >= '$DESDE' AND calculado_en < '$HASTA'"
pendientes cotizacion "$USER_COTIZACION" "$PW_COTIZACION"
# R3: aceptaciones (evidencia inmutable) y su cobro (una vez por claveRecaudo = aceptacionId).
q polizas "$USER_POLIZAS" "$PW_POLIZAS" "SELECT json_build_object('base', 'polizas',
  'tipo', 'aceptacion', 'id', id, 'clave', idempotency_key, 'cotizacionId', cotizacion_id,
  'instante', aceptada_en) FROM aceptaciones WHERE aceptada_en >= '$DESDE' AND aceptada_en < '$HASTA'"
pendientes polizas "$USER_POLIZAS" "$PW_POLIZAS"
q dinero "$USER_DINERO" "$PW_DINERO" "SELECT json_build_object('base', 'dinero', 'tipo', 'recaudo',
  'id', recaudo_id, 'clave', clave_recaudo, 'cotizacionId', cotizacion_id, 'estado', estado,
  'instante', creado_en) FROM recaudos WHERE creado_en >= '$DESDE'"
pendientes dinero "$USER_DINERO" "$PW_DINERO"
# R5: evento recibido (HU-W21, por idExterno), su evaluación, la activación, el siniestro
# paramétrico (HU-W22, uno por evento y póliza) y su pago (HU-W23, uno por siniestro).
q adaptador_telemetria "$USER_ADAPTADOR_TELEMETRIA" "$PW_ADAPTADOR_TELEMETRIA" "SELECT json_build_object(
  'base', 'adaptador_telemetria', 'tipo', 'evento_recibido', 'id', evento_id, 'clave', id_externo,
  'instante', recibido_en) FROM eventos_recibidos WHERE recibido_en >= '$DESDE' AND recibido_en < '$HASTA'"
pendientes adaptador_telemetria "$USER_ADAPTADOR_TELEMETRIA" "$PW_ADAPTADOR_TELEMETRIA"
q pagos_parametricos "$USER_PAGOS_PARAMETRICOS" "$PW_PAGOS_PARAMETRICOS" "SELECT json_build_object(
  'base', 'pagos_parametricos', 'tipo', 'evento_evaluado', 'id', evento_id, 'clave', id_externo,
  'decision', decision, 'instante', evaluado_en) FROM eventos_evaluados WHERE evaluado_en >= '$DESDE'"
q pagos_parametricos "$USER_PAGOS_PARAMETRICOS" "$PW_PAGOS_PARAMETRICOS" "SELECT json_build_object(
  'base', 'pagos_parametricos', 'tipo', 'activacion', 'id', evento_id, 'polizaId', poliza_id,
  'estado', estado, 'siniestroId', siniestro_id, 'instante', creada_en) FROM activaciones
  WHERE creada_en >= '$DESDE'"
pendientes pagos_parametricos "$USER_PAGOS_PARAMETRICOS" "$PW_PAGOS_PARAMETRICOS"
q siniestros "$USER_SINIESTROS" "$PW_SINIESTROS" "SELECT json_build_object('base', 'siniestros',
  'tipo', 'siniestro', 'id', id, 'eventoId', evento_id, 'polizaId', poliza_id, 'instante', reportado_en)
  FROM siniestros WHERE origen = 'PARAMETRICO' AND reportado_en >= '$DESDE'"
pendientes siniestros "$USER_SINIESTROS" "$PW_SINIESTROS"
q dinero "$USER_DINERO" "$PW_DINERO" "SELECT json_build_object('base', 'dinero', 'tipo', 'indemnizacion',
  'id', indemnizacion_id, 'siniestroId', siniestro_id, 'estado', estado, 'instante', creado_en)
  FROM indemnizaciones WHERE creado_en >= '$DESDE'"
echo "extraccion terminada"
SH

overrides="$(jq -n --arg script "${script}" --arg desde "${DESDE}" --arg hasta "${HASTA}" '{
  containerOverrides: [{
    name: "reconciliacion",
    command: ["sh", "-c", $script],
    environment: [{name: "DESDE", value: $desde}, {name: "HASTA", value: $hasta}]
  }]
}')"

log "Extrayendo operaciones de ${DESDE} a ${HASTA} (${ENV})"
task_arn=$(aws ecs run-task --cluster "$(jq -r .cluster <<<"${tarea}")" \
  --task-definition "$(jq -r .task_definition <<<"${tarea}")" \
  --launch-type FARGATE --overrides "${overrides}" \
  --network-configuration "awsvpcConfiguration={subnets=[$(jq -r '.subnets | join(",")' <<<"${tarea}")],securityGroups=[$(jq -r .security_group <<<"${tarea}")],assignPublicIp=DISABLED}" \
  --query 'tasks[0].taskArn' --output text)
[ -n "${task_arn}" ] && [ "${task_arn}" != "None" ] || die "No se pudo iniciar la tarea de reconciliación."

echo "Tarea ${task_arn##*/}; esperando a que termine..."
aws ecs wait tasks-stopped --cluster "$(jq -r .cluster <<<"${tarea}")" --tasks "${task_arn}"
salida=$(aws ecs describe-tasks --cluster "$(jq -r .cluster <<<"${tarea}")" --tasks "${task_arn}" \
  --query 'tasks[0].containers[0].exitCode' --output text)

grupo="$(jq -r .log_group <<<"${tarea}")"
flujo="reconciliacion/reconciliacion/${task_arn##*/}"
: >"${RESULTADOS}/observadas.ndjson"
token=""
while :; do
  pagina=$(aws logs get-log-events --log-group-name "${grupo}" --log-stream-name "${flujo}" --start-from-head \
    ${token:+--next-token "${token}"} --output json)
  jq -r '.events[].message | select(startswith("@@ ")) | ltrimstr("@@ ")' <<<"${pagina}" >>"${RESULTADOS}/observadas.ndjson"
  siguiente=$(jq -r .nextForwardToken <<<"${pagina}")
  [ "${siguiente}" != "${token}" ] || break
  token="${siguiente}"
done

[ "${salida}" = "0" ] || die "La extracción falló (salida ${salida}). Revisar ${grupo}."
log "Observadas: $(wc -l <"${RESULTADOS}/observadas.ndjson" | tr -d ' ') filas en ${RESULTADOS}/observadas.ndjson"
