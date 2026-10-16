#!/usr/bin/env bash
# HU-W28 AC4: activa y recupera la alarma de p95 del perfilamiento con una métrica
# sintética, sin carga. Publica OperationDuration con las dimensiones exactas de la
# alarma cada minuto, espera ALARM, deja de publicar y espera OK. Solo lee las
# alarmas del perfilamiento: no cambia su configuración ni su estado.
#
#   NIVEL=critica      900-1000 ms: se abren la advertencia y la crítica.
#   NIVEL=advertencia  entre la advertencia y la meta: solo se abre la advertencia.
#
# Variables: ENV, AWS_REGION, NIVEL (critica), PUBLICAR_MIN (5), ESPERA_ALARMA_S (600),
# ESPERA_OK_S (900), SONDEO_S (15), METRICS_NAMESPACE (Solventa).
# Sale con 0 si la alarma pasa a ALARM y vuelve a OK; con 1 si no.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require aws jq
require_env

NIVEL="${NIVEL:-critica}"
PUBLICAR_MIN="${PUBLICAR_MIN:-5}"
ESPERA_ALARMA_S="${ESPERA_ALARMA_S:-600}"
ESPERA_OK_S="${ESPERA_OK_S:-900}"
SONDEO_S="${SONDEO_S:-15}"
METRICS_NAMESPACE="${METRICS_NAMESPACE:-Solventa}"
case "${NIVEL}" in
  critica | advertencia) ;;
  *) die "NIVEL debe ser critica o advertencia." ;;
esac

prefijo="${NAME}-${ENV}-perfilamiento-p95"
alarma="${prefijo}-${NIVEL}"
alarmas=("${prefijo}-advertencia" "${prefijo}-critica")
dimensiones="Environment=${ENV},Service=cotizacion,Operation=perfilamiento"
envios=0

estado() {
  aws cloudwatch describe-alarms --alarm-names "$1" --query 'MetricAlarms[0].StateValue' --output text
}

umbral() {
  aws cloudwatch describe-alarms --alarm-names "$1" --query 'MetricAlarms[0].Threshold' --output text
}

ahora() { date -u +%s; }
hora() { date -u '+%H:%M:%S'; }

# esperar <alarma> <estado> <segundos> [publicar]: sondea hasta llegar al estado.
esperar() {
  local nombre="$1" esperado="$2" limite=$(($(ahora) + $3)) publicar="${4:-}" ultimo_envio=0 actual
  while :; do
    if [ -n "${publicar}" ] && [ $(($(ahora) - ultimo_envio)) -ge 60 ] && [ "${envios}" -lt "${PUBLICAR_MIN}" ]; then
      publicar_metrica
      ultimo_envio=$(ahora)
    fi
    actual="$(estado "${nombre}")"
    [ "${actual}" = "${esperado}" ] && return 0
    [ "$(ahora)" -ge "${limite}" ] && { warn "${nombre} sigue en ${actual} después de $3 s."; return 1; }
    sleep "${SONDEO_S}"
  done
}

publicar_metrica() {
  # shellcheck disable=SC2086 # valores y conteos van como argumentos separados
  aws cloudwatch put-metric-data --namespace "${METRICS_NAMESPACE}" --metric-name OperationDuration \
    --unit Milliseconds --dimensions "${dimensiones}" --values ${valores} --counts ${conteos}
  envios=$((envios + 1))
  printf '  %s  publicado %s/%s: %s ms\n' "$(hora)" "${envios}" "${PUBLICAR_MIN}" "${valores}"
}

log "Alarmas del perfilamiento en ${ENV}"
for nombre in "${alarmas[@]}"; do
  actual="$(estado "${nombre}")"
  [ "${actual}" != "None" ] || die "No existe ${nombre}. Ejecute make infra-aplicaciones ENV=${ENV}."
  printf '  %-45s %s (umbral %s ms)\n' "${nombre}" "${actual}" "$(umbral "${nombre}")"
done

# Idempotente: una ejecución anterior interrumpida pudo dejar una alarma abierta.
for nombre in "${alarmas[@]}"; do
  if [ "$(estado "${nombre}")" = "ALARM" ]; then
    warn "${nombre} está en ALARM; se espera a que vuelva a OK antes de empezar."
    esperar "${nombre}" OK "${ESPERA_OK_S}" || die "No se puede empezar con ${nombre} en ALARM."
  fi
done

advertencia="$(umbral "${prefijo}-advertencia")"
critica="$(umbral "${prefijo}-critica")"
if [ "${NIVEL}" = "critica" ]; then
  valores="$(jq -rn --argjson c "${critica}" '[900, 950, 1000] | map(if . > $c then . else $c + . / 10 end | floor) | join(" ")')"
  conteos="20 20 20"
else
  valores="$(jq -rn --argjson a "${advertencia}" --argjson c "${critica}" '($a + $c) / 2 | floor')"
  conteos="60"
fi

inicio=$(ahora)
log "1. Publicar OperationDuration sintético (${dimensiones}) cada minuto, hasta ${PUBLICAR_MIN} veces"
if ! esperar "${alarma}" ALARM "${ESPERA_ALARMA_S}" publicar; then
  printf '\nResultado: FALLA, %s no pasó a ALARM en %s s.\n' "${alarma}" "${ESPERA_ALARMA_S}"
  exit 1
fi
en_alarma=$(ahora)
printf '  %s  %s en ALARM a los %s s\n' "$(hora)" "${alarma}" $((en_alarma - inicio))

log "2. Dejar de publicar y esperar la recuperación (treat_missing_data = notBreaching)"
for nombre in "${alarmas[@]}"; do
  if ! esperar "${nombre}" OK "${ESPERA_OK_S}"; then
    printf '\nResultado: FALLA, %s no volvió a OK en %s s.\n' "${nombre}" "${ESPERA_OK_S}"
    exit 1
  fi
done
recuperada=$(ahora)
printf '  %s  alarmas del perfilamiento en OK a los %s s de la alerta\n' "$(hora)" $((recuperada - en_alarma))

log "Resultado: OK"
printf '  Alerta:      %s s desde la primera publicación\n' $((en_alarma - inicio))
printf '  Recuperada:  %s s desde la alerta\n' $((recuperada - en_alarma))
printf '  Correos esperados en alert_emails: %s\n' \
  "$([ "${NIVEL}" = "critica" ] && echo "2 de ALARMA (advertencia y crítica) y 2 de RECUPERADA" || echo "1 de ALARMA y 1 de RECUPERADA (advertencia)")"
