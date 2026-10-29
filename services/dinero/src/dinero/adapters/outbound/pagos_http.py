"""PagosPuerto sobre `POST /v1/cobros` de adaptador-pagos (HU-W16, 16.10; ADR 0013, decisión 5).

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

El monto viaja como texto decimal (`format(monto, "f")`, sin notación científica ni `float`).
Propaga `X-Correlation-Id` del movimiento. Nunca registra el pagador ni los cuerpos.
"""

import asyncio
from typing import Annotated, Literal
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from pydantic.alias_generators import to_camel
from solventa_common.correlation import CORRELATION_HEADER

from dinero.application.recaudo.puertos import (
    CobroAprobado,
    CobroIncierto,
    CobroRechazado,
    ResultadoCobro,
    SolicitudCobro,
)
from dinero.domain.recaudo.modelos import CODIGO, LARGO_TRANSACCION

TIEMPO_AGOTADO = "ADAPTADOR_TIEMPO_AGOTADO"
NO_DISPONIBLE = "ADAPTADOR_NO_DISPONIBLE"
RESPUESTA_INVALIDA = "ADAPTADOR_RESPUESTA_INVALIDA"

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
        try:
            async with asyncio.timeout(self._timeout_s):
                respuesta = await self._cliente.post(
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
        except (TimeoutError, httpx.TimeoutException):
            return CobroIncierto(TIEMPO_AGOTADO)
        except httpx.HTTPError:
            return CobroIncierto(NO_DISPONIBLE)
        if respuesta.status_code != 200:
            return CobroIncierto(_codigo_de(respuesta))
        try:
            cobro = _CobroDto.model_validate_json(respuesta.content)
        except ValidationError:
            return CobroIncierto(RESPUESTA_INVALIDA)
        if cobro.clave_recaudo != solicitud.clave_recaudo:
            return CobroIncierto(RESPUESTA_INVALIDA)
        if cobro.estado == "RECHAZADO":
            return CobroRechazado(motivo=cobro.motivo, transaccion_id=cobro.transaccion_id)
        if cobro.transaccion_id is None:
            return CobroIncierto(RESPUESTA_INVALIDA)
        return CobroAprobado(cobro.transaccion_id)
