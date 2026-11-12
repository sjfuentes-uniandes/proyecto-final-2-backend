"""Mantener la copia local de las coberturas a partir de los eventos `Poliza*` (HU-W21, DS3-22;
operación `parametrico.coberturas`).

En la transacción del inbox: bloquea la póliza, lee su cobertura, aplica la regla de orden (función
pura) y, si el evento avanza, guarda el resultado. Un evento atrasado se confirma igual (queda en
el inbox) y no cambia nada. Sin llamadas a otros servicios.
"""

import logging

from solventa_common.telemetry import operacion

from pagos_parametricos.application.parametrico.puertos import RepositorioCoberturas
from pagos_parametricos.domain.parametrico.orden import (
    EventoPoliza,
    PolizaEmitida,
    aplicar_evento_poliza,
)

OPERACION = "parametrico.coberturas"
APLICADA = "aplicada"
ATRASADA = "atrasada"

logger = logging.getLogger("pagos_parametricos.coberturas")


class AplicarEventoPoliza[T]:
    def __init__(self, coberturas: RepositorioCoberturas[T]) -> None:
        self._coberturas = coberturas

    async def ejecutar(self, evento: EventoPoliza, tx: T) -> str:
        """`aplicada` si la cobertura cambió; `atrasada` si el orden no avanza."""
        with operacion(OPERACION) as op:
            actual = await self._coberturas.bloquear_y_leer(tx, evento.poliza_id)
            nueva = aplicar_evento_poliza(actual, evento)
            if nueva is None:
                op.marcar(ATRASADA)
                op.anotar(
                    poliza_id=str(evento.poliza_id),
                    tipo="emitida" if isinstance(evento, PolizaEmitida) else "cancelada",
                )
                return ATRASADA
            await self._coberturas.guardar(tx, nueva)
            op.marcar(APLICADA)
            op.anotar(poliza_id=str(evento.poliza_id), estado=nueva.estado.value)
            return APLICADA
