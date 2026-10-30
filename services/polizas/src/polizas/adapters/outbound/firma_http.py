"""FirmaPuerto sobre `POST /v1/firmas` de adaptador-firma (HU-W17, 17.8; ADR 0014, decisión 5).

Solo un 200 con el `FirmaDto` de `contracts/openapi/adaptador-firma.yaml`, el mismo `documentoId`
(comparado como uuid) y la misma huella (texto exacto) es una firma. Todo lo demás es `FalloFirma`,
con un código de la póliza y no el del catálogo del adaptador:

- sin respuesta a tiempo, o su 504 (el proveedor no respondió): `FIRMA_TIEMPO_AGOTADO`;
- sin conexión, su 503 u otro 5xx: `FIRMA_NO_DISPONIBLE`;
- su 502, cualquier otro estado o un 200 fuera de contrato: `FIRMA_RESPUESTA_INVALIDA`.

El timeout (6,5 s por defecto) supera al del adaptador hacia el proveedor (5 s): así llega su firma
o su 504. Se aplica por fase en HTTPX y como corte total (`SinProteccion(corte_duro_ms)`: una firma
no cabe en los 700 ms de `resiliencia`). Sin reintentos: la emisión sigue EMITIENDO y la tarea
emisora reintenta con la misma `Idempotency-Key` (el `documentoId`, que el adaptador fija).

Propaga `X-Correlation-Id` del recaudo. Nunca registra el firmante, la huella ni los cuerpos.
"""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, field_validator
from pydantic.alias_generators import to_camel
from solventa_common.correlation import CORRELATION_HEADER
from solventa_common.resiliencia import (
    DependenciaNoDisponible,
    Proteccion,
    SinProteccion,
    TiempoAgotado,
)

from polizas.application.poliza.puertos import FirmaObtenida, SolicitudFirma
from polizas.domain.poliza.errores import FalloFirma

OPERACION = "emision.firma"
GRUPO = "adaptador-firma"


class _FirmaDto(BaseModel):
    """`FirmaDto` del contrato, estricto: lo que no cumple es una respuesta inválida."""

    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, strict=True, extra="ignore"
    )

    documento_id: Annotated[UUID, Field(strict=False)]
    estado: Literal["FIRMADO"]
    firma_id: Annotated[str, Field(min_length=1)]
    sello_tiempo: Annotated[AwareDatetime, Field(strict=False)]
    huella: str

    @field_validator("firma_id")
    @classmethod
    def _no_en_blanco(cls, valor: str) -> str:
        if not valor.strip():
            raise ValueError("firmaId en blanco")
        return valor


def _fallo_por_estado(estado: int) -> FalloFirma:
    if estado == 504:
        return FalloFirma(FalloFirma.TIEMPO_AGOTADO)
    if estado >= 500 and estado != 502:
        return FalloFirma(FalloFirma.NO_DISPONIBLE)
    return FalloFirma(FalloFirma.RESPUESTA_INVALIDA)


class AdaptadorFirmaHttp:
    def __init__(
        self,
        cliente: httpx.AsyncClient,
        base_url: str,
        timeout_ms: int,
        proteccion: Proteccion | None = None,
    ) -> None:
        self._cliente = cliente
        self._base_url = base_url.rstrip("/")
        self._timeout_ms = timeout_ms
        self._proteccion = proteccion or SinProteccion(corte_duro_ms=timeout_ms)

    async def firmar(self, solicitud: SolicitudFirma) -> FirmaObtenida:
        try:
            respuesta = await self._proteccion.ejecutar(
                OPERACION,
                GRUPO,
                lambda timeout_s: self._llamar(solicitud, timeout_s),
                self._timeout_ms,
                None,
            )
        except TiempoAgotado as error:
            raise FalloFirma(FalloFirma.TIEMPO_AGOTADO) from error
        except DependenciaNoDisponible as error:
            raise FalloFirma(FalloFirma.NO_DISPONIBLE) from error
        return _firma(respuesta, solicitud)

    async def _llamar(self, solicitud: SolicitudFirma, timeout_s: float) -> httpx.Response:
        return await self._cliente.post(
            f"{self._base_url}/v1/firmas",
            json={
                "documentoId": str(solicitud.documento_id),
                "huella": solicitud.huella,
                "firmanteRef": str(solicitud.firmante_ref),
            },
            headers={CORRELATION_HEADER: solicitud.correlation_id},
            timeout=timeout_s,
        )


def _firma(respuesta: httpx.Response, solicitud: SolicitudFirma) -> FirmaObtenida:
    """Solo un 200 con el `FirmaDto` del mismo documento y la misma huella es la firma."""
    if respuesta.status_code != 200:
        raise _fallo_por_estado(respuesta.status_code)
    try:
        dto = _FirmaDto.model_validate_json(respuesta.content)
    except ValidationError as error:
        raise FalloFirma(FalloFirma.RESPUESTA_INVALIDA) from error
    if dto.documento_id != solicitud.documento_id or dto.huella != solicitud.huella:
        raise FalloFirma(FalloFirma.RESPUESTA_INVALIDA)
    sello: datetime = dto.sello_tiempo
    return FirmaObtenida(
        documento_id=dto.documento_id,
        huella=dto.huella,
        firma_id=dto.firma_id,
        sello_tiempo=sello,
    )
