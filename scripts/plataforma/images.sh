#!/usr/bin/env bash
# Paso 3: construye y publica en ECR las imágenes de los microservicios.
#   - Código: ${SRC_DIR}/<servicio>/Dockerfile (services/ por defecto), con
#     contexto en la raíz del repo para que la imagen incluya libs/.
#   - Sin SERVICES: todos los servicios del catálogo que tienen Dockerfile.
#   - Etiqueta inmutable = commit (12 caracteres); si el árbol tiene cambios sin
#     commit se agrega -dirty-<timestamp>. Si la etiqueta ya existe en ECR no se
#     vuelve a construir.
# Requiere que platform exista (los repositorios ECR se crean allí).
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require terraform aws jq docker git
require_env
tf_init platform

repositories="$(platform_output ecr_repositories)"
registry=$(jq -r 'first(.[]) | split("/")[0]' <<<"${repositories}")
mapfile -t services < <(selected_services)
[ "${#services[@]}" -gt 0 ] || { warn "No hay servicios con Dockerfile en ${SRC_DIR} (ni SERVICES indicado); nada que publicar."; exit 0; }

tag="$(git -C "${ROOT_DIR}" rev-parse --short=12 HEAD)"
if [ -n "$(git -C "${ROOT_DIR}" status --porcelain -- "${SRC_DIR}" "${ROOT_DIR}/libs")" ]; then
  tag="${tag}-dirty-$(date -u +%Y%m%d%H%M%S)"
  warn "Hay cambios sin commit en ${SRC_DIR} o libs/; se usa la etiqueta ${tag}."
fi

log "Publicando ${services[*]} con etiqueta ${tag} (${ENV})"
aws ecr get-login-password --region "${AWS_REGION}" | docker login --username AWS --password-stdin "${registry}" >/dev/null

for service in "${services[@]}"; do
  dockerfile="${SRC_DIR}/${service}/Dockerfile"
  [ -f "${dockerfile}" ] || die "No existe ${dockerfile}."
  repository_url=$(jq -r --arg s "${service}" '.[$s]' <<<"${repositories}")
  repository_name="${repository_url#*/}"

  if aws ecr describe-images --repository-name "${repository_name}" --image-ids imageTag="${tag}" >/dev/null 2>&1; then
    echo "  = ${service}:${tag} ya existe en ECR; se reutiliza."
    continue
  fi
  echo "  + ${service} -> ${repository_url}:${tag}"
  docker buildx build \
    --platform linux/amd64 \
    --provenance=false \
    --file "${dockerfile}" \
    --tag "${repository_url}:${tag}" \
    --push "${ROOT_DIR}"
done

log "Imágenes publicadas"
