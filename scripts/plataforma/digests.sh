#!/usr/bin/env bash
# Genera infra/envs/<ENV>/images.tfvars.json con el digest más reciente de cada
# repositorio ECR que tenga imágenes. apps despliega exactamente esos servicios,
# así que publicar un solo microservicio no retira a los demás.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require terraform aws jq
require_env
tf_init platform

output="${ENV_DIR}/images.tfvars.json"
repositories="$(platform_output ecr_repositories)"
digests='{}'
for service in $(jq -r 'keys[]' <<<"${repositories}"); do
  repository_name=$(jq -r --arg s "${service}" '.[$s] | split("/")[1:] | join("/")' <<<"${repositories}")
  digest=$(aws ecr describe-images --repository-name "${repository_name}" \
    --query 'sort_by(imageDetails,&imagePushedAt)[-1].imageDigest' --output text 2>/dev/null || true)
  if [ -n "${digest}" ] && [ "${digest}" != "None" ]; then
    digests=$(jq --arg s "${service}" --arg d "${digest}" '. + {($s): $d}' <<<"${digests}")
  fi
done
jq -n --argjson d "${digests}" '{image_digests: $d}' >"${output}"
echo "Servicios con imagen: $(jq -r '.image_digests | keys | join(", ")' "${output}")"
