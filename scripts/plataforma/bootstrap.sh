#!/usr/bin/env bash
# Una vez por cuenta: bucket de estado remoto y rol OIDC para GitHub Actions.
# Requiere credenciales de administrador. Es idempotente.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require terraform aws

log "Bootstrap de la cuenta $(account_id) en ${AWS_REGION}"
terraform -chdir="${INFRA_DIR}/bootstrap" init -input=false >/dev/null
# shellcheck disable=SC2046
terraform -chdir="${INFRA_DIR}/bootstrap" apply -input=false $(approve_flag) \
  -var "aws_region=${AWS_REGION}" -var "name=${NAME}" \
  ${GITHUB_REPOSITORY:+-var "github_repository=${GITHUB_REPOSITORY}"} \
  ${CREATE_GITHUB_OIDC_PROVIDER:+-var "create_github_oidc_provider=${CREATE_GITHUB_OIDC_PROVIDER}"}

log "Listo. Configurar en GitHub (Settings > Secrets and variables > Actions > Variables):"
echo "  AWS_ROLE_ARN = $(terraform -chdir="${INFRA_DIR}/bootstrap" output -raw github_role_arn)"
echo "  AWS_REGION   = ${AWS_REGION}"
