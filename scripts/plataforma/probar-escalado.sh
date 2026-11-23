#!/usr/bin/env bash
# HU-W25 N2 en local: corre una fase de tests/e2e/test_hu_w25_escalado.py con las credenciales de
# quien lo ejecuta, por el mismo camino que e2e.yml (require_env, tf_init platform, E2E_REQUERIDO=1).
# Publica eventos sintéticos en el tópico de eventos de int y, el respaldo, fuerza una alarma de
# escalado de pagos-parametricos. Recorrido y orden de las fases: plan de HU-W25 §f (Proyecto/docs);
# configuración: infra/DESPLIEGUE.md, sección 7.
#
#   FASE=publicar   ráfaga (y complemento) desde el mínimo de tareas; hasta ~19 min
#   FASE=recoger    goteo mientras haya más del mínimo, línea de tiempo y reconciliación; enseguida
#   FASE=respaldo   solo si no escaló ni con el complemento; después, otra vez recoger
#
# Variables: ENV (obligatoria, int), SERVICE (pagos-parametricos), FASE, AWS_REGION.
# Sale con el código de pytest: una guarda incumplida falla (E2E_REQUERIDO=1), no se omite.

# lib.sh pone ENV=int por omisión: se lee antes si quien lo ejecuta lo indicó.
ENV_INDICADO="${ENV:-}"
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

SERVICE="${SERVICE:-}"
FASE="${FASE:-}"
[ -n "${ENV_INDICADO}" ] || die "Indique ENV=int: la N2 de HU-W25 solo corre en int."
[ "${ENV}" = "int" ] || die "La N2 de HU-W25 solo corre con ENV=int (ENV=${ENV})."
[ "${SERVICE}" = "pagos-parametricos" ] ||
  die "SERVICE debe ser pagos-parametricos (SERVICE=${SERVICE:-vacío}): la N2 de HU-W25 solo escala ese servicio."
case "${FASE}" in
  publicar | recoger | respaldo) ;;
  *) die "FASE debe ser publicar, recoger o respaldo (FASE=${FASE:-vacía})." ;;
esac

require aws terraform uv
require_env

log "Estado de la plataforma de ${ENV}"
tf_init platform

log "HU-W25 N2: hu_w25_${FASE} de ${SERVICE} en ${ENV}"
cd "${ROOT_DIR}"
export E2E_REQUERIDO=1
exec uv run pytest tests/e2e/test_hu_w25_escalado.py -v -rA -k "hu_w25_${FASE}"
