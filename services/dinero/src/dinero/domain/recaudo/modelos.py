"""Movimiento de recaudo del primer cobro de la prima (HU-W16; ADR 0013, decisiones 2, 4, 5 y 7).
Puro: sin Pydantic ni E/S.

- Nace `PENDIENTE` a partir de `OfertaAceptada` y guarda solo lo que trae el evento: `claveRecaudo`
  (la clave única y la idempotente hacia el proveedor), `cotizacionId`, el pagador sintético
  (`clienteId`) y la prima. `periodicidad` es informativa: v1 cobra solo el primer recaudo.
- Solo una respuesta definitiva lo lleva a `CONFIRMADO` (con `transaccionId`) o a `RECHAZADO` (con
  `motivo`). Un incierto (timeout, 5xx o respuesta inválida) lo deja `PENDIENTE` y registra el
  código: nunca se asume rechazo. De un estado final no se sale.
- El monto es `Decimal`, nunca `float`, con a lo sumo 2 decimales en su escala (16.10): es lo que
  admiten adaptador-pagos y el proveedor. Ningún dato de tarjeta ni de cuenta.
- Los códigos (`motivo`, `ultimo_incierto`) tienen forma de código: así no entra texto libre del
  proveedor ni PII.
"""

import dataclasses
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Self, TypeGuard
from uuid import UUID, uuid4

from dinero.domain.recaudo.errores import RecaudoInvalido, TransicionInvalida

# El mismo patrón que los CHECK de la migración 001.
CODIGO = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
LARGO_TRANSACCION = 128
# Escala máxima del monto: la de `monto` en contracts/openapi/adaptador-pagos.yaml.
DECIMALES_MAX = 2
# Un RECHAZADO sin motivo, o con un texto que no es un código (16.10).
MOTIVO_NO_INFORMADO = "MOTIVO_NO_INFORMADO"


class EstadoRecaudo(StrEnum):
    PENDIENTE = "PENDIENTE"
    CONFIRMADO = "CONFIRMADO"
    RECHAZADO = "RECHAZADO"


class Moneda(StrEnum):
    # La única que admite adaptador-pagos (contracts/openapi/adaptador-pagos.yaml).
    COP = "COP"


def _con_zona(instante: object) -> bool:
    return isinstance(instante, datetime) and instante.utcoffset() is not None


def _es_codigo(valor: object) -> TypeGuard[str]:
    return isinstance(valor, str) and CODIGO.fullmatch(valor) is not None


def _es_transaccion(valor: object) -> bool:
    return isinstance(valor, str) and bool(valor.strip()) and len(valor) <= LARGO_TRANSACCION


def motivo_de_rechazo(motivo: str | None) -> str:
    """El motivo del proveedor si tiene forma de código; si no, `MOTIVO_NO_INFORMADO`."""
    return motivo if _es_codigo(motivo) else MOTIVO_NO_INFORMADO


@dataclass(frozen=True, slots=True)
class Recaudo:
    recaudo_id: UUID
    clave_recaudo: UUID
    cotizacion_id: UUID
    pagador_ref: UUID
    monto: Decimal
    moneda: Moneda
    periodicidad: str
    estado: EstadoRecaudo
    transaccion_id: str | None
    motivo: str | None
    # Código del último resultado incierto (p. ej., TIEMPO_AGOTADO); se conserva al resolver.
    ultimo_incierto: str | None
    creado_en: datetime
    actualizado_en: datetime
    resuelto_en: datetime | None
    correlation_id: str

    def __post_init__(self) -> None:
        for campo in ("recaudo_id", "clave_recaudo", "cotizacion_id", "pagador_ref"):
            if not isinstance(getattr(self, campo), UUID):
                raise RecaudoInvalido(f"{campo}: se espera un uuid")
        monto = self.monto
        if type(monto) is not Decimal or not monto.is_finite() or monto <= 0:
            raise RecaudoInvalido("monto: se espera un Decimal finito mayor que 0")
        exponente = monto.as_tuple().exponent
        if isinstance(exponente, int) and exponente < -DECIMALES_MAX:
            raise RecaudoInvalido(f"monto: como máximo {DECIMALES_MAX} decimales")
        if not isinstance(self.moneda, Moneda):
            raise RecaudoInvalido("moneda: se espera una Moneda")
        if not isinstance(self.estado, EstadoRecaudo):
            raise RecaudoInvalido("estado: se espera un EstadoRecaudo")
        for campo in ("periodicidad", "correlation_id"):
            valor = getattr(self, campo)
            if not isinstance(valor, str) or not valor:
                raise RecaudoInvalido(f"{campo}: obligatorio")
        for campo in ("creado_en", "actualizado_en"):
            if not _con_zona(getattr(self, campo)):
                raise RecaudoInvalido(f"{campo}: se espera una hora con zona")
        if self.resuelto_en is not None and not _con_zona(self.resuelto_en):
            raise RecaudoInvalido("resuelto_en: se espera una hora con zona")
        if self.ultimo_incierto is not None and not _es_codigo(self.ultimo_incierto):
            raise RecaudoInvalido("ultimo_incierto: se espera un código")
        self._validar_resultado()

    def _validar_resultado(self) -> None:
        estado = self.estado
        if estado is EstadoRecaudo.PENDIENTE:
            if self.transaccion_id is not None:
                raise RecaudoInvalido("transaccion_id: un PENDIENTE no tiene transacción")
            if self.motivo is not None:
                raise RecaudoInvalido("motivo: un PENDIENTE no tiene motivo")
            if self.resuelto_en is not None:
                raise RecaudoInvalido("resuelto_en: un PENDIENTE no está resuelto")
            return
        if self.resuelto_en is None:
            raise RecaudoInvalido(f"resuelto_en: obligatorio en {estado}")
        if estado is EstadoRecaudo.CONFIRMADO:
            if not _es_transaccion(self.transaccion_id):
                raise RecaudoInvalido("transaccion_id: obligatorio en CONFIRMADO")
            if self.motivo is not None:
                raise RecaudoInvalido("motivo: un CONFIRMADO no tiene motivo")
            return
        if not _es_codigo(self.motivo):
            raise RecaudoInvalido("motivo: obligatorio en RECHAZADO, con forma de código")
        if self.transaccion_id is not None and not _es_transaccion(self.transaccion_id):
            raise RecaudoInvalido("transaccion_id: vacío o demasiado largo")

    @classmethod
    def pendiente(
        cls,
        *,
        clave_recaudo: UUID,
        cotizacion_id: UUID,
        pagador_ref: UUID,
        monto: Decimal,
        moneda: str,
        periodicidad: str,
        correlation_id: str,
        ahora: datetime,
        recaudo_id: UUID | None = None,
    ) -> Self:
        """El movimiento de una `OfertaAceptada` nueva, antes de llamar al adaptador."""
        try:
            moneda_valida = Moneda(moneda)
        except ValueError:
            raise RecaudoInvalido("moneda: no admitida") from None
        return cls(
            recaudo_id=uuid4() if recaudo_id is None else recaudo_id,
            clave_recaudo=clave_recaudo,
            cotizacion_id=cotizacion_id,
            pagador_ref=pagador_ref,
            monto=monto,
            moneda=moneda_valida,
            periodicidad=periodicidad,
            estado=EstadoRecaudo.PENDIENTE,
            transaccion_id=None,
            motivo=None,
            ultimo_incierto=None,
            creado_en=ahora,
            actualizado_en=ahora,
            resuelto_en=None,
            correlation_id=correlation_id,
        )

    @property
    def final(self) -> bool:
        return self.estado is not EstadoRecaudo.PENDIENTE

    def _desde_pendiente(self, hacia: str) -> None:
        if self.final:
            raise TransicionInvalida(self.estado, hacia)

    def confirmar(self, transaccion_id: str, ahora: datetime) -> Self:
        """El adaptador respondió APROBADO."""
        self._desde_pendiente(EstadoRecaudo.CONFIRMADO)
        return dataclasses.replace(
            self,
            estado=EstadoRecaudo.CONFIRMADO,
            transaccion_id=transaccion_id,
            actualizado_en=ahora,
            resuelto_en=ahora,
        )

    def rechazar(self, motivo: str, ahora: datetime, transaccion_id: str | None = None) -> Self:
        """El adaptador respondió RECHAZADO; puede traer la transacción del proveedor."""
        self._desde_pendiente(EstadoRecaudo.RECHAZADO)
        return dataclasses.replace(
            self,
            estado=EstadoRecaudo.RECHAZADO,
            motivo=motivo,
            transaccion_id=transaccion_id,
            actualizado_en=ahora,
            resuelto_en=ahora,
        )

    def registrar_incierto(self, codigo: str, ahora: datetime) -> Self:
        """Timeout, 5xx o respuesta inválida: sigue PENDIENTE y queda el código."""
        self._desde_pendiente(EstadoRecaudo.PENDIENTE)
        return dataclasses.replace(self, ultimo_incierto=codigo, actualizado_en=ahora)
