#!/usr/bin/env bash
# Paso 1: infra/platform (red, datos, mensajería, identidad, borde, ECR).
# Uso: platform.sh [apply|plan]
# Con PLAN_FILE (ruta absoluta), plan guarda el plan ahí y apply aplica ese plan
# exacto sin volver a calcularlo (así lo usa el workflow: plan, aprobación, apply).
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require terraform aws
require_env

action="${1:-apply}"
log "Plataforma del ambiente ${ENV}: terraform ${action}"
tf_init platform
case "${action}" in
  plan)
    terraform -chdir="${INFRA_DIR}/platform" plan -input=false -var-file="${ENV_DIR}/platform.tfvars" \
      ${PLAN_FILE:+"-out=${PLAN_FILE}"}
    ;;
  apply)
    if [ -n "${PLAN_FILE:-}" ]; then
      terraform -chdir="${INFRA_DIR}/platform" apply -input=false "${PLAN_FILE}"
      exit 0
    fi
    # shellcheck disable=SC2046
    terraform -chdir="${INFRA_DIR}/platform" apply -input=false $(approve_flag) -var-file="${ENV_DIR}/platform.tfvars"
    ;;
  *) die "Acción no soportada: ${action}" ;;
esac
