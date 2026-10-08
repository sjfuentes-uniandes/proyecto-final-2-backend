#!/usr/bin/env bash
# Paso 4: infra/apps. Despliega los servicios que tienen imagen en ECR y espera
# a que queden estables.
# Uso: apps.sh [apply|plan]   (PAUSED=1 lleva todo a 0 tareas; PAUSED=0 reanuda)
# Con PLAN_FILE (ruta absoluta), plan guarda el plan ahí y apply aplica ese plan
# exacto (sin recalcular digests ni variables).
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require terraform aws jq
require_env
export_apps_vars

action="${1:-apply}"
saved_plan=""
[ "${action}" = "apply" ] && saved_plan="${PLAN_FILE:-}"
images="${ENV_DIR}/images.tfvars.json"
if [ -z "${saved_plan}" ]; then
  "$(dirname "${BASH_SOURCE[0]}")/digests.sh"
  if [ "$(jq '.image_digests | length' "${images}")" = "0" ]; then
    warn "Ningún servicio tiene imagen en ECR todavía: no hay aplicaciones que desplegar. Ejecute make infra-imagenes."
    exit 0
  fi
fi

out_args=()
[ -n "${PLAN_FILE:-}" ] && out_args=(-out="${PLAN_FILE}")
pause_args=()
if [ -n "${PAUSED:-}" ]; then
  pause_args=(-var "paused=$([ "${PAUSED}" = "1" ] && echo true || echo false)")
fi

log "Aplicaciones del ambiente ${ENV}: terraform ${action}"
tf_init apps
deployed_services() {
  terraform -chdir="${INFRA_DIR}/apps" output -json deployed_services 2>/dev/null | jq -c 'keys' || echo '[]'
}
before="$(deployed_services)"
case "${action}" in
  plan)
    terraform -chdir="${INFRA_DIR}/apps" plan -input=false \
      -var-file="${ENV_DIR}/apps.tfvars" -var-file="${images}" ${pause_args[@]+"${pause_args[@]}"} ${out_args[@]+"${out_args[@]}"}
    exit 0
    ;;
  apply)
    if [ -n "${saved_plan}" ]; then
      terraform -chdir="${INFRA_DIR}/apps" apply -input=false "${saved_plan}"
    else
      # shellcheck disable=SC2046
      terraform -chdir="${INFRA_DIR}/apps" apply -input=false $(approve_flag) \
        -var-file="${ENV_DIR}/apps.tfvars" -var-file="${images}" ${pause_args[@]+"${pause_args[@]}"}
    fi
    ;;
  *) die "Acción no soportada: ${action}" ;;
esac

[ "${PAUSED:-0}" = "1" ] && { log "Servicios pausados (0 tareas)"; exit 0; }

cluster=$(terraform -chdir="${INFRA_DIR}/apps" output -raw cluster_name)

# Service Connect solo entrega a una tarea los endpoints que existían cuando
# arrancó: si se creó un servicio nuevo, se reinician los que ya lo invocaban.
after="$(deployed_services)"
new=$(jq -nc --argjson a "${after}" --argjson b "${before}" '$a - $b')
if [ "${before}" != "[]" ] && [ "${new}" != "[]" ]; then
  while IFS= read -r service; do
    echo "  ~ ${service}: nuevo despliegue para descubrir $(jq -r 'join(", ")' <<<"${new}")"
    aws ecs update-service --cluster "${cluster}" --service "${service}" --force-new-deployment >/dev/null
  done < <(platform_output platform | jq -r --argjson new "${new}" --argjson old "${before}" \
    '.catalog | to_entries[] | select(.key as $k | $old | index($k)) | select(any(.value.calls[]; . as $c | $new | index($c))) | .key')
fi
names=()
while IFS= read -r name; do names+=("${name}"); done < <(terraform -chdir="${INFRA_DIR}/apps" output -json deployed_services | jq -r '.[].service_name')
log "Esperando a que ${#names[@]} servicios queden estables"
for ((i = 0; i < ${#names[@]}; i += 10)); do
  aws ecs wait services-stable --cluster "${cluster}" --services "${names[@]:i:10}"
done
terraform -chdir="${INFRA_DIR}/apps" output api_urls
terraform -chdir="${INFRA_DIR}/apps" output pending_services
