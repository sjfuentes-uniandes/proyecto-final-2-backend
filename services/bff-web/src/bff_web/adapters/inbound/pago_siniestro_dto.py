"""Vista de H08 de `bff-web` (HU-W23, W23-B04; copia del prototipo `bff_pago_dto.py`).

`PagoSiniestroVista` es lo que el portal necesita para H08 y sus variantes. Plan B (DS3-26, v1.2):
`bff-web` la arma **solo con `siniestros`** (el detalle del caso, que ya trae la liquidación, el
historial, la cuenta enmascarada y la proyección del pago que `siniestros` guarda desde los eventos
`Indemnizacion*`) más el nombre del cliente. En la sincronización final (§2.7, SYNC-01) la
`indemnizacion` pasa a componerse con `GET /v1/indemnizaciones?siniestroId=` de `dinero`, **sin
cambiar este contrato**: `IndemnizacionVista` solo lleva lo que ambas fuentes pueden llenar.

`indemnizacion` es null mientras `dinero` no ha publicado un resultado (la orden está en camino o
la pasarela no ha respondido): el portal muestra «Esperando confirmación de la pasarela» y oculta
intentos y duplicados. El portal presenta `RECHAZADO` como «Pago por gestionar» (AC4).
"""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import Field

from bff_web.adapters.inbound.siniestros_dto import (
    CodigoSiniestro,
    EntradaHistorial,
    EstadoPago,
    EstadoSiniestro,
    LiquidacionDto,
    TextoDecimal,
    _Dto,
)

ReferenciaPago = Annotated[str, Field(pattern=r"^IND-88-[0-9]{4}-[0-9]{4,}-[0-9]{2}$")]


class IndemnizacionVista(_Dto):
    """La indemnización con su resultado en la pasarela. Sin datos de la cuenta del beneficiario."""

    referencia: ReferenciaPago = Field(
        description="IND-<codigo>-01: la clave única e idempotente hacia la pasarela (CF-S3-07)"
    )
    estado: EstadoPago = Field(
        description="PENDIENTE, CONFIRMADO o RECHAZADO. Nunca SIN_PAGO. `dinero` dice CONFIRMADA y "
        "RECHAZADA: el BFF los presenta como CONFIRMADO y RECHAZADO, como `PagoDto`"
    )
    monto: TextoDecimal
    moneda: str = Field(pattern=r"^[A-Z]{3}$")
    transaccion_id: str | None = Field(
        description="Solo en CONFIRMADO: la confirmación de la pasarela"
    )
    motivo: str | None = Field(
        description="Solo en RECHAZADO: el código normalizado de la pasarela (el portal lo traduce)"
    )
    intentos: int = Field(ge=0, description="Llamadas efectivas de pago hechas a la pasarela")
    duplicados_descartados: int = Field(
        ge=0, description="Órdenes o confirmaciones repetidas descartadas sin otro movimiento"
    )
    confirmada_en: datetime | None = Field(description="Solo en CONFIRMADO")


class PagoSiniestroVista(_Dto):
    """H08: el pago de un siniestro aprobado con su trazabilidad."""

    siniestro_id: UUID
    codigo: CodigoSiniestro
    estado: EstadoSiniestro = Field(description="Siempre APROBADO: sin pago ordenado no hay vista")
    cliente_nombre: str | None = Field(description="Null si `clientes` no responde a tiempo")
    reportado_en: datetime
    compromiso_horas: int = Field(
        ge=1, description="Compromiso del trámite, para comparar con el tiempo total (DS3-04)"
    )
    liquidacion: LiquidacionDto = Field(
        description="Valor a indemnizar, deducible y referencia del pago (HU-W20)"
    )
    cuenta_mascarada: str | None = Field(
        description="Cuenta destino enmascarada, p. ej. ****1004. Nunca la cuenta completa"
    )
    autorizado_por: str | None = Field(
        description="Nombre del analista que aprobó; null si lo aprobó un paramétrico (HU-W22)"
    )
    historial: list[EntradaHistorial] = Field(
        description="Trazabilidad del trámite, del más antiguo al más reciente; incluye "
        "PAGO_CONFIRMADO o PAGO_RECHAZADO cuando `siniestros` ya reflejó el resultado"
    )
    indemnizacion: IndemnizacionVista | None = Field(
        description="Null mientras `dinero` no haya publicado un resultado"
    )
