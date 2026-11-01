"""La póliza de una aceptación (HU-W17, 17.6; ADR 0014, decisiones 1 a 3). Puros: sin Pydantic ni
E/S; el instante actual lo recibe cada operación.

- Nace `EMITIENDO` con el primer `RecaudoConfirmado` de una aceptación conocida: una por
  aceptación. Su número (`<ramo>-<año>-<6 dígitos>`) **se reserva al crearla**, porque el PDF lo
  lleva antes de `ACTIVA`; el año es el de la creación en Bogotá. Un número reservado que nunca
  llega a `ACTIVA` deja un hueco en la secuencia, que se acepta.
- Dentro de `EMITIENDO`, dos puntos de control que se registran una sola vez, para que un reintento
  no repita trabajo: el documento subido y la firma de su huella.
- `activar` exige los dos y pasa a `ACTIVA`, que es final. `emitida_en` es la fecha del documento:
  la que se imprime en el PDF (decisión 4), fijada antes de generarlo.
- Un fallo deja `EMITIENDO` con su código (forma de código, como en dinero: sin texto libre ni PII).
- HU-W17 (17.8): `correlation_id` es el del `RecaudoConfirmado` que la creó (nulo si llegó sin uno
  válido). Lo propaga la emisión y lo lleva `PolizaEmitida`; no cambia.
"""

import dataclasses
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import StrEnum
from types import MappingProxyType
from typing import Self
from uuid import UUID, uuid4

from polizas.domain.poliza.errores import (
    HuellaNoCoincide,
    PolizaInvalida,
    ProductoSinRamo,
    TransicionInvalida,
)

# Colombia no tiene horario de verano desde 1993: un desfase fijo es America/Bogota para cualquier
# fecha de aceptación, sin depender de la base de zonas del sistema (en el lock, `tzdata` solo llega
# en Windows, por psycopg).
BOGOTA = timezone(timedelta(hours=-5), "America/Bogota")
# Los mismos patrones que los CHECK de la migración 004 y que `PolizaEmitida` v1.
CODIGO = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
HUELLA = re.compile(r"^sha256:[0-9a-f]{64}$")
NUMERO = re.compile(r"^[0-9]{3}-[0-9]{4}-[0-9]{6}$")
SECUENCIA_MAX = 999_999


class EstadoPoliza(StrEnum):
    EMITIENDO = "EMITIENDO"
    ACTIVA = "ACTIVA"


@dataclass(frozen=True, slots=True)
class Producto:
    """El producto y la versión de la oferta aceptada (instantánea de la decisión)."""

    id: str
    version: str


@dataclass(frozen=True, slots=True)
class Ramo:
    codigo: str
    nombre: str


# ADR 0014, decisión 2: convención de Solventa, no un dato del catálogo.
RAMOS: Mapping[str, Ramo] = MappingProxyType(
    {"vida-hipotecario": Ramo(codigo="100", nombre="Vida hipotecario")}
)


def ramo_de(producto_id: str) -> Ramo:
    ramo = RAMOS.get(producto_id)
    if ramo is None:
        raise ProductoSinRamo(producto_id)
    return ramo


def _con_zona(instante: object) -> bool:
    return isinstance(instante, datetime) and instante.utcoffset() is not None


def _texto(valor: object) -> bool:
    return isinstance(valor, str) and valor.strip() != ""


def _fecha_local(instante: datetime, campo: str) -> datetime:
    if not _con_zona(instante):
        raise PolizaInvalida(f"{campo}: se espera una hora con zona")
    return instante.astimezone(BOGOTA)


def numero_poliza(ramo: Ramo, creada_en: datetime, secuencia: int) -> str:
    """`<ramo>-<año de creación en Bogotá>-<secuencia en 6 dígitos>`."""
    anio = _fecha_local(creada_en, "creada_en").year
    if not 1 <= secuencia <= SECUENCIA_MAX:
        raise PolizaInvalida("numero: la secuencia no cabe en 6 dígitos")
    return f"{ramo.codigo}-{anio:04d}-{secuencia:06d}"


@dataclass(frozen=True, slots=True)
class Vigencia:
    """Fechas inclusivas en America/Bogota."""

    desde: date
    hasta: date

    def __post_init__(self) -> None:
        if self.hasta <= self.desde:
            raise PolizaInvalida("vigencia: hasta debe ser posterior a desde")


def _aniversario(dia: date) -> date:
    """El mismo día un año después; el de un 29 de febrero es el 1 de marzo."""
    try:
        return dia.replace(year=dia.year + 1)
    except ValueError:
        return date(dia.year + 1, 3, 1)


def vigencia_de(aceptada_en: datetime) -> Vigencia:
    """Desde el día siguiente a la aceptación (en Bogotá) hasta el anterior a su aniversario
    (decisión 3): un año, sin depender del plazo del crédito."""
    desde = _fecha_local(aceptada_en, "aceptada_en").date() + timedelta(days=1)
    return Vigencia(desde=desde, hasta=_aniversario(desde) - timedelta(days=1))


@dataclass(frozen=True, slots=True)
class Documento:
    """Punto de control: la versión del PDF subida al bucket. `fecha_documento` es la que lleva el
    PDF y será `emitida_en`."""

    clave: str
    version_id: str
    huella: str
    tamano_bytes: int
    fecha_documento: datetime

    def __post_init__(self) -> None:
        for campo in ("clave", "version_id"):
            if not _texto(getattr(self, campo)):
                raise PolizaInvalida(f"documento.{campo}: obligatorio")
        if not isinstance(self.huella, str) or HUELLA.fullmatch(self.huella) is None:
            raise PolizaInvalida("documento.huella: se espera sha256:<64 hex>")
        tamano = self.tamano_bytes
        if isinstance(tamano, bool) or not isinstance(tamano, int) or tamano <= 0:
            raise PolizaInvalida("documento.tamano_bytes: se espera un entero mayor que 0")
        if not _con_zona(self.fecha_documento):
            raise PolizaInvalida("documento.fecha_documento: se espera una hora con zona")


@dataclass(frozen=True, slots=True)
class Firma:
    """Punto de control: la firma de Solventa sobre la huella del documento."""

    firma_id: str
    sello_tiempo: datetime

    def __post_init__(self) -> None:
        if not _texto(self.firma_id):
            raise PolizaInvalida("firma.firma_id: obligatorio")
        if not _con_zona(self.sello_tiempo):
            raise PolizaInvalida("firma.sello_tiempo: se espera una hora con zona")


@dataclass(frozen=True, slots=True)
class OrigenPoliza:
    """Lo que la póliza toma de la aceptación y de la instantánea de su decisión."""

    aceptacion_id: UUID
    cotizacion_id: UUID
    cliente_id: UUID
    producto: Producto
    aceptada_en: datetime


@dataclass(frozen=True, slots=True)
class Poliza:
    poliza_id: UUID
    # = claveRecaudo: una póliza por aceptación.
    aceptacion_id: UUID
    cotizacion_id: UUID
    cliente_id: UUID
    producto: Producto
    ramo: Ramo
    numero: str
    vigencia: Vigencia
    estado: EstadoPoliza
    documento: Documento | None
    firma: Firma | None
    emitida_en: datetime | None
    ultimo_fallo: str | None
    creada_en: datetime
    actualizada_en: datetime
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        for campo in ("poliza_id", "aceptacion_id", "cotizacion_id", "cliente_id"):
            if not isinstance(getattr(self, campo), UUID):
                raise PolizaInvalida(f"{campo}: se espera un uuid")
        if not (_texto(self.producto.id) and _texto(self.producto.version)):
            raise PolizaInvalida("producto: id y version obligatorios")
        if NUMERO.fullmatch(self.numero) is None or not self.numero.startswith(
            f"{self.ramo.codigo}-"
        ):
            raise PolizaInvalida("numero: se espera <ramo>-<año>-<6 dígitos>")
        if not isinstance(self.estado, EstadoPoliza):
            raise PolizaInvalida("estado: se espera un EstadoPoliza")
        for campo in ("creada_en", "actualizada_en"):
            if not _con_zona(getattr(self, campo)):
                raise PolizaInvalida(f"{campo}: se espera una hora con zona")
        if self.ultimo_fallo is not None and CODIGO.fullmatch(self.ultimo_fallo) is None:
            raise PolizaInvalida("ultimo_fallo: se espera un código")
        if self.firma is not None and self.documento is None:
            raise PolizaInvalida("firma: solo con el documento registrado")
        if self.estado is EstadoPoliza.EMITIENDO:
            if self.emitida_en is not None:
                raise PolizaInvalida("emitida_en: una póliza EMITIENDO no está emitida")
            return
        if self.documento is None or self.firma is None:
            raise PolizaInvalida("estado: ACTIVA exige documento y firma")
        if not _con_zona(self.emitida_en):
            raise PolizaInvalida("emitida_en: obligatoria en ACTIVA, con zona")

    @classmethod
    def emitiendo(
        cls,
        origen: OrigenPoliza,
        *,
        secuencia: int,
        ahora: datetime,
        poliza_id: UUID | None = None,
        correlation_id: str | None = None,
    ) -> Self:
        """La emisión del primer `RecaudoConfirmado`, con su número ya reservado. Lanza
        `ProductoSinRamo` si el producto no está en la tabla de ramos."""
        ramo = ramo_de(origen.producto.id)
        return cls(
            poliza_id=uuid4() if poliza_id is None else poliza_id,
            aceptacion_id=origen.aceptacion_id,
            cotizacion_id=origen.cotizacion_id,
            cliente_id=origen.cliente_id,
            producto=origen.producto,
            ramo=ramo,
            numero=numero_poliza(ramo, ahora, secuencia),
            vigencia=vigencia_de(origen.aceptada_en),
            estado=EstadoPoliza.EMITIENDO,
            documento=None,
            firma=None,
            emitida_en=None,
            ultimo_fallo=None,
            creada_en=ahora,
            actualizada_en=ahora,
            correlation_id=correlation_id,
        )

    def _emitiendo(self, hacia: str) -> None:
        if self.estado is not EstadoPoliza.EMITIENDO:
            raise TransicionInvalida(self.estado, hacia)

    def registrar_documento(self, documento: Documento, ahora: datetime) -> Self:
        """El PDF ya está en el bucket: un reintento reutiliza esa versión."""
        self._emitiendo("DOCUMENTO")
        if self.documento is not None:
            raise TransicionInvalida(self.estado, "DOCUMENTO")
        return dataclasses.replace(self, documento=documento, actualizada_en=ahora)

    def registrar_firma(self, firma: Firma, huella_firmada: str, ahora: datetime) -> Self:
        """La huella del documento ya está firmada; `huella_firmada` es la que devolvió la firma."""
        self._emitiendo("FIRMA")
        if self.documento is None or self.firma is not None:
            raise TransicionInvalida(self.estado, "FIRMA")
        if huella_firmada != self.documento.huella:
            raise HuellaNoCoincide()
        return dataclasses.replace(self, firma=firma, actualizada_en=ahora)

    def activar(self, ahora: datetime) -> Self:
        self._emitiendo(EstadoPoliza.ACTIVA)
        if self.documento is None or self.firma is None:
            raise TransicionInvalida(self.estado, EstadoPoliza.ACTIVA)
        return dataclasses.replace(
            self,
            estado=EstadoPoliza.ACTIVA,
            emitida_en=self.documento.fecha_documento,
            actualizada_en=ahora,
        )

    def registrar_fallo(self, codigo: str, ahora: datetime) -> Self:
        """PDF, subida o firma fallidos: sigue EMITIENDO, con sus puntos de control."""
        self._emitiendo("FALLO")
        return dataclasses.replace(self, ultimo_fallo=codigo, actualizada_en=ahora)
