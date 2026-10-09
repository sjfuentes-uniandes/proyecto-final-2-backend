"""PuertoIdentidad y PuertoConsentimiento sobre el contrato propuesto de clientes (01 §3.4.1).

En Sprint 1 la URL apunta al stub de simulador-aliados (DT-02). Una respuesta fuera del
contrato (estado o alcance desconocido) se trata como dependencia no disponible (03 §B.7).
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError
from pydantic.alias_generators import to_camel

from cotizacion.domain.perfilamiento.errores import ClienteNoEncontrado, DependenciaNoDisponible
from cotizacion.domain.perfilamiento.modelos import (
    DecisionConsentimiento,
    EstadoIdentidad,
    Fuente,
    VerificacionConsentimiento,
)

DEPENDENCIA_IDENTIDAD = "clientes.identidad"
DEPENDENCIA_CONSENTIMIENTO = "clientes.consentimiento"

CausaConsentimiento = Literal[
    "SIN_CONSENTIMIENTO", "REVOCADO", "VENCIDO", "PROPOSITO_NO_AUTORIZADO"
]


class _Dto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class IdentidadDto(_Dto):
    cliente_id: UUID
    estado: EstadoIdentidad
    verificado_en: datetime | None


class AlcanceRechazadoDto(_Dto):
    alcance: Fuente
    causa: Literal["ALCANCE_NO_AUTORIZADO"]


class VerificacionDto(_Dto):
    decision: DecisionConsentimiento
    consentimiento_id: UUID | None
    vigente_hasta: datetime | None
    alcances_permitidos: list[Fuente]
    alcances_rechazados: list[AlcanceRechazadoDto]
    causa: CausaConsentimiento | None

    def a_dominio(self) -> VerificacionConsentimiento:
        return VerificacionConsentimiento(
            decision=self.decision,
            consentimiento_id=self.consentimiento_id,
            vigente_hasta=_utc(self.vigente_hasta),
            alcances_permitidos=frozenset(self.alcances_permitidos),
            causa=self.causa,
        )


def _utc(instante: datetime | None) -> datetime | None:
    if instante is None:
        return None
    if instante.tzinfo is None:
        return instante.replace(tzinfo=UTC)
    return instante.astimezone(UTC)


class ClientesHttp:
    def __init__(self, cliente: httpx.AsyncClient, base_url: str, timeout_ms: int) -> None:
        self._cliente = cliente
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_ms / 1000

    async def _enviar(
        self, dependencia: str, metodo: str, ruta: str, cuerpo: object = None
    ) -> object:
        try:
            respuesta = await self._cliente.request(
                metodo, f"{self._base_url}{ruta}", json=cuerpo, timeout=self._timeout_s
            )
        except httpx.HTTPError as error:
            raise DependenciaNoDisponible(dependencia) from error
        if respuesta.status_code == 404:
            raise ClienteNoEncontrado()
        if respuesta.status_code != 200:
            raise DependenciaNoDisponible(dependencia)
        try:
            return respuesta.json()
        except ValueError as error:
            raise DependenciaNoDisponible(dependencia) from error

    async def estado(self, cliente_id: UUID) -> EstadoIdentidad:
        cuerpo = await self._enviar(
            DEPENDENCIA_IDENTIDAD, "GET", f"/v1/clientes/{cliente_id}/identidad"
        )
        try:
            return IdentidadDto.model_validate(cuerpo).estado
        except ValidationError as error:
            raise DependenciaNoDisponible(DEPENDENCIA_IDENTIDAD) from error

    async def verificar(
        self, cliente_id: UUID, proposito: str, alcances: Sequence[Fuente]
    ) -> VerificacionConsentimiento:
        cuerpo = await self._enviar(
            DEPENDENCIA_CONSENTIMIENTO,
            "POST",
            f"/v1/clientes/{cliente_id}/consentimientos/verificaciones",
            {"proposito": proposito, "alcances": [str(a) for a in alcances]},
        )
        try:
            return VerificacionDto.model_validate(cuerpo).a_dominio()
        except ValidationError as error:
            raise DependenciaNoDisponible(DEPENDENCIA_CONSENTIMIENTO) from error
