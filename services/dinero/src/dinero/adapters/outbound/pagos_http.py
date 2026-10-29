"""PagosPuerto sobre `POST /v1/cobros` (16.10) y `GET /v1/cobros/{claveRecaudo}?pagadorRef` (16.11,
la consulta de la conciliación) de adaptador-pagos (HU-W16; ADR 0013, decisión 5).

Solo un 200 con el `CobroDto` de `contracts/openapi/adaptador-pagos.yaml` y la misma `claveRecaudo`
es una respuesta definitiva (APROBADO con `transaccionId`, o RECHAZADO). Todo lo demás es incierto
y nunca lanza:

- otro estado (502, 503, 504, un 422 inesperado, 3xx...): el `codigo` de su cuerpo si tiene forma
  de código; si no, `HTTP_<estado>`;
- sin respuesta a tiempo: `ADAPTADOR_TIEMPO_AGOTADO`; error de conexión o de protocolo:
  `ADAPTADOR_NO_DISPONIBLE`; un 200 fuera de contrato: `ADAPTADOR_RESPUESTA_INVALIDA`. Son códigos
  internos del movimiento (`ultimo_incierto`), no del catálogo de errores: dinero no expone API.

El timeout (6,5 s por defecto) supera al del adaptador hacia el proveedor (5 s): así llega su
respuesta definitiva o su 504. Se aplica por fase en HTTPX y como corte total de la llamada. Sin
reintentos: la incertidumbre la resuelve la conciliación (16.11) con la misma `claveRecaudo`.

La consulta responde igual que el cobro, con una respuesta más: `404 COBRO_NO_ENCONTRADO` (con ese
código en el cuerpo) es `CobroNoEncontrado`, que la conciliación reintenta con la misma clave. Un
404 sin ese código es incierto, como en el cobro.

El monto viaja como texto decimal (`format(monto, "f")`, sin notación científica ni `float`).
Propaga `X-Correlation-Id` del movimiento. Nunca registra el pagador ni los cuerpos.
"""

import asyncio
from collections.abc import Coroutine
from typing import Annotated, Any, Literal
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from pydantic.alias_generators import to_camel
from solventa_common.correlation import CORRELATION_HEADER

from dinero.application.recaudo.puertos import (
    CobroAprobado,
    CobroIncierto,
    CobroNoEncontrado,
    CobroRechazado,
    ConsultaCobro,
    ResultadoCobro,
    ResultadoConsulta,
    SolicitudCobro,
)
from dinero.domain.recaudo.modelos import CODIGO, LARGO_TRANSACCION

TIEMPO_AGOTADO = "ADAPTADOR_TIEMPO_AGOTADO"
NO_DISPONIBLE = "ADAPTADOR_NO_DISPONIBLE"
RESPUESTA_INVALIDA = "ADAPTADOR_RESPUESTA_INVALIDA"
# El código del catálogo que adaptador-pagos responde con 404 en la consulta.
COBRO_NO_ENCONTRADO = "COBRO_NO_ENCONTRADO"

Transaccion = Annotated[str, Field(min_length=1, max_length=LARGO_TRANSACCION)]


class _CobroDto(BaseModel):
    """`CobroDto` del contrato, estricto: lo que no cumple es un incierto, no un rechazo."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, strict=True)

    clave_recaudo: Annotated[UUID, Field(strict=False)]
    estado: Literal["APROBADO", "RECHAZADO"]
    transaccion_id: Transaccion | None
    motivo: str | None

    @field_validator("transaccion_id")
    @classmethod
    def _no_en_blanco(cls, valor: str | None) -> str | None:
        if valor is not None and not valor.strip():
            raise ValueError("transaccionId en blanco")
        return valor


class _Error(BaseModel):
    codigo: Annotated[str, Field(pattern=CODIGO.pattern)]


def _codigo_de(respuesta: httpx.Response) -> str:
    try:
        return _Error.model_validate_json(respuesta.content).codigo
    except ValidationError:
        return f"HTTP_{respuesta.status_code}"


class AdaptadorPagosHttp:
    def __init__(self, cliente: httpx.AsyncClient, base_url: str, timeout_ms: int) -> None:
        self._cliente = cliente
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_ms / 1000

    async def cobrar(self, solicitud: SolicitudCobro) -> ResultadoCobro:
        respuesta = await self._llamar(
            self._cliente.post(
                f"{self._base_url}/v1/cobros",
                json={
                    "claveRecaudo": str(solicitud.clave_recaudo),
                    "monto": format(solicitud.monto, "f"),
                    "moneda": solicitud.moneda,
                    "pagadorRef": str(solicitud.pagador_ref),
                },
                headers={CORRELATION_HEADER: solicitud.correlation_id},
                timeout=self._timeout_s,
            )
        )
        if isinstance(respuesta, CobroIncierto):
            return respuesta
        return _resultado(respuesta, solicitud.clave_recaudo)

    async def consultar(self, consulta: ConsultaCobro) -> ResultadoConsulta:
        respuesta = await self._llamar(
            self._cliente.get(
                f"{self._base_url}/v1/cobros/{consulta.clave_recaudo}",
                params={"pagadorRef": str(consulta.pagador_ref)},
                headers={CORRELATION_HEADER: consulta.correlation_id},
                timeout=self._timeout_s,
            )
        )
        if isinstance(respuesta, CobroIncierto):
            return respuesta
        if respuesta.status_code == 404 and _codigo_de(respuesta) == COBRO_NO_ENCONTRADO:
            return CobroNoEncontrado()
        return _resultado(respuesta, consulta.clave_recaudo)

    async def _llamar(
        self, peticion: Coroutine[Any, Any, httpx.Response]
    ) -> httpx.Response | CobroIncierto:
        """La respuesta, o el incierto si no llegó a tiempo o no hubo conexión."""
        try:
            async with asyncio.timeout(self._timeout_s):
                return await peticion
        except (TimeoutError, httpx.TimeoutException):
            return CobroIncierto(TIEMPO_AGOTADO)
        except httpx.HTTPError:
            return CobroIncierto(NO_DISPONIBLE)


def _resultado(respuesta: httpx.Response, clave_recaudo: UUID) -> ResultadoCobro:
    """Solo un 200 con el `CobroDto` de la misma clave es definitivo."""
    if respuesta.status_code != 200:
        return CobroIncierto(_codigo_de(respuesta))
    try:
        cobro = _CobroDto.model_validate_json(respuesta.content)
    except ValidationError:
        return CobroIncierto(RESPUESTA_INVALIDA)
    if cobro.clave_recaudo != clave_recaudo:
        return CobroIncierto(RESPUESTA_INVALIDA)
    if cobro.estado == "RECHAZADO":
        return CobroRechazado(motivo=cobro.motivo, transaccion_id=cobro.transaccion_id)
    if cobro.transaccion_id is None:
        return CobroIncierto(RESPUESTA_INVALIDA)
    return CobroAprobado(cobro.transaccion_id)
