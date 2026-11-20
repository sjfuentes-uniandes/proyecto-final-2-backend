"""De la activación al siniestro (HU-W22, DS3-23). Puro: sin E/S, sin reloj y sin HTTP.

Una activación `PENDIENTE` (W21) trae el valor, la moneda y la versión de la regla **tal como estaban
al evaluar**; el resto de lo que pide `POST /v1/siniestros/parametricos` sale de la cobertura (el
titular, el número, el ramo y la ventana) y del evento (la medición). La clave del caso, y la
`Idempotency-Key`, es `<eventoId>:<polizaId>`.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Final
from uuid import UUID

from pagos_parametricos.domain.parametrico.modelos import EventoObservado
from pagos_parametricos.domain.parametrico.ventana import COLOMBIA

# La regla de DS3-21 que activa un pago: `valorObservado ≥ umbral` (con la póliza activa, en su
# ventana y zona). Es el código que `siniestros` guarda y el portal traduce.
REGLA_UMBRAL: Final = "VALOR_IGUAL_O_SUPERIOR_AL_UMBRAL"
ESTADO_SINIESTRO_CREADO: Final = "SINIESTRO_CREADO"
ESTADO_PENDIENTE: Final = "PENDIENTE"


class ActivacionIncompleta(Exception):
    """La cobertura no guarda lo que el siniestro necesita (el titular, el ramo, el número o la
    ventana): es una cobertura anterior a la migración 003 y hay que volver a publicar su
    `PolizaEmitida`. Excepción marcadora: no lleva datos."""


def clave_idempotente(evento_id: UUID, poliza_id: UUID) -> str:
    """`<eventoId>:<polizaId>`: la `Idempotency-Key` y la clave única del caso."""
    return f"{evento_id}:{poliza_id}"


@dataclass(frozen=True, slots=True)
class ActivacionPendiente:
    """Una activación `PENDIENTE` junto con la cobertura de su póliza."""

    evento_id: UUID
    poliza_id: UUID
    valor_pago: Decimal
    moneda: str
    version_regla: str
    cliente_id: UUID | None
    numero: str | None
    ramo: str | None
    umbral: Decimal | None
    vigente_desde: datetime | None
    vigente_hasta: datetime | None


@dataclass(frozen=True, slots=True)
class Disparador:
    """Lo que activó el pago (H09): la medición contra el umbral contratado."""

    tipo_evento: str
    valor_observado: str
    umbral: str
    unidad: str


@dataclass(frozen=True, slots=True)
class SolicitudSiniestro:
    """El cuerpo de `POST /v1/siniestros/parametricos`."""

    clave: str
    evento_id: UUID
    poliza_id: UUID
    cliente_id: UUID
    numero: str
    ramo: str
    vigencia_desde: date
    vigencia_hasta: date
    moneda: str
    evento: EventoObservado
    umbral: str
    regla: str
    version_regla: str
    valor: str

    @property
    def disparador(self) -> Disparador:
        return Disparador(
            tipo_evento=self.evento.tipo_evento,
            valor_observado=format(self.evento.valor_observado, "f"),
            umbral=self.umbral,
            unidad=self.evento.unidad,
        )


@dataclass(frozen=True, slots=True)
class SiniestroCreado:
    """La respuesta de `siniestros`: el siniestro de la activación y si esta llamada lo creó (201) o
    ya existía (200)."""

    siniestro_id: UUID
    codigo: str
    creado: bool


def vigencia_de_ventana(desde: datetime, hasta: datetime) -> tuple[date, date]:
    """Las fechas inclusivas de la póliza, a partir de su ventana `[desde, hasta)` en UTC (R-150):
    la fecha de Colombia de `desde` y la del día anterior a `hasta`."""
    inicio = desde.astimezone(COLOMBIA).date()
    fin = (hasta.astimezone(COLOMBIA) - timedelta(days=1)).date()
    return inicio, fin


def armar_solicitud(evento: EventoObservado, activacion: ActivacionPendiente) -> SolicitudSiniestro:
    """La solicitud de la activación. `ActivacionIncompleta` si la cobertura no trae el titular, el
    número, el ramo, el umbral o la ventana."""
    if (
        activacion.cliente_id is None
        or activacion.numero is None
        or activacion.ramo is None
        or activacion.umbral is None
        or activacion.vigente_desde is None
        or activacion.vigente_hasta is None
    ):
        raise ActivacionIncompleta
    desde, hasta = vigencia_de_ventana(
        activacion.vigente_desde.astimezone(UTC), activacion.vigente_hasta.astimezone(UTC)
    )
    return SolicitudSiniestro(
        clave=clave_idempotente(activacion.evento_id, activacion.poliza_id),
        evento_id=activacion.evento_id,
        poliza_id=activacion.poliza_id,
        cliente_id=activacion.cliente_id,
        numero=activacion.numero,
        ramo=activacion.ramo,
        vigencia_desde=desde,
        vigencia_hasta=hasta,
        moneda=activacion.moneda,
        evento=evento,
        umbral=format(activacion.umbral, "f"),
        regla=REGLA_UMBRAL,
        version_regla=activacion.version_regla,
        valor=format(activacion.valor_pago, "f"),
    )
