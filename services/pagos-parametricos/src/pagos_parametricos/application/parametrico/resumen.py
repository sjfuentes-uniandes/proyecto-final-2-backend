"""Seguimiento del procesamiento paramétrico para H09 (HU-W22, W22-B09; operación
`parametrico.resumen`).

Solo lectura, sobre los índices por `evento_origen` y `evaluado_en` (DS3-19): una consulta no recorre
el resto de la tabla. Definiciones (R-180):

- `procesados`: eventos distintos ya evaluados del origen; `duplicados`: entregas repetidas que se
  descartaron sin cambiar la decisión ni crear otro siniestro; `recibidos` = `procesados +
  duplicados`; `activados`: activaciones del origen, con o sin siniestro todavía.
- `enDlq`: la DLQ **completa** de `parametricos-eventos` (`GetQueueAttributes`); `None` si no se puede
  leer (sin cola configurada, sin permiso o con un error): el resumen no falla por eso.
- `ritmoPorMinuto`: los últimos 15 minutos completos (con ceros), del más antiguo al más reciente.
- `ultimosPagos`: hasta 10 activaciones **con siniestro**, la más reciente primero.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from solventa_common.telemetry import operacion

from pagos_parametricos.application.parametrico.puertos import Reloj
from pagos_parametricos.domain.parametrico.activacion import Disparador

OPERACION = "parametrico.resumen"
MINUTOS_DE_RITMO = 15
MAX_ULTIMOS = 10
MENSAJE_DLQ = "resumen: no se pudo leer la DLQ; enDlq queda en null"

logger = logging.getLogger("pagos_parametricos.resumen")


@dataclass(frozen=True, slots=True)
class PagoActivado:
    siniestro_id: UUID
    siniestro_codigo: str
    poliza_id: UUID
    poliza_numero: str | None
    disparador: Disparador
    valor: str
    moneda: str
    estado_activacion: str


@dataclass(frozen=True, slots=True)
class MinutoDeRitmo:
    minuto: datetime
    procesados: int


@dataclass(frozen=True, slots=True)
class Resumen:
    evento_origen: str | None
    desde: datetime | None
    recibidos: int
    procesados: int
    activados: int
    duplicados: int
    en_dlq: int | None
    ritmo_por_minuto: tuple[MinutoDeRitmo, ...]
    ultimos_pagos: tuple[PagoActivado, ...]


@dataclass(frozen=True, slots=True)
class OrigenReciente:
    evento_origen: str
    desde: datetime
    hasta: datetime
    eventos: int


@dataclass(frozen=True, slots=True)
class DatosDeOrigen:
    """Lo que lee la base de un origen (sin el ritmo completado ni la DLQ)."""

    desde: datetime | None
    procesados: int
    duplicados: int
    activados: int
    por_minuto: dict[datetime, int]
    ultimos_pagos: tuple[PagoActivado, ...]


class LecturaResumen(Protocol):
    async def origen_mas_reciente(self) -> str | None:
        """El `evento_origen` de la evaluación más reciente; None si no hay eventos."""
        ...

    async def datos(self, evento_origen: str, desde_ritmo: datetime) -> DatosDeOrigen:
        """Los conteos del origen, los eventos evaluados por minuto desde `desde_ritmo` y los
        últimos pagos con siniestro."""
        ...

    async def origenes(self, limite: int) -> list[OrigenReciente]: ...


class ProfundidadDlq(Protocol):
    async def profundidad(self) -> int:
        """Mensajes en la DLQ de `parametricos-eventos`."""
        ...


def _minuto(instante: datetime) -> datetime:
    return instante.replace(second=0, microsecond=0)


def ritmo_completo(por_minuto: dict[datetime, int], ahora: datetime) -> tuple[MinutoDeRitmo, ...]:
    """Los últimos 15 minutos completos (con ceros), del más antiguo al más reciente."""
    actual = _minuto(ahora)
    inicio = actual - timedelta(minutes=MINUTOS_DE_RITMO - 1)
    return tuple(
        MinutoDeRitmo(m, por_minuto.get(m, 0))
        for m in (inicio + timedelta(minutes=i) for i in range(MINUTOS_DE_RITMO))
    )


class ConsultarResumen:
    def __init__(self, lectura: LecturaResumen, dlq: ProfundidadDlq | None, reloj: Reloj) -> None:
        self._lectura = lectura
        self._dlq = dlq
        self._reloj = reloj

    async def resumen(self, evento_origen: str | None) -> Resumen:
        with operacion(OPERACION):
            ahora = self._reloj.ahora()
            en_dlq = await self._profundidad_dlq()
            origen = evento_origen or await self._lectura.origen_mas_reciente()
            if origen is None:
                return Resumen(None, None, 0, 0, 0, 0, en_dlq, ritmo_completo({}, ahora), ())
            inicio = _minuto(ahora) - timedelta(minutes=MINUTOS_DE_RITMO - 1)
            datos = await self._lectura.datos(origen, inicio)
            return Resumen(
                evento_origen=origen,
                desde=datos.desde,
                recibidos=datos.procesados + datos.duplicados,
                procesados=datos.procesados,
                activados=datos.activados,
                duplicados=datos.duplicados,
                en_dlq=en_dlq,
                ritmo_por_minuto=ritmo_completo(datos.por_minuto, ahora),
                ultimos_pagos=datos.ultimos_pagos,
            )

    async def origenes(self, limite: int) -> list[OrigenReciente]:
        with operacion(OPERACION):
            return await self._lectura.origenes(limite)

    async def _profundidad_dlq(self) -> int | None:
        if self._dlq is None:
            return None
        try:
            return await self._dlq.profundidad()
        except Exception as error:  # noqa: BLE001 - sin permiso o sin cola: el resumen sigue
            logger.warning(MENSAJE_DLQ, extra={"error_type": type(error).__name__})
            return None
