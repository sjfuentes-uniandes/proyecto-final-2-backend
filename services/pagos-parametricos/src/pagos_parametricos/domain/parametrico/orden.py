"""Orden de los eventos de póliza (HU-W21, DS3-22): `coberturas_activas` solo avanza con un orden
mayor, para que una cancelación atrasada no reactive la cobertura y una emisión atrasada no
reactive una cancelada.

El orden es el `aggregateVersion` del sobre si el evento y la fila guardada lo traen; si no, el
`occurredAt` (CF-S3-11: `polizas` no publica `aggregateVersion`). Gana el más reciente y un empate
no cambia el estado. Si uno trae versión y el otro no, se comparan los instantes. Se reutiliza
`solventa_common.versionado.avanza`; `leer_version` no, porque lanza `EventoInvalido` cuando el
sobre no trae versión y mandaría todas las `PolizaEmitida` a la DLQ.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from solventa_common.versionado import avanza

from pagos_parametricos.domain.parametrico.modelos import Cobertura, EstadoCobertura, Regla

_EPOCA = datetime(1970, 1, 1, tzinfo=UTC)
_MICROSEGUNDO = timedelta(microseconds=1)


@dataclass(frozen=True, slots=True)
class PolizaEmitida:
    """`PolizaEmitida` con `coberturaParametrica`: crea o reemplaza la cobertura."""

    poliza_id: UUID
    numero: str
    regla: Regla
    occurred_at: datetime
    aggregate_version: int | None = None


@dataclass(frozen=True, slots=True)
class PolizaCancelada:
    """`PolizaCancelada` (sintética mientras `polizas` no la publique): deja la cobertura
    cancelada."""

    poliza_id: UUID
    occurred_at: datetime
    aggregate_version: int | None = None


type EventoPoliza = PolizaEmitida | PolizaCancelada


def _microsegundos(instante: datetime) -> int:
    return (instante.astimezone(UTC) - _EPOCA) // _MICROSEGUNDO


def avanza_orden(actual: Cobertura | None, evento: EventoPoliza) -> bool:
    """True si `evento` es más reciente que lo ya aplicado (sin estado previo, siempre)."""
    if actual is None:
        return True
    if actual.aggregate_version is not None and evento.aggregate_version is not None:
        return avanza(actual.aggregate_version, evento.aggregate_version)
    # Un instante igual no avanza: `avanza` pide estrictamente mayor.
    return avanza(_microsegundos(actual.ultimo_evento_en), _microsegundos(evento.occurred_at))


def aplicar_evento_poliza(actual: Cobertura | None, evento: EventoPoliza) -> Cobertura | None:
    """La cobertura resultante, o None si el evento está atrasado (no cambia nada)."""
    if not avanza_orden(actual, evento):
        return None
    match evento:
        case PolizaEmitida():
            return Cobertura(
                poliza_id=evento.poliza_id,
                estado=EstadoCobertura.ACTIVA,
                regla=evento.regla,
                ultimo_evento_en=evento.occurred_at,
                aggregate_version=evento.aggregate_version,
                numero=evento.numero,
            )
        case PolizaCancelada():
            return Cobertura(
                poliza_id=evento.poliza_id,
                estado=EstadoCobertura.CANCELADA,
                # Se conserva lo que ya se sabía de la póliza; una cancelación que llegó primero no
                # sabe nada.
                regla=actual.regla if actual else None,
                ultimo_evento_en=evento.occurred_at,
                aggregate_version=evento.aggregate_version,
                numero=actual.numero if actual else None,
            )
