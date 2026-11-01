"""La lectura de la póliza (HU-W17, 17.9; ADR 0014, decisiones 4 y 5). Puros: sin Pydantic ni E/S.

- `VistaPoliza` es lo que ve el portal: la póliza, más lo que toma de la oferta aceptada (la
  instantánea de su decisión, DS2-10) y del recaudo confirmado. Sin la clave del objeto, el último
  fallo ni la reserva, que son internos.
- `verificar` es la regla de «verificable» (decisión 5): una póliza ACTIVA cuyo número y huella son
  los firmados. Cualquier otro caso da la misma respuesta, sin decir si el número existe.
"""

import hmac
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from polizas.domain.aceptacion.modelos import Aceptacion
from polizas.domain.poliza.modelos import EstadoPoliza, Firma, Poliza
from polizas.domain.recaudo.modelos import ResultadoRecaudo
from polizas.domain.suscripcion.modelos import Cobertura, InsumosSuscripcion


@dataclass(frozen=True, slots=True)
class VistaPoliza:
    poliza: Poliza
    coberturas: tuple[Cobertura, ...]
    suma_asegurada: Decimal
    prima: Decimal
    moneda: str
    periodicidad: str
    aceptada_en: datetime
    recaudada_en: datetime


def vista_poliza(
    poliza: Poliza,
    aceptacion: Aceptacion,
    insumos: InsumosSuscripcion,
    recaudo: ResultadoRecaudo,
) -> VistaPoliza:
    """La prima, la suma, la moneda y la periodicidad son las de la oferta aceptada, como en el PDF
    y en `PolizaEmitida`."""
    return VistaPoliza(
        poliza=poliza,
        coberturas=insumos.coberturas,
        suma_asegurada=insumos.suma_asegurada,
        prima=insumos.prima_total,
        moneda=insumos.moneda,
        periodicidad=insumos.periodicidad,
        aceptada_en=aceptacion.aceptada_en,
        recaudada_en=recaudo.ocurrido_en,
    )


@dataclass(frozen=True, slots=True)
class EnlaceDocumento:
    """La URL prefirmada de la versión registrada del PDF y cuándo vence."""

    url: str
    vence_en: datetime


@dataclass(frozen=True, slots=True)
class Verificacion:
    valido: bool
    numero: str
    firma: Firma | None


def verificar(poliza: Poliza | None, numero: str, huella: str) -> Verificacion:
    """Válida solo si la póliza existe con ese número, está ACTIVA y `huella` es la del documento
    registrado, que es la firmada (`Poliza.registrar_firma`). Si no, la misma respuesta, sin la
    razón; la huella se compara en tiempo constante."""
    if (
        poliza is None
        or poliza.numero != numero
        or poliza.estado is not EstadoPoliza.ACTIVA
        or poliza.documento is None
        or poliza.firma is None
        or not hmac.compare_digest(poliza.documento.huella.encode(), huella.encode())
    ):
        return Verificacion(valido=False, numero=numero, firma=None)
    return Verificacion(valido=True, numero=numero, firma=poliza.firma)
