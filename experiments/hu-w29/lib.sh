# shellcheck shell=bash
# Funciones comunes de los scripts del experimento multizona (HU-W29). Reutiliza las de la
# plataforma (log, die, require, tf_init, platform_output) con ENV=exp por omisión.
#
# Variables de entorno:
#   ENV         ambiente del experimento                 por defecto: exp
#   RESULTADOS  carpeta de la evidencia de esta corrida   por defecto: experiments/hu-w29/resultados
ENV="${ENV:-exp}"
# shellcheck source=../../scripts/plataforma/lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../../scripts/plataforma" && pwd)/lib.sh"

EXPERIMENTO_DIR="${ROOT_DIR}/experiments/hu-w29"
RESULTADOS="${RESULTADOS:-${EXPERIMENTO_DIR}/resultados}"
mkdir -p "${RESULTADOS}"

[ "${ENV}" = "exp" ] || die "HU-W29 se ejecuta solo en exp (D4): int tiene puntos únicos de falla en la zona A."

# Output experiment de platform (null fuera de exp): zona, rol de FIS, subredes, base y tarea.
experimento() {
  local salida
  salida="$(platform_output experiment)"
  [ "${salida}" != "null" ] || die "platform no tiene experiments_enabled = true en ${ENV_DIR}/platform.tfvars."
  echo "${salida}"
}

ahora_utc() { date -u +%Y-%m-%dT%H:%M:%SZ; }
