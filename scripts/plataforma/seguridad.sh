#!/usr/bin/env bash
# Escaneo de seguridad de Terraform con Trivy (sucesor de tfsec), sin AWS.
#   1. Informa los hallazgos HIGH (no bloquean).
#   2. Falla si queda algún hallazgo CRITICAL que no esté justificado en
#      infra/.trivyignore.yaml.
# Usa trivy del PATH o, si no está, la imagen aquasec/trivy con Docker.
# Variables: TRIVY_VERSION (imagen de Docker), TRIVY=0 omite el escaneo.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

TRIVY_VERSION="${TRIVY_VERSION:-0.67.2}"
if [ "${TRIVY:-1}" = "0" ]; then
  warn "TRIVY=0: se omite el escaneo de seguridad de Terraform."
  exit 0
fi

# trivy_run <argumentos de trivy...>: se ejecuta desde la raíz del repositorio.
if command -v trivy >/dev/null 2>&1; then
  trivy_run() { (cd "${ROOT_DIR}" && trivy "$@"); }
elif command -v docker >/dev/null 2>&1; then
  trivy_run() { docker run --rm -v "${ROOT_DIR}:/src:ro" -w /src "aquasec/trivy:${TRIVY_VERSION}" "$@"; }
else
  die "Falta trivy o docker en el PATH (TRIVY=0 omite el escaneo)."
fi

args=(config --quiet --ignorefile infra/.trivyignore.yaml --tf-vars infra/envs/int/platform.tfvars)

log "Trivy: hallazgos HIGH (informativos)"
trivy_run "${args[@]}" --severity HIGH --exit-code 0 infra

log "Trivy: hallazgos CRITICAL (bloquean)"
trivy_run "${args[@]}" --severity CRITICAL --exit-code 1 infra
echo "Sin hallazgos críticos sin justificar."
