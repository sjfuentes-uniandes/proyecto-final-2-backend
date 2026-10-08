#!/usr/bin/env bash
# Paso 1: infra/platform (red, datos, mensajería, identidad, borde, ECR).
# Uso: platform.sh [apply|plan]
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require terraform aws
require_env

action="${1:-apply}"
log "Plataforma del ambiente ${ENV}: terraform ${action}"
tf_init platform
case "${action}" in
  plan) terraform -chdir="${INFRA_DIR}/platform" plan -input=false -var-file="${ENV_DIR}/platform.tfvars" ;;
  apply)
    # shellcheck disable=SC2046
    terraform -chdir="${INFRA_DIR}/platform" apply -input=false $(approve_flag) -var-file="${ENV_DIR}/platform.tfvars"
    ;;
  *) die "Acción no soportada: ${action}" ;;
esac
