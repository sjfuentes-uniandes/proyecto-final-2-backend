"""Validación de la aceptación (HU-W15, W15-B02). Pura: el instante y la versión vigente los recibe.

Precedencia (la primera que falla decide):

| # | Condición                                            | Error                      |
| - | ---------------------------------------------------- | -------------------------- |
| 1 | versionDeclaraciones ≠ la vigente (DS2-24)           | TextoNoVigente             |
| 2 | la decisión no es APROBADA                           | DecisionNoAprobada         |
| 3 | ahora ≥ vigenteHasta de la instantánea (DS2-10)      | OfertaVencida              |
| 4 | falta leidoCondiciones o informacionVeraz            | DeclaracionesIncompletas   |

La vigencia es la de `domain/suscripcion/vigencia.py`: el instante exacto `vigenteHasta` ya es
vencido. Débito automático y avisos electrónicos se guardan con su valor, pero no son obligatorios.
"""

from datetime import datetime

from polizas.domain.aceptacion.errores import (
    DecisionNoAprobada,
    DeclaracionesIncompletas,
    OfertaVencida,
    TextoNoVigente,
)
from polizas.domain.aceptacion.modelos import Declaraciones
from polizas.domain.suscripcion.modelos import Decision, ResultadoDecision
from polizas.domain.suscripcion.vigencia import oferta_vigente


def declaraciones_faltantes(declaraciones: Declaraciones) -> tuple[str, ...]:
    """Las declaraciones obligatorias sin aceptar, con su nombre del contrato y en su orden."""
    obligatorias = (
        ("leidoCondiciones", declaraciones.leido_condiciones),
        ("informacionVeraz", declaraciones.informacion_veraz),
    )
    return tuple(nombre for nombre, aceptada in obligatorias if not aceptada)


def validar_aceptacion(
    decision: Decision,
    declaraciones: Declaraciones,
    version_declaraciones: str,
    version_vigente: str,
    ahora: datetime,
) -> None:
    """No devuelve nada si la aceptación procede; si no, lanza el error de la primera condición."""
    if version_declaraciones != version_vigente:
        raise TextoNoVigente()
    if decision.resultado is not ResultadoDecision.APROBADA:
        raise DecisionNoAprobada()
    if not oferta_vigente(decision.insumos.vigente_hasta, ahora):
        raise OfertaVencida()
    faltantes = declaraciones_faltantes(declaraciones)
    if faltantes:
        raise DeclaracionesIncompletas(faltantes)
