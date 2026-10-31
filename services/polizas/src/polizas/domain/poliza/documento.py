"""Los datos del PDF de la póliza y su clave en el bucket (HU-W17, 17.7; ADR 0014, decisión 4).
Puros: sin Pydantic ni E/S.

- `DatosDocumento` es todo lo que lleva el PDF, y nada más: con él, el generador no lee nada y da
  siempre los mismos bytes. Sale de la póliza EMITIENDO, de su aceptación, de la instantánea de su
  decisión (DS2-10) y del recaudo confirmado.
- **Sin nombres** (decisión 4; plan §b, decisión 7): el titular y el canal van solo como
  identificadores (`cliente_id`, `socio_id`), porque polizas no llama a clientes ni a api-socios.
  Tampoco lleva lo sensible de los insumos (edad, perfil).
- `fecha_documento` es la única fecha que no sale de la aceptación: la elige quien emite, antes de
  generar el PDF, y será `emitida_en` (17.6).
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from polizas.domain.poliza.errores import PolizaInvalida
from polizas.domain.poliza.modelos import NUMERO, Poliza, Producto, Ramo, Vigencia
from polizas.domain.suscripcion.modelos import Cobertura


def _texto(valor: object) -> bool:
    return isinstance(valor, str) and valor.strip() != ""


def _con_zona(instante: object) -> bool:
    return isinstance(instante, datetime) and instante.utcoffset() is not None


def _monto_positivo(valor: object) -> bool:
    # Decimal, nunca float; bool no es un monto.
    return isinstance(valor, Decimal) and valor.is_finite() and valor > 0


@dataclass(frozen=True, slots=True)
class FuenteDocumento:
    """Lo que el PDF toma de la aceptación, de la instantánea de su decisión y del recaudo."""

    coberturas: tuple[Cobertura, ...]
    suma_asegurada: Decimal
    prima: Decimal
    periodicidad: str
    moneda: str
    socio_id: str | None
    aceptada_en: datetime
    transaccion_id: str
    version_reglas_prima: str
    version_reglas_suscripcion: str


@dataclass(frozen=True, slots=True)
class DatosDocumento:
    numero: str
    ramo: Ramo
    producto: Producto
    vigencia: Vigencia
    coberturas: tuple[Cobertura, ...]
    suma_asegurada: Decimal
    prima: Decimal
    periodicidad: str
    moneda: str
    cliente_id: UUID
    socio_id: str | None
    aceptada_en: datetime
    transaccion_id: str
    version_reglas_prima: str
    version_reglas_suscripcion: str
    fecha_documento: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.numero, str) or NUMERO.fullmatch(self.numero) is None:
            raise PolizaInvalida("numero: se espera <ramo>-<año>-<6 dígitos>")
        if not isinstance(self.coberturas, tuple) or not self.coberturas:
            raise PolizaInvalida("coberturas: se espera al menos una")
        for campo in ("suma_asegurada", "prima"):
            if not _monto_positivo(getattr(self, campo)):
                raise PolizaInvalida(f"{campo}: se espera un Decimal mayor que 0")
        if not isinstance(self.moneda, str) or not (
            len(self.moneda) == 3 and self.moneda.isascii() and self.moneda.isupper()
        ):
            raise PolizaInvalida("moneda: se espera un código ISO 4217")
        if not _texto(self.periodicidad):
            raise PolizaInvalida("periodicidad: obligatoria")
        if not isinstance(self.cliente_id, UUID):
            raise PolizaInvalida("cliente_id: se espera un uuid")
        if self.socio_id is not None and not _texto(self.socio_id):
            raise PolizaInvalida("socio_id: vacío; se espera None o el identificador")
        if not _con_zona(self.aceptada_en):
            raise PolizaInvalida("aceptada_en: se espera una hora con zona")
        for campo in ("transaccion_id", "version_reglas_prima", "version_reglas_suscripcion"):
            if not _texto(getattr(self, campo)):
                raise PolizaInvalida(f"{campo}: obligatorio")
        if not _con_zona(self.fecha_documento):
            raise PolizaInvalida("fecha_documento: se espera una hora con zona")


def datos_documento(
    poliza: Poliza, fuente: FuenteDocumento, fecha_documento: datetime
) -> DatosDocumento:
    return DatosDocumento(
        numero=poliza.numero,
        ramo=poliza.ramo,
        producto=poliza.producto,
        vigencia=poliza.vigencia,
        coberturas=fuente.coberturas,
        suma_asegurada=fuente.suma_asegurada,
        prima=fuente.prima,
        periodicidad=fuente.periodicidad,
        moneda=fuente.moneda,
        cliente_id=poliza.cliente_id,
        socio_id=fuente.socio_id,
        aceptada_en=fuente.aceptada_en,
        transaccion_id=fuente.transaccion_id,
        version_reglas_prima=fuente.version_reglas_prima,
        version_reglas_suscripcion=fuente.version_reglas_suscripcion,
        fecha_documento=fecha_documento,
    )


def clave_documento(poliza: Poliza) -> str:
    """`polizas/<polizaId>/<numero>.pdf`: fija desde la creación (el número se reserva al crear), así
    que cada reintento sube otra versión del mismo objeto. El último segmento es el nombre del
    archivo al descargarlo."""
    return f"polizas/{poliza.poliza_id}/{poliza.numero}.pdf"
