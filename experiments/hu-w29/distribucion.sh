#!/usr/bin/env bash
# Foto de la distribución por zona (N2 y evidencia antes, durante y después):
#   ./distribucion.sh antes|durante|despues
# Escribe resultados/distribucion-<etiqueta>.json con las tareas RUNNING y saludables de cada
# servicio por zona, y la zona de la base principal y de su standby. Solo lee.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require terraform aws jq
require_env
etiqueta="${1:?Indique antes, durante o despues}"
tf_init platform

exp="$(experimento)"
cluster="$(jq -r .reconciliation_task.cluster <<<"${exp}")"
db_id="$(jq -r '.db_instance_arn | split(":") | last' <<<"${exp}")"

tareas="[]"
servicios="$(aws ecs list-services --cluster "${cluster}" --query 'serviceArns' --output json)"
for servicio in $(jq -r '.[]' <<<"${servicios}"); do
  arns="$(aws ecs list-tasks --cluster "${cluster}" --service-name "${servicio}" --desired-status RUNNING --query taskArns --output json)"
  [ "$(jq length <<<"${arns}")" -gt 0 ] || continue
  # describe-tasks admite hasta 100 tareas por llamada; cada servicio tiene a lo sumo max_replicas.
  detalle="$(aws ecs describe-tasks --cluster "${cluster}" --tasks $(jq -r '.[]' <<<"${arns}") \
    --query 'tasks[].{servicio:group,zona:availabilityZone,estado:lastStatus,salud:healthStatus}' --output json)"
  tareas="$(jq -s 'add' <(echo "${tareas}") <(echo "${detalle}"))"
done

base="$(aws rds describe-db-instances --db-instance-identifier "${db_id}" \
  --query 'DBInstances[0].{principal:AvailabilityZone,standby:SecondaryAvailabilityZone,multiAz:MultiAZ,estado:DBInstanceStatus}' --output json)"

jq -n --arg etiqueta "${etiqueta}" --arg instante "$(ahora_utc)" --arg zona_falla "$(jq -r .zone <<<"${exp}")" \
  --argjson tareas "${tareas}" --argjson base "${base}" '{
  etiqueta: $etiqueta, instante: $instante, zonaFalla: $zona_falla, base: $base,
  servicios: ($tareas | map(.servicio |= ltrimstr("service:")) | group_by(.servicio) | map({
    key: .[0].servicio,
    value: (group_by(.zona) | map({key: .[0].zona, value: {
      running: map(select(.estado == "RUNNING")) | length,
      saludables: map(select(.estado == "RUNNING" and .salud != "UNHEALTHY")) | length
    }}) | from_entries)
  }) | from_entries)
}' >"${RESULTADOS}/distribucion-${etiqueta}.json"

log "Distribución ${etiqueta}: ${RESULTADOS}/distribucion-${etiqueta}.json"
# N2: cada servicio crítico debe tener tareas saludables en las dos zonas antes de la falla.
criticos="bff-web bff-movil api-socios cotizacion polizas dinero siniestros pagos-parametricos"
for servicio in ${criticos}; do
  zonas=$(jq -r --arg s "${servicio}" '.servicios[$s] // {} | to_entries | map(select(.value.saludables > 0)) | length' \
    "${RESULTADOS}/distribucion-${etiqueta}.json")
  printf '  %-20s zonas con tareas saludables: %s\n' "${servicio}" "${zonas}"
  if [ "${etiqueta}" = "antes" ] && [ "${zonas}" -lt 2 ]; then
    die "N2: ${servicio} no tiene tareas saludables en dos zonas; no iniciar el experimento."
  fi
done
