#!/usr/bin/env bash
# Destruye el ambiente completo en orden inverso: apps -> platform.
# El bucket de estado y el rol de GitHub (infra/bootstrap) se conservan.
# Es idempotente: si una raíz ya no tiene recursos, se omite.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require terraform aws jq
require_env
export_apps_vars

if [ "${AUTO_APPROVE}" != "1" ]; then
  read -r -p "Se destruirá TODO el ambiente ${ENV} (datos de RDS y auditoría incluidos). Escriba el nombre del ambiente para confirmar: " answer
  [ "${answer}" = "${ENV}" ] || die "Cancelado."
fi

log "1/3 Aplicaciones (${ENV})"
tf_init apps
if state_has_resources apps; then
  # image_digests vacío: el plan elimina todos los servicios sin consultar ECR.
  terraform -chdir="${INFRA_DIR}/apps" destroy -input=false -auto-approve \
    -var-file="${ENV_DIR}/apps.tfvars" -var 'image_digests={}'
else
  echo "Sin recursos en el estado de apps."
fi

log "2/3 Vaciando el bucket de auditoría (Object Lock en modo GOVERNANCE)"
tf_init platform
if state_has_resources platform; then
  bucket=$(platform_output platform | jq -r '.audit_bucket.name')
  if aws s3api head-bucket --bucket "${bucket}" >/dev/null 2>&1; then
    while :; do
      batch=$(aws s3api list-object-versions --bucket "${bucket}" --max-items 500 \
        --query '{Objects: [Versions[].{Key:Key,VersionId:VersionId}, DeleteMarkers[].{Key:Key,VersionId:VersionId}][] | [?Key]}' \
        --output json 2>/dev/null || echo '{"Objects":[]}')
      [ "$(jq '.Objects | length' <<<"${batch}")" -gt 0 ] || break
      aws s3api delete-objects --bucket "${bucket}" --bypass-governance-retention \
        --delete "$(jq -c '{Objects: .Objects, Quiet: true}' <<<"${batch}")" >/dev/null
    done
  fi

  log "3/3 Plataforma (${ENV})"
  terraform -chdir="${INFRA_DIR}/platform" destroy -input=false -auto-approve \
    -var-file="${ENV_DIR}/platform.tfvars"
else
  echo "Sin recursos en el estado de platform."
fi

rm -f "${ENV_DIR}/images.tfvars.json"
log "Ambiente ${ENV} destruido. Se conservan el bucket de estado y el rol de GitHub (infra/bootstrap)."
