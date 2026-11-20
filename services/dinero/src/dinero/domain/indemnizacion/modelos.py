"""Indemnización: el pago único de un siniestro aprobado (HU-W23; ADR 0015; DS3-17).
Puro: sin Pydantic ni E/S. Tiene la forma de `Recaudo` (HU-W16).

- Nace `PENDIENTE` a partir de `SiniestroAprobado` y guarda solo lo que trae el evento: el
  siniestro, la referencia (`IND-<codigo>-01`, única y clave idempotente hacia la pasarela), el
  monto y el `beneficiarioRef`, una referencia opaca: nunca la cuenta.
- Solo una respuesta definitiva la lleva a `CONFIRMADA` (con `transaccionId`) o a `RECHAZADA` (con
  `motivo`). Un incierto (timeout, 5xx o respuesta inválida) la deja `PENDIENTE` y registra el
  código: nunca se asume rechazo. De un estado final no se sale (ADR 0013, decisión 5).
- `registrar_duplicado` cuenta una orden o una confirmación repetida sin cambiar nada más.
- `intentos` cuenta las llamadas efectivas de pago (`POST /v1/desembolsos`): un incierto también es
  un intento. Resolver por una consulta no suma uno, pero si la pasarela ya conoce la orden es que
  hubo al menos uno.
- El monto es `Decimal`, nunca `float`, con a lo sumo 2 decimales (lo que admite la pasarela).
"""

import dataclasses
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Self, TypeGuard
from uuid import UUID, uuid4

from dinero.domain.indemnizacion.errores import IndemnizacionInvalida, TransicionInvalida

# El mismo patrón que los CHECK de la migración 002 (y que `Recaudo`).
CODIGO = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
REFERENCIA = re.compile(r"^IND-88-[0-9]{4}-[0-9]{4,}-[0-9]{2}$")
LARGO_TRANSACCION = 128
LARGO_BENEFICIARIO = 128
DECIMALES_MAX = 2
# Un RECHAZADA sin motivo, o con un texto que no es un código.
MOTIVO_NO_INFORMADO = "MOTIVO_NO_INFORMADO"
# `tipo` fijo: el CHECK de la tabla y la mitad de la clave única (siniestro_id, tipo).
TIPO = "INDEMNIZACION"


class EstadoIndemnizacion(StrEnum):
    PENDIENTE = "PENDIENTE"
    CONFIRMADA = "CONFIRMADA"
    RECHAZADA = "RECHAZADA"


class Moneda(StrEnum):
    # La única que admite adaptador-pagos.
    COP = "COP"


def _con_zona(instante: object) -> bool:
    return isinstance(instante, datetime) and instante.utcoffset() is not None


def _es_codigo(valor: object) -> TypeGuard[str]:
    return isinstance(valor, str) and CODIGO.fullmatch(valor) is not None


def _es_transaccion(valor: object) -> bool:
    return isinstance(valor, str) and bool(valor.strip()) and len(valor) <= LARGO_TRANSACCION


def motivo_de_rechazo(motivo: str | None) -> str:
    """El motivo de la pasarela si tiene forma de código; si no, `MOTIVO_NO_INFORMADO`."""
    return motivo if _es_codigo(motivo) else MOTIVO_NO_INFORMADO


@dataclass(frozen=True, slots=True)
class Indemnizacion:
    indemnizacion_id: UUID
    siniestro_id: UUID
    tipo: str
    referencia: str
    monto: Decimal
    moneda: Moneda
    # Referencia opaca del beneficiario: nunca va a los logs, a los eventos ni a la API.
    beneficiario_ref: str
    estado: EstadoIndemnizacion
    transaccion_id: str | None
    motivo: str | None
    intentos: int
    duplicados_descartados: int
    # Código del último resultado incierto (p. ej., ADAPTADOR_TIEMPO_AGOTADO); se conserva al resolver.
    ultimo_incierto: str | None
    # DS3-18: hasta cuándo la reclama una conciliación.
    reclamado_hasta: datetime | None
    creado_en: datetime
    actualizado_en: datetime
    resuelto_en: datetime | None
    correlation_id: str

    def __post_init__(self) -> None:
        for campo in ("indemnizacion_id", "siniestro_id"):
            if not isinstance(getattr(self, campo), UUID):
                raise IndemnizacionInvalida(f"{campo}: se espera un uuid")
        if self.tipo != TIPO:
            raise IndemnizacionInvalida(f"tipo: solo {TIPO}")
        if not isinstance(self.referencia, str) or REFERENCIA.fullmatch(self.referencia) is None:
            raise IndemnizacionInvalida("referencia: se espera IND-<codigo>-NN")
        monto = self.monto
        if type(monto) is not Decimal or not monto.is_finite() or monto <= 0:
            raise IndemnizacionInvalida("monto: se espera un Decimal finito mayor que 0")
        exponente = monto.as_tuple().exponent
        if isinstance(exponente, int) and exponente < -DECIMALES_MAX:
            raise IndemnizacionInvalida(f"monto: como máximo {DECIMALES_MAX} decimales")
        if not isinstance(self.moneda, Moneda):
            raise IndemnizacionInvalida("moneda: se espera una Moneda")
        beneficiario = self.beneficiario_ref
        if (
            not isinstance(beneficiario, str)
            or not beneficiario.strip()
            or len(beneficiario) > LARGO_BENEFICIARIO
        ):
            raise IndemnizacionInvalida("beneficiario_ref: obligatorio, hasta 128 caracteres")
        if not isinstance(self.estado, EstadoIndemnizacion):
            raise IndemnizacionInvalida("estado: se espera un EstadoIndemnizacion")
        for campo in ("intentos", "duplicados_descartados"):
            valor = getattr(self, campo)
            if type(valor) is not int or valor < 0:
                raise IndemnizacionInvalida(f"{campo}: se espera un entero no negativo")
        if not isinstance(self.correlation_id, str) or not self.correlation_id:
            raise IndemnizacionInvalida("correlation_id: obligatorio")
        for campo in ("creado_en", "actualizado_en"):
            if not _con_zona(getattr(self, campo)):
                raise IndemnizacionInvalida(f"{campo}: se espera una hora con zona")
        for campo in ("resuelto_en", "reclamado_hasta"):
            valor = getattr(self, campo)
            if valor is not None and not _con_zona(valor):
                raise IndemnizacionInvalida(f"{campo}: se espera una hora con zona")
        if self.ultimo_incierto is not None and not _es_codigo(self.ultimo_incierto):
            raise IndemnizacionInvalida("ultimo_incierto: se espera un código")
        self._validar_resultado()

    def _validar_resultado(self) -> None:
        estado = self.estado
        if estado is EstadoIndemnizacion.PENDIENTE:
            if self.transaccion_id is not None:
                raise IndemnizacionInvalida("transaccion_id: una PENDIENTE no tiene transacción")
            if self.motivo is not None:
                raise IndemnizacionInvalida("motivo: una PENDIENTE no tiene motivo")
            if self.resuelto_en is not None:
                raise IndemnizacionInvalida("resuelto_en: una PENDIENTE no está resuelta")
            return
        if self.resuelto_en is None:
            raise IndemnizacionInvalida(f"resuelto_en: obligatorio en {estado}")
        if estado is EstadoIndemnizacion.CONFIRMADA:
            if not _es_transaccion(self.transaccion_id):
                raise IndemnizacionInvalida("transaccion_id: obligatorio en CONFIRMADA")
            if self.motivo is not None:
                raise IndemnizacionInvalida("motivo: una CONFIRMADA no tiene motivo")
            return
        if not _es_codigo(self.motivo):
            raise IndemnizacionInvalida("motivo: obligatorio en RECHAZADA, con forma de código")
        if self.transaccion_id is not None and not _es_transaccion(self.transaccion_id):
            raise IndemnizacionInvalida("transaccion_id: vacío o demasiado largo")

    @classmethod
    def pendiente(
        cls,
        *,
        siniestro_id: UUID,
        referencia: str,
        monto: Decimal,
        moneda: str,
        beneficiario_ref: str,
        correlation_id: str,
        ahora: datetime,
        indemnizacion_id: UUID | None = None,
    ) -> Self:
        """La indemnización de un `SiniestroAprobado` nuevo, antes de llamar a la pasarela."""
        try:
            moneda_valida = Moneda(moneda)
        except ValueError:
            raise IndemnizacionInvalida("moneda: no admitida") from None
        return cls(
            indemnizacion_id=uuid4() if indemnizacion_id is None else indemnizacion_id,
            siniestro_id=siniestro_id,
            tipo=TIPO,
            referencia=referencia,
            monto=monto,
            moneda=moneda_valida,
            beneficiario_ref=beneficiario_ref,
            estado=EstadoIndemnizacion.PENDIENTE,
            transaccion_id=None,
            motivo=None,
            intentos=0,
            duplicados_descartados=0,
            ultimo_incierto=None,
            reclamado_hasta=None,
            creado_en=ahora,
            actualizado_en=ahora,
            resuelto_en=None,
            correlation_id=correlation_id,
        )

    @property
    def final(self) -> bool:
        return self.estado is not EstadoIndemnizacion.PENDIENTE

    def _desde_pendiente(self, hacia: str) -> None:
        if self.final:
            raise TransicionInvalida(self.estado, hacia)

    def _intentos(self, intento: bool) -> int:
        # Si la pasarela ya conoce la orden (resuelto por consulta), hubo al menos un intento.
        return self.intentos + 1 if intento else max(self.intentos, 1)

    def confirmar(self, transaccion_id: str, ahora: datetime, *, intento: bool = True) -> Self:
        """La pasarela respondió CONFIRMADO. `intento` es falso si salió de una consulta."""
        self._desde_pendiente(EstadoIndemnizacion.CONFIRMADA)
        return dataclasses.replace(
            self,
            estado=EstadoIndemnizacion.CONFIRMADA,
            transaccion_id=transaccion_id,
            intentos=self._intentos(intento),
            reclamado_hasta=None,
            actualizado_en=ahora,
            resuelto_en=ahora,
        )

    def rechazar(
        self,
        motivo: str,
        ahora: datetime,
        transaccion_id: str | None = None,
        *,
        intento: bool = True,
    ) -> Self:
        """La pasarela respondió RECHAZADO; puede traer la transacción."""
        self._desde_pendiente(EstadoIndemnizacion.RECHAZADA)
        return dataclasses.replace(
            self,
            estado=EstadoIndemnizacion.RECHAZADA,
            motivo=motivo,
            transaccion_id=transaccion_id,
            intentos=self._intentos(intento),
            reclamado_hasta=None,
            actualizado_en=ahora,
            resuelto_en=ahora,
        )

    def registrar_incierto(self, codigo: str, ahora: datetime, *, intento: bool = True) -> Self:
        """Timeout, 5xx o respuesta inválida: sigue PENDIENTE y queda el código."""
        self._desde_pendiente(EstadoIndemnizacion.PENDIENTE)
        return dataclasses.replace(
            self,
            ultimo_incierto=codigo,
            intentos=self.intentos + 1 if intento else self.intentos,
            reclamado_hasta=None,
            actualizado_en=ahora,
        )

    def registrar_duplicado(self) -> Self:
        """Una orden o una confirmación repetida: solo suma al contador, en cualquier estado."""
        return dataclasses.replace(self, duplicados_descartados=self.duplicados_descartados + 1)

    def reclamar(self, hasta: datetime) -> Self:
        """La conciliación la toma hasta `hasta` (DS3-18)."""
        self._desde_pendiente(EstadoIndemnizacion.PENDIENTE)
        return dataclasses.replace(self, reclamado_hasta=hasta)
