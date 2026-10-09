"""PuertoFuente sobre POST /v1/consultas de adaptador-datos (03 §B.3 paso 5).

No lanza por errores del proveedor ni del adaptador: los devuelve como estado de la fuente.
Solo se propaga la cancelación (asyncio.CancelledError).
"""

from datetime import datetime
from decimal import Decimal
from time import perf_counter
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError
from pydantic.alias_generators import to_camel

from cotizacion.application.perfilamiento.puertos import Reloj
from cotizacion.domain.perfilamiento.modelos import (
    ContextoConsulta,
    EstadoCampo,
    EstadoFuente,
    ResultadoFuente,
    SolicitudFuente,
    ValorCampo,
)

# Margen sobre el deadline para que el corte lo haga adaptador-datos y llegue su 504.
MARGEN_HTTP_MS = 50
_ESTADO_POR_CODIGO = {504: EstadoFuente.TIEMPO_AGOTADO, 502: EstadoFuente.RESPUESTA_INVALIDA}


class _Dto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ValorCampoDto(_Dto):
    estado: Literal["DISPONIBLE", "NO_DISPONIBLE"]
    valor: Decimal | None


class RespuestaConsultaDto(_Dto):
    proveedor: str
    version_fuente: str
    confianza: Decimal
    consultada_en: datetime
    campos: dict[str, ValorCampoDto]


class AdaptadorDatosHttp:
    def __init__(self, cliente: httpx.AsyncClient, base_url: str, reloj: Reloj) -> None:
        self._cliente = cliente
        self._url = f"{base_url.rstrip('/')}/v1/consultas"
        # Disponible para quien necesite el instante de la consulta en cotizacion;
        # consultadaEn del linaje viene del adaptador (03 §B.5).
        self._reloj = reloj

    async def consultar(
        self, solicitud: SolicitudFuente, contexto: ContextoConsulta
    ) -> ResultadoFuente:
        inicio = perf_counter()
        try:
            respuesta = await self._cliente.post(
                self._url,
                json={
                    "fuente": str(solicitud.fuente),
                    "clienteId": str(contexto.cliente_id),
                    "consentimientoId": (
                        None
                        if contexto.consentimiento_id is None
                        else str(contexto.consentimiento_id)
                    ),
                    "proposito": contexto.proposito,
                    "camposAutorizados": list(solicitud.campos),
                },
                headers={"X-Deadline-Ms": str(contexto.deadline_ms)},
                timeout=(contexto.deadline_ms + MARGEN_HTTP_MS) / 1000,
            )
        except httpx.TimeoutException:
            return self._fallida(solicitud, EstadoFuente.TIEMPO_AGOTADO, inicio)
        except Exception:  # noqa: BLE001 - la rama nunca propaga; el estado lo explica
            return self._fallida(solicitud, EstadoFuente.NO_DISPONIBLE, inicio)
        if respuesta.status_code != 200:
            estado = _ESTADO_POR_CODIGO.get(respuesta.status_code, EstadoFuente.NO_DISPONIBLE)
            return self._fallida(solicitud, estado, inicio)
        try:
            cuerpo = RespuestaConsultaDto.model_validate_json(respuesta.content)
        except ValidationError:
            return self._fallida(solicitud, EstadoFuente.RESPUESTA_INVALIDA, inicio)
        return ResultadoFuente(
            fuente=solicitud.fuente,
            estado=EstadoFuente.CONSULTADA,
            campos_solicitados=solicitud.campos,
            campos={
                campo: ValorCampo(EstadoCampo(valor.estado), valor.valor)
                for campo, valor in cuerpo.campos.items()
                if campo in solicitud.campos
            },
            version_fuente=cuerpo.version_fuente,
            confianza=cuerpo.confianza,
            consultada_en=cuerpo.consultada_en,
            duracion_ms=_ms(inicio),
            proveedor=cuerpo.proveedor,
        )

    @staticmethod
    def _fallida(
        solicitud: SolicitudFuente, estado: EstadoFuente, inicio: float
    ) -> ResultadoFuente:
        return ResultadoFuente(
            fuente=solicitud.fuente,
            estado=estado,
            campos_solicitados=solicitud.campos,
            campos={},
            version_fuente=None,
            confianza=None,
            consultada_en=None,
            duracion_ms=_ms(inicio),
            proveedor=None,
        )


def _ms(inicio: float) -> int:
    return round((perf_counter() - inicio) * 1000)
