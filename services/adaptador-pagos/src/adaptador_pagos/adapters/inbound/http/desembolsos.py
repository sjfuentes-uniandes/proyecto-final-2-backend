"""API /v1/desembolsos: ordena el pago de una indemnización y consulta su estado (HU-W23).

Solo la llama `dinero`. Mismo estilo que los cobros (HU-W16):

- La clave idempotente viaja en el cuerpo (`referencia`) y el adaptador la reenvía al aliado `pagos`
  como `Idempotency-Key`. La API del adaptador no lleva ese encabezado.
- La consulta lleva `beneficiarioRef`, como la de los cobros lleva `pagadorRef`: el aliado simulado
  no tiene memoria de las órdenes y elige el escenario por el beneficiario sintético (R-103).
- Mismas respuestas de error: 504 `TIEMPO_AGOTADO`, 503 `FUENTE_NO_DISPONIBLE` y 502
  `RESPUESTA_INVALIDA`, y 404 `DESEMBOLSO_NO_ENCONTRADO` en la consulta.
- Ningún dato de la cuenta del beneficiario: solo su referencia opaca.
"""

from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Path, Query, Request
from pydantic import Field, field_validator
from solventa_common.errors import ErrorNegocio

from adaptador_pagos.adapters.inbound.http.cobros import (
    _ERRORES_PROVEEDOR,
    _Dto,
    _error,
    _traducir_errores_del_proveedor,
)
from adaptador_pagos.application.desembolsos.consultar_desembolso import ConsultarDesembolso
from adaptador_pagos.application.desembolsos.desembolsar import Desembolsar
from adaptador_pagos.domain.desembolsos.modelos import (
    Desembolso,
    DesembolsoNoEncontrado,
    EstadoDesembolso,
)
from adaptador_pagos.domain.desembolsos.modelos import (
    SolicitudDesembolso as SolicitudDesembolsoDominio,
)

router = APIRouter(prefix="/v1/desembolsos", tags=["desembolsos"])

# `IND-<codigo>-NN` (DS3-11): la referencia única del pago de la indemnización.
REFERENCIA = r"^IND-88-[0-9]{4}-[0-9]{4,}-[0-9]{2}$"

Referencia = Annotated[str, Path(pattern=REFERENCIA)]
BeneficiarioRef = Annotated[str, Query(alias="beneficiarioRef", min_length=1, max_length=128)]


class SolicitudDesembolso(_Dto):
    referencia: Annotated[str, Field(pattern=REFERENCIA, examples=["IND-88-2026-0442-01"])]
    # Texto decimal, nunca número (CLAUDE.md): [0-9] y no \d, que admite dígitos Unicode.
    monto: Annotated[str, Field(pattern=r"^[0-9]+(\.[0-9]{1,2})?$", examples=["2500000.00"])]
    moneda: Literal["COP"]
    beneficiario_ref: Annotated[str, Field(min_length=1, max_length=128, examples=["ben-sint-001"])]

    @field_validator("monto")
    @classmethod
    def _mayor_que_cero(cls, valor: str) -> str:
        if Decimal(valor) <= 0:
            raise ValueError("monto debe ser mayor que 0")
        return valor


class DesembolsoDto(_Dto):
    referencia: str = Field(pattern=REFERENCIA)
    estado: EstadoDesembolso
    transaccion_id: str | None
    motivo: str | None

    @classmethod
    def de(cls, desembolso: Desembolso) -> "DesembolsoDto":
        return cls(
            referencia=desembolso.referencia,
            estado=desembolso.estado,
            transaccion_id=desembolso.transaccion_id,
            motivo=desembolso.motivo,
        )


@router.post(
    "",
    response_model=DesembolsoDto,
    response_model_by_alias=True,
    responses={
        400: _error("ERROR_HTTP: el cuerpo no se puede leer como JSON"),
        **_ERRORES_PROVEEDOR,
    },
)
async def ordenar(solicitud: SolicitudDesembolso, request: Request) -> DesembolsoDto:
    """Ordena el desembolso con `referencia` como clave idempotente hacia el proveedor."""
    caso: Desembolsar = request.app.state.desembolsar
    with _traducir_errores_del_proveedor():
        desembolso = await caso.ejecutar(
            SolicitudDesembolsoDominio(
                referencia=solicitud.referencia,
                monto=Decimal(solicitud.monto),
                moneda=solicitud.moneda,
                beneficiario_ref=solicitud.beneficiario_ref,
            )
        )
    return DesembolsoDto.de(desembolso)


@router.get(
    "/{referencia}",
    response_model=DesembolsoDto,
    response_model_by_alias=True,
    responses={
        404: _error("DESEMBOLSO_NO_ENCONTRADO: el proveedor no tiene esa orden de desembolso"),
        **_ERRORES_PROVEEDOR,
    },
)
async def consultar(
    referencia: Referencia, beneficiario_ref: BeneficiarioRef, request: Request
) -> DesembolsoDto:
    """Consulta el estado del desembolso en el proveedor, para conciliar una indemnización
    pendiente."""
    caso: ConsultarDesembolso = request.app.state.consultar_desembolso
    with _traducir_errores_del_proveedor():
        try:
            desembolso = await caso.ejecutar(referencia, beneficiario_ref)
        except DesembolsoNoEncontrado as error:
            raise ErrorNegocio(
                "DESEMBOLSO_NO_ENCONTRADO",
                "El proveedor no tiene una orden de desembolso con esa referencia",
                404,
            ) from error
    return DesembolsoDto.de(desembolso)
