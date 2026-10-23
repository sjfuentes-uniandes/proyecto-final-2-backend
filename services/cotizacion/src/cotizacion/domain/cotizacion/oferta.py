"""Creación de la oferta a partir de la prima (HU-W04). Pura: sin reloj ni E/S.

La oferta toma de la cotización el precio (la prima total), la moneda, la periodicidad y las
coberturas incluidas, y de las reglas de prima sus condiciones: vigencia, exclusiones resumidas y la
parte de la suma asegurada de cada cobertura. Las reglas publicadas antes de HU-W04 no traen esas
condiciones: la vigencia es entonces la de la configuración (`oferta_vigencia_horas`, HU-W13) y la
oferta no tiene exclusiones.

`vigenteHasta` conserva el nombre y el significado que fijó HU-W13 (DS2-20): instante UTC y
vigencia semiabierta, vigente si ahora < vigenteHasta.
"""

from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from uuid import UUID

from cotizacion.domain.cotizacion.modelos import (
    CoberturaOfertada,
    Cotizacion,
    Oferta,
    ReglasPrima,
)
from cotizacion.domain.cotizacion.prima import CONTEXTO, redondear
from cotizacion.domain.cotizacion.vigencia import vigente_hasta

# Versión del documento de la oferta; cambia solo si cambia su significado.
VERSION_OFERTA = 1


def fin_de_vigencia(
    calculado_en: datetime, reglas: ReglasPrima, vigencia_horas_por_omision: int
) -> datetime:
    if reglas.oferta is None:
        return vigente_hasta(calculado_en, vigencia_horas_por_omision)
    return calculado_en + timedelta(minutes=reglas.oferta.vigencia_minutos)


def crear_oferta(
    cotizacion: Cotizacion, reglas: ReglasPrima, oferta_id: UUID, vigencia_horas_por_omision: int
) -> Oferta:
    """La oferta de la cotización recién calculada con esas reglas."""
    condiciones = reglas.oferta
    porcentajes = condiciones.porcentajes_suma if condiciones else {}
    suma = cotizacion.entradas.suma_asegurada
    with localcontext(CONTEXTO):
        coberturas = tuple(
            CoberturaOfertada(
                c.codigo, c.nombre, redondear(suma * porcentajes.get(c.codigo, Decimal(1)), reglas)
            )
            for c in cotizacion.coberturas
        )
    return Oferta(
        id=oferta_id,
        cotizacion_id=cotizacion.id,
        precio=cotizacion.desglose.prima_total,
        moneda=cotizacion.moneda,
        periodicidad=cotizacion.periodicidad,
        coberturas=coberturas,
        exclusiones=condiciones.exclusiones if condiciones else (),
        version=VERSION_OFERTA,
        version_reglas=cotizacion.version_reglas,
        creada_en=cotizacion.calculado_en,
        vigente_hasta=fin_de_vigencia(cotizacion.calculado_en, reglas, vigencia_horas_por_omision),
    )


def vigente_hasta_de(cotizacion: Cotizacion, vigencia_horas_por_omision: int) -> datetime:
    """Fin de la vigencia de la oferta de la cotización. Las cotizaciones anteriores a la migración
    004 no tienen oferta guardada: se deriva como en HU-W13."""
    if cotizacion.oferta is not None:
        return cotizacion.oferta.vigente_hasta
    return vigente_hasta(cotizacion.calculado_en, vigencia_horas_por_omision)
