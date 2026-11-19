"""HU-W22 (H09): «Siniestros paramétricos» para el analista (grupo `analistas-siniestros`).

Compone el resumen con tres fuentes (DS3-26, v1.6):

1. **`pagos-parametricos`** (`GET /v1/parametricos/resumen`): indicadores, ritmo y últimos pagos. Es la
   fuente principal. Falla cerrada, como la cola de descarte (HU-W24): solo pasa su 422
   `SOLICITUD_INVALIDA`; cualquier otra respuesta, un timeout o un error de conexión es 503
   `SERVICIO_NO_DISPONIBLE` sin detalles.
2. **CloudWatch** (`MetricasParametricos`): la profundidad de `parametricos-eventos`, las tareas
   activas y, si `pagos-parametricos` no pudo leerla, la DLQ. Sin métrica o sin permiso, `null`.
3. **`siniestros`** (`GET /v1/siniestros/parametricos/pagos`): el `estadoPago` de cada fila. Si no
   responde, `null` en todas.

**Una fuente secundaria que falla nunca da 503**: su campo va en `null` y H09 lo muestra «no
disponible». `bff-web` no llama a `dinero` hasta SYNC-01. El aviso de «p95 durante la avalancha»
(CF-S3-10) no tiene fuente: `p95VentaConsultaMs` es siempre `null` y el portal lo oculta.
"""

import asyncio
import logging
from collections.abc import Callable
from typing import Any
from uuid import UUID

import httpx
from solventa_common.errors import ErrorNegocio
from solventa_common.telemetry import RESULTADO_RECHAZADO, operacion

from bff_web.adapters.outbound.pagos_parametricos import (
    ErrorPagosParametricos,
    PagosParametricosCliente,
)
from bff_web.adapters.outbound.siniestros import SiniestrosCliente
from bff_web.application.descartes import servicio_no_disponible
from bff_web.application.siniestros import Analista

OPERACION = "parametrico.resumen"
CODIGOS_PASAN = {"SOLICITUD_INVALIDA": 422}
MENSAJE_SECUNDARIA = "parametricos: fuente secundaria no disponible; su campo va en null"
MAX_PAGOS = 20

logger = logging.getLogger("bff_web.parametricos")


def _entero(valor: Any) -> int | None:
    return valor if isinstance(valor, int) and not isinstance(valor, bool) else None


class Parametricos:
    def __init__(
        self,
        pagos_parametricos: PagosParametricosCliente,
        siniestros: SiniestrosCliente,
        metricas: Callable[[], Any] | None,
        cola: str,
        cluster: str | None,
        servicio: str | None,
        consumidores_maximos: int | None,
        secundaria_timeout_ms: int,
    ) -> None:
        self.pagos_parametricos = pagos_parametricos
        self.siniestros = siniestros
        self._metricas = metricas
        self._cola = cola
        self._cluster = cluster
        self._servicio = servicio
        self._maximos = consumidores_maximos
        self._timeout_s = secundaria_timeout_ms / 1000

    async def resumen(self, evento_origen: str | None, analista: Analista) -> dict[str, Any]:
        with operacion(OPERACION) as op:
            try:
                base = await self._principal(
                    self.pagos_parametricos.resumen(evento_origen, analista.id)
                )
            except ErrorNegocio as error:
                if error.estado_http < 500:
                    op.marcar(RESULTADO_RECHAZADO)
                raise
            if not isinstance(base, dict):
                raise servicio_no_disponible()
            pagos = base.get("ultimosPagos")
            filas = [p for p in pagos if isinstance(p, dict)] if isinstance(pagos, list) else []
            estados, metricas = await asyncio.gather(
                self._estados_de_pago(filas, analista), self._leer_metricas()
            )
            vista = dict(base)
            vista["enDlq"] = _entero(base.get("enDlq"))
            if vista["enDlq"] is None:
                vista["enDlq"] = metricas["dlq"]
            vista["cola"] = {
                "profundidad": metricas["profundidad"],
                "consumidoresActivos": metricas["tareas"],
                "consumidoresMaximos": self._maximos,
            }
            vista["p95VentaConsultaMs"] = None
            vista["ultimosPagos"] = [
                {**fila, "estadoPago": estados.get(str(fila.get("siniestroId")))} for fila in filas
            ]
            return vista

    async def origenes(self, limite: int, analista: Analista) -> Any:
        with operacion(OPERACION) as op:
            try:
                return await self._principal(self.pagos_parametricos.origenes(limite, analista.id))
            except ErrorNegocio as error:
                if error.estado_http < 500:
                    op.marcar(RESULTADO_RECHAZADO)
                raise

    @staticmethod
    async def _principal(llamada: Any) -> Any:
        try:
            return await llamada
        except ErrorPagosParametricos as error:
            codigo = error.cuerpo.get("codigo")
            if CODIGOS_PASAN.get(str(codigo)) != error.status:
                raise servicio_no_disponible() from error
            detalles = error.cuerpo.get("detalles")
            raise ErrorNegocio(
                str(codigo),
                str(error.cuerpo.get("mensaje") or "Solicitud rechazada"),
                error.status,
                detalles if isinstance(detalles, dict) else {},
            ) from error
        except (httpx.HTTPError, ValueError) as error:
            raise servicio_no_disponible() from error

    async def _estados_de_pago(
        self, filas: list[dict[str, Any]], analista: Analista
    ) -> dict[str, str]:
        """`siniestroId` → `estadoPago` (de `siniestros`); vacío si no responde: cada fila queda null."""
        ids: list[UUID] = []
        for fila in filas[:MAX_PAGOS]:
            try:
                ids.append(UUID(str(fila.get("siniestroId"))))
            except ValueError:
                continue
        if not ids:
            return {}
        try:
            cuerpo = await asyncio.wait_for(
                self.siniestros.pagos_parametricos(ids, analista.id, analista.nombre),
                self._timeout_s,
            )
            items = cuerpo["items"]
            return {str(i["siniestroId"]): str(i["estadoPago"]) for i in items}
        except Exception as error:  # noqa: BLE001 - fuente secundaria: nunca un 503
            logger.warning(
                MENSAJE_SECUNDARIA,
                extra={"fuente": "siniestros", "error_type": type(error).__name__},
            )
            return {}

    async def _leer_metricas(self) -> dict[str, int | None]:
        """CloudWatch: profundidad, DLQ y tareas; todo `None` si falla o no hay permiso."""
        vacio: dict[str, int | None] = {"profundidad": None, "dlq": None, "tareas": None}
        if self._metricas is None:
            return vacio
        try:
            lector = self._metricas()
            leidas: dict[str, int | None] = await asyncio.wait_for(
                asyncio.to_thread(
                    lector.leer, self._cola, f"{self._cola}-dlq", self._cluster, self._servicio
                ),
                self._timeout_s,
            )
            return {**vacio, **leidas}
        except Exception as error:  # noqa: BLE001 - sin permiso o sin métrica: null (SYNC-01)
            logger.warning(
                MENSAJE_SECUNDARIA,
                extra={"fuente": "cloudwatch", "error_type": type(error).__name__},
            )
            return vacio
