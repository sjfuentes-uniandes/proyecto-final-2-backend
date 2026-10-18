"""Verificación del consentimiento antes de consultar datos financieros (HU-W31).

Servicio a servicio: la invoca `cotizacion` en el perfilamiento, con el `clienteId` en la ruta
(ADR 0006) y, como `GET /v1/clientes/{clienteId}/identidad`, sin encabezados de actor. Cumple sin
cambios lo que `cotizacion` espera (su pacto `contracts/pact/cotizacion-clientes.json`).
La respuesta no lleva valores financieros: solo la decisión, identificadores y códigos.
"""

from datetime import datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Path, Request
from pydantic import Field
from solventa_common import correlation
from solventa_common.errors import ErrorNegocio

from clientes.adapters.inbound.http.comun import Dto, error
from clientes.application.consentimientos.casos import VerificarConsentimiento
from clientes.domain.consentimientos.verificacion import (
    CausaAlcance,
    CausaRechazo,
    Decision,
    Verificacion,
)
from clientes.domain.identidad.modelos import ClienteNoEncontrado

router = APIRouter(prefix="/v1/clientes", tags=["consentimientos"])


class Proposito(StrEnum):
    """Los de `PROPOSITOS` del dominio."""

    PERFILAMIENTO_PRECIO = "PERFILAMIENTO_PRECIO"


class Alcance(StrEnum):
    """Los de `ALCANCES` del dominio (ADR 0001)."""

    FA_PRODUCTOS_VIGENTES = "FA_PRODUCTOS_VIGENTES"
    FA_HISTORIAL_PAGOS_12M = "FA_HISTORIAL_PAGOS_12M"
    FA_INGRESOS_AGREGADOS = "FA_INGRESOS_AGREGADOS"
    DA_REGISTROS_PUBLICOS = "DA_REGISTROS_PUBLICOS"


class SolicitudVerificacionConsentimiento(Dto):
    proposito: Proposito
    alcances: Annotated[list[Alcance], Field(min_length=1)]


class AlcanceRechazadoDto(Dto):
    alcance: Alcance
    causa: CausaAlcance


class VerificacionConsentimientoDto(Dto):
    """`consentimientoId` y `vigenteHasta` son nulos en un `RECHAZADO`; `causa`, solo en él."""

    decision: Decision
    consentimiento_id: UUID | None
    vigente_hasta: datetime | None
    alcances_permitidos: list[Alcance]
    alcances_rechazados: list[AlcanceRechazadoDto]
    causa: CausaRechazo | None

    @classmethod
    def desde(cls, verificacion: Verificacion) -> "VerificacionConsentimientoDto":
        return cls(
            decision=verificacion.decision,
            consentimiento_id=verificacion.consentimiento_id,
            vigente_hasta=verificacion.vigente_hasta,
            alcances_permitidos=[Alcance(a) for a in verificacion.alcances_permitidos],
            alcances_rechazados=[
                AlcanceRechazadoDto(alcance=Alcance(a), causa=CausaAlcance.ALCANCE_NO_AUTORIZADO)
                for a in verificacion.alcances_rechazados
            ],
            causa=verificacion.causa,
        )


ClienteId = Annotated[UUID, Path(alias="clienteId")]


@router.post(
    "/{clienteId}/consentimientos/verificaciones",
    response_model=VerificacionConsentimientoDto,
    response_model_by_alias=True,
    summary="Verifica el consentimiento vigente del cliente para un propósito y unos alcances",
    responses={
        400: error("ERROR_HTTP: el cuerpo no se puede leer como JSON"),
        404: error("CLIENTE_NO_ENCONTRADO: el cliente no existe; no se registra ninguna decisión"),
        422: error("SOLICITUD_INVALIDA: propósito o alcances ausentes o no soportados"),
    },
)
async def verificar(
    cliente_id: ClienteId, solicitud: SolicitudVerificacionConsentimiento, request: Request
) -> VerificacionConsentimientoDto:
    """Sin caché (ADR 0011): cada llamada lee el consentimiento y registra la decisión en el
    outbox (`VerificacionConsentimientoRegistrada`). Un fallo técnico responde 5xx y quien
    verifica falla cerrado."""
    caso: VerificarConsentimiento = request.app.state.verificar_consentimiento
    try:
        verificacion = await caso.ejecutar(
            cliente_id,
            solicitud.proposito.value,
            [a.value for a in solicitud.alcances],
            correlation_id=correlation.obtener() or "",
        )
    except ClienteNoEncontrado as falla:
        raise ErrorNegocio("CLIENTE_NO_ENCONTRADO", "El cliente no existe", 404) from falla
    return VerificacionConsentimientoDto.desde(verificacion)
