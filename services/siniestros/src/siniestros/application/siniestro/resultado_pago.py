"""Reflejar el resultado del pago de la indemnización en el siniestro (HU-W23; ADR 0015; DS3-25).

Lo llama el consumidor de `siniestros-indemnizaciones` (modo (a) de `solventa_common.inbox`) en la
**misma transacción** que la fila del inbox: el siniestro se bloquea (`FOR UPDATE`), se escriben las
columnas `pago_*` y la entrada del historial inmutable (`PAGO_CONFIRMADO` o `PAGO_RECHAZADO`), y se
confirma todo junto. No reemite eventos (`consulta` proyecta los `Indemnizacion*` directamente).

- **El primer resultado gana.** Un `Confirmada` repetido o fuera de orden no cambia nada (ni
  columnas ni historial): resultado `duplicado`.
- Un siniestro que no existe o no está `APROBADO` no puede recibir un pago: se descarta con un aviso
  (`desconocido`) y el mensaje se consume (reintentarlo no lo arreglaría).
- La entrada del historial lleva la hora de la pasarela (`confirmadaEn` o `rechazadaEn`), no la del
  mensaje; el actor es el sistema de pagos. Solo códigos, la referencia y contadores: ni la cuenta,
  ni el nombre, ni la transacción en los logs.
"""

import logging
from enum import StrEnum
from uuid import uuid4

from solventa_common.telemetry import operacion

from siniestros.application.siniestro.puertos import TxSiniestros
from siniestros.domain.siniestro.modelos import EntradaHistorial, EstadoSiniestro
from siniestros.domain.siniestro.pago import EstadoPago, ResultadoPago, acepta_resultado

OPERACION = "siniestro.resultado_pago"
ROL_SISTEMA_PAGOS = "SISTEMA_PAGOS"
MENSAJE_DESCONOCIDO = "pago: el siniestro no existe o no está aprobado; se descarta el resultado"
MENSAJE_REFLEJADO = "pago: resultado reflejado en el siniestro"
MENSAJE_REPETIDO = "pago: resultado repetido o fuera de orden; no cambia nada"

logger = logging.getLogger("siniestros.pago")


class Reflejo(StrEnum):
    """El resultado de la operación `siniestro.resultado_pago` (contracts/operaciones.md)."""

    CONFIRMADO = "confirmado"
    RECHAZADO = "rechazado"
    DUPLICADO = "duplicado"
    DESCONOCIDO = "desconocido"


class RegistrarResultadoPago:
    async def registrar(self, resultado: ResultadoPago, tx: TxSiniestros) -> Reflejo:
        with operacion(OPERACION) as medicion:
            reflejo = await self._reflejar(resultado, tx)
            medicion.marcar(reflejo.value)
            medicion.anotar(
                siniestro_id=str(resultado.siniestro_id), referencia=resultado.referencia
            )
            return reflejo

    async def _reflejar(self, resultado: ResultadoPago, tx: TxSiniestros) -> Reflejo:
        caso = await tx.bloquear_siniestro(resultado.siniestro_id)
        if caso is None or caso.estado is not EstadoSiniestro.APROBADO:
            logger.warning(MENSAJE_DESCONOCIDO, extra={"siniestro_id": str(resultado.siniestro_id)})
            return Reflejo.DESCONOCIDO
        if not acepta_resultado(await tx.estado_pago(caso.id)):
            logger.info(
                MENSAJE_REPETIDO,
                extra={"siniestro_id": str(caso.id), "referencia": resultado.referencia},
            )
            return Reflejo.DUPLICADO
        await tx.guardar_pago(resultado)
        await tx.agregar_historial(
            EntradaHistorial(
                id=uuid4(),
                siniestro_id=caso.id,
                accion=resultado.accion,
                ocurrido_en=resultado.ocurrido_en,
                actor_rol=ROL_SISTEMA_PAGOS,
                detalles=_detalles(resultado),
            )
        )
        logger.info(
            MENSAJE_REFLEJADO,
            extra={
                "siniestro_id": str(caso.id),
                "referencia": resultado.referencia,
                "estado": resultado.estado.value,
            },
        )
        return (
            Reflejo.CONFIRMADO if resultado.estado is EstadoPago.CONFIRMADO else Reflejo.RECHAZADO
        )


def _detalles(resultado: ResultadoPago) -> dict[str, object]:
    """Parámetros de la entrada del historial: sin PII ni datos de la cuenta."""
    detalles: dict[str, object] = {
        "referencia": resultado.referencia,
        "intentos": resultado.intentos,
        "duplicadosDescartados": resultado.duplicados_descartados,
    }
    if resultado.transaccion_id is not None:
        detalles["transaccionId"] = resultado.transaccion_id
    if resultado.motivo is not None:
        detalles["motivo"] = resultado.motivo
    return detalles
