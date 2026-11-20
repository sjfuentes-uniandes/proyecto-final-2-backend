"""Vista de H09 de `bff-web` (HU-W22, W22-B03; prototipo de `adapters/inbound/parametricos_dto.py`).

`ResumenParametricosVista` es lo que el portal necesita para H09 «Siniestros paramétricos». `bff-web`
la compone con tres fuentes, y **ninguna falla de una fuente secundaria da 503**:

- `pagos-parametricos` (`GET /v1/parametricos/resumen`): indicadores, ritmo y últimos pagos. Es la
  fuente principal: si no responde, 503 `SERVICIO_NO_DISPONIBLE`.
- CloudWatch: la profundidad de `parametricos-eventos`, las tareas activas y, si `pagos-parametricos`
  no pudo leerla, la DLQ. Sin métrica o sin permiso, el campo es null (H09 muestra «no disponible»).
- `siniestros` (`GET /v1/siniestros/parametricos/pagos`): el estado de pago de cada fila. Si no
  responde, `estadoPago` es null en todas.

El estado de pago sale de `siniestros` y no de `dinero`: `bff-web` no llama a `dinero` hasta SYNC-01.
"""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import Field

from bff_web.adapters.inbound.siniestros_dto import (
    CodigoSiniestro,
    EstadoPago,
    NumeroPoliza,
    _Dto,
)

TextoDecimal = Annotated[str, Field(pattern=r"^[0-9]+(\.[0-9]{1,3})?$")]
EventoOrigen = Annotated[str, Field(pattern=r"^[A-Za-z0-9._:-]{1,64}$")]


class RitmoMinutoVista(_Dto):
    minuto: datetime = Field(description="Inicio del minuto (UTC)")
    procesados: int


class DisparadorVista(_Dto):
    tipo_evento: str
    valor_observado: TextoDecimal
    umbral: TextoDecimal
    unidad: str


class PagoActivadoVista(_Dto):
    """Una fila de «Últimos pagos activados». El enlace al detalle usa `siniestroId`."""

    siniestro_id: UUID
    siniestro_codigo: CodigoSiniestro
    poliza_id: UUID
    poliza_numero: NumeroPoliza | None
    disparador: DisparadorVista
    valor: Annotated[str, Field(pattern=r"^[0-9]+(\.[0-9]{1,2})?$")]
    moneda: str = Field(pattern=r"^[A-Z]{3}$")
    estado_activacion: str = Field(pattern=r"^[A-Z_]{3,32}$")
    estado_pago: EstadoPago | None = Field(
        description="Desde `siniestros`: PENDIENTE hasta que `dinero` informa un resultado. Null si "
        "`siniestros` no respondió"
    )


class ColaParametricos(_Dto):
    """La cola `parametricos-eventos` y quienes la consumen, desde CloudWatch (DS3-26, HU-W28). Cada
    campo es null si no hay métrica o `bff-web` no tiene permiso para leerla."""

    profundidad: int | None = Field(description="Mensajes visibles esperando ser procesados")
    consumidores_activos: int | None = Field(
        description="Tareas de `pagos-parametricos` en ejecución"
    )
    consumidores_maximos: int | None = Field(
        description="Tareas que el escalado permite como máximo"
    )


class ResumenParametricosVista(_Dto):
    """H09. Los números de procesamiento son los de `pagos-parametricos` para `eventoOrigen`."""

    evento_origen: EventoOrigen | None
    desde: datetime | None
    recibidos: int
    procesados: int
    activados: int
    duplicados: int
    en_dlq: int | None = Field(
        description="Mensajes en la DLQ de `parametricos-eventos`: el de `pagos-parametricos` y, si ese "
        "es null, el de CloudWatch. Null si ninguna fuente lo da"
    )
    ritmo_por_minuto: list[RitmoMinutoVista]
    cola: ColaParametricos
    p95_venta_consulta_ms: int | None = Field(
        description="p95 del tráfico en línea de venta y consulta durante la avalancha (CF-S3-10). Null "
        "si no hay métrica: el portal oculta ese aviso"
    )
    ultimos_pagos: list[PagoActivadoVista]


class OrigenRecienteVista(_Dto):
    evento_origen: EventoOrigen
    desde: datetime
    hasta: datetime
    eventos: int


class OrigenesRecientesVista(_Dto):
    items: list[OrigenRecienteVista]
