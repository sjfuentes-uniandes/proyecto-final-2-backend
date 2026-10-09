# shellcheck shell=bash
# Funciones comunes de los scripts de la plataforma (infra/platform + infra/apps).
# Compatibles con bash 3.2 (el de macOS): sin mapfile, ${var,,} ni arreglos
# asociativos, y los arreglos que pueden quedar vacíos se expanden con ${a[@]+...}.
# Las usan el Makefile (targets infra-*) y los workflows de GitHub Actions, de
# modo que el despliegue local y el de CI siguen exactamente el mismo camino.
#
# Variables de entorno:
#   ENV           ambiente (carpeta infra/envs/<ENV>)        por defecto: int
#   AWS_REGION    región de la cuenta y del estado remoto    por defecto: us-east-1
#   NAME          prefijo del bucket de estado               por defecto: solventa
#   AUTO_APPROVE  1 = aplica sin preguntar (CI)              por defecto: 0
#   SERVICES      lista separada por comas o espacios        por defecto: todos
#   SRC_DIR       carpeta con <servicio>/Dockerfile          por defecto: services

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
INFRA_DIR="${ROOT_DIR}/infra"
ENV="${ENV:-int}"
AWS_REGION="${AWS_REGION:-us-east-1}"
NAME="${NAME:-solventa}"
AUTO_APPROVE="${AUTO_APPROVE:-0}"
SRC_DIR="${SRC_DIR:-${ROOT_DIR}/services}"
ENV_DIR="${INFRA_DIR}/envs/${ENV}"
export AWS_REGION AWS_DEFAULT_REGION="${AWS_REGION}"

log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[aviso] %s\033[0m\n' "$*" >&2; }
die() { printf '\033[1;31m[error] %s\033[0m\n' "$*" >&2; exit 1; }

require() {
  local tool
  for tool in "$@"; do
    command -v "${tool}" >/dev/null 2>&1 || die "Falta ${tool} en el PATH."
  done
}

require_env() {
  [ -d "${ENV_DIR}" ] || die "No existe ${ENV_DIR}. Copie infra/envs/int como plantilla del ambiente ${ENV}."
  [ -f "${ENV_DIR}/platform.tfvars" ] || die "Falta ${ENV_DIR}/platform.tfvars."
  [ -f "${ENV_DIR}/apps.tfvars" ] || die "Falta ${ENV_DIR}/apps.tfvars."
  local declared
  declared=$(sed -nE 's/^[[:space:]]*environment[[:space:]]*=[[:space:]]*"([^"]+)".*/\1/p' "${ENV_DIR}/platform.tfvars")
  [ "${declared}" = "${ENV}" ] || die "environment en ${ENV_DIR}/platform.tfvars (${declared:-vacío}) debe ser \"${ENV}\"."
}

account_id() {
  aws sts get-caller-identity --query Account --output text
}

state_bucket() {
  echo "${NAME}-tfstate-$(account_id)"
}

# Parámetros comunes de apps: leer el estado de platform del mismo ambiente.
export_apps_vars() {
  TF_VAR_state_bucket="$(state_bucket)"
  TF_VAR_state_region="${AWS_REGION}"
  TF_VAR_environment="${ENV}"
  export TF_VAR_state_bucket TF_VAR_state_region TF_VAR_environment
}

# tf_init <platform|apps>: backend S3 del ambiente (key = <ENV>/<raíz>.tfstate).
tf_init() {
  local root="$1" bucket
  bucket="$(state_bucket)"
  aws s3api head-bucket --bucket "${bucket}" >/dev/null 2>&1 ||
    die "No existe el bucket de estado ${bucket}. Ejecute primero: make infra-bootstrap"
  terraform -chdir="${INFRA_DIR}/${root}" init -input=false -reconfigure \
    -backend-config="bucket=${bucket}" \
    -backend-config="key=${ENV}/${root}.tfstate" \
    -backend-config="region=${AWS_REGION}" \
    -backend-config="encrypt=true" \
    -backend-config="use_lockfile=true" >/dev/null
}

approve_flag() {
  if [ "${AUTO_APPROVE}" = "1" ]; then echo "-auto-approve"; fi
}

# ¿Tiene recursos el estado de una raíz? (para destruir de forma idempotente)
state_has_resources() {
  local root="$1"
  [ -n "$(terraform -chdir="${INFRA_DIR}/${root}" state list 2>/dev/null | head -1)" ]
}

platform_output() {
  terraform -chdir="${INFRA_DIR}/platform" output -json "$1"
}

# Servicios del catálogo (claves de ecr_repositories de platform).
catalog_services() {
  platform_output ecr_repositories | jq -r 'keys[]'
}

# Servicios pedidos en SERVICES, validados contra el catálogo. Sin SERVICES:
# todos los del catálogo que tengan código (SRC_DIR/<servicio>/Dockerfile).
selected_services() {
  local catalog requested service
  catalog="$(catalog_services)"
  requested="$(echo "${SERVICES:-}" | tr ',' ' ' | xargs -n1 2>/dev/null || true)"
  if [ -z "${requested}" ]; then
    for service in ${catalog}; do
      [ -f "${SRC_DIR}/${service}/Dockerfile" ] && echo "${service}"
    done
    return 0
  fi
  for service in ${requested}; do
    echo "${catalog}" | grep -qx "${service}" || die "El servicio ${service} no está en el catálogo (infra/platform/catalog.tf)."
    echo "${service}"
  done
}
