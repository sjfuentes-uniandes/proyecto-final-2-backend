"""`Verificaciones` de H11 sobre la copia local de coberturas (HU-W21, W21-B12).

Lee del mensaje de la DLQ solo lo necesario (`tipoEvento`, `zona`, `observadoEn`, `eventoId` y, si
trae una, la póliza), busca las coberturas candidatas por índice y mira si el evento ya tiene un
siniestro. Lecturas cortas sobre el pool, sin llamadas a otros servicios. Si el mensaje no se puede
interpretar, o la base falla, devuelve None (el detalle sale sin verificación, como antes de
HU-W21): verificar nunca bloquea el reproceso.
"""

import logging
from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

import psycopg

from pagos_parametricos.adapters.outbound.coberturas_pg import (
    COLUMNAS,
    cobertura_de,
    leer_candidatas,
)
from pagos_parametricos.application.descartes import Reloj
from pagos_parametricos.domain.descarte import MensajeDescartado, leer
from pagos_parametricos.domain.parametrico.modelos import Cobertura
from pagos_parametricos.domain.parametrico.verificacion import verificar

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger("pagos_parametricos.verificaciones")
MENSAJE_FALLO = "verificacion: no se pudo consultar la copia de coberturas"

_POR_ID = f"SELECT {COLUMNAS} FROM coberturas_activas WHERE poliza_id = %s"
_POR_NUMERO = f"SELECT {COLUMNAS} FROM coberturas_activas WHERE numero = %s"
_PAGO_PREVIO = (
    "SELECT EXISTS (SELECT 1 FROM activaciones WHERE evento_id = %s AND siniestro_id IS NOT NULL)"
)


def _texto(valor: object) -> str | None:
    return valor if isinstance(valor, str) and valor else None


def _uuid(valor: str | None) -> UUID | None:
    try:
        return UUID(valor) if valor else None
    except ValueError:
        return None


def _instante(valor: object) -> datetime | None:
    if not isinstance(valor, str):
        return None
    try:
        instante = datetime.fromisoformat(valor)
    except ValueError:
        return None
    return instante if instante.tzinfo else None


class VerificacionesPg:
    """Implementa el puerto `Verificaciones` de `application/descartes.py`."""

    def __init__(self, pool: "AsyncConnectionPool[Any]", reloj: Reloj) -> None:
        self._pool = pool
        self._reloj = reloj

    async def verificar(self, mensaje: MensajeDescartado) -> Mapping[str, Any] | None:
        sobre = leer(mensaje.cuerpo)
        payload = sobre.get("payload") if sobre else None
        if not isinstance(payload, Mapping):
            return None
        tipo, zona = _texto(payload.get("tipoEvento")), _texto(payload.get("zona"))
        if mensaje.poliza is None and (tipo is None or zona is None):
            return None
        observado_en = _instante(payload.get("observadoEn")) or self._reloj.ahora()
        evento_id = _uuid(mensaje.evento_id)
        try:
            async with self._pool.connection() as conexion:
                candidatas = await self._candidatas(conexion, mensaje.poliza, tipo, zona)
                pago_previo = False
                if evento_id is not None:
                    cursor = await conexion.execute(_PAGO_PREVIO, (evento_id,))
                    fila = await cursor.fetchone()
                    pago_previo = bool(fila and fila[0])
        except (psycopg.Error, OSError) as error:
            logger.warning(MENSAJE_FALLO, extra={"error_type": type(error).__name__})
            return None
        return verificar(candidatas, observado_en, pago_previo)

    @staticmethod
    async def _candidatas(
        conexion: Any, poliza: str | None, tipo: str | None, zona: str | None
    ) -> list[Cobertura]:
        if poliza is not None:
            id_poliza = _uuid(poliza)
            cursor = await (
                conexion.execute(_POR_ID, (id_poliza,))
                if id_poliza
                else conexion.execute(_POR_NUMERO, (poliza,))
            )
            return [cobertura_de(fila) for fila in await cursor.fetchall()]
        assert tipo is not None and zona is not None  # lo comprueba `verificar`
        return await leer_candidatas(conexion, tipo, zona)
