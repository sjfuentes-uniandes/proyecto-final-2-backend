"""Huella de la evidencia de aceptación (HU-W15, W15-B02).

SHA-256 del JSON canónico (claves ordenadas, sin espacios, instantes UTC con `Z`), con la misma
función `huella` de los insumos de la suscripción. Cubre lo que el tomador aceptó y quién y cuándo
lo registró: cotización, oferta, decisión, `huellaCalculo`, `vigenteHasta`, versión del texto de
las declaraciones, declaraciones, cliente, actor, canal e instante. Todo está en la fila guardada,
así que la huella se recalcula igual desde la base (evidencia inmutable).
"""

from typing import Any

from polizas.domain.aceptacion.modelos import Aceptacion, Declaraciones
from polizas.domain.suscripcion.insumos import huella, instante


def declaraciones_canonicas(declaraciones: Declaraciones) -> dict[str, bool]:
    return {
        "leidoCondiciones": declaraciones.leido_condiciones,
        "informacionVeraz": declaraciones.informacion_veraz,
        "debitoAutomatico": declaraciones.debito_automatico,
        "avisosElectronicos": declaraciones.avisos_electronicos,
    }


def evidencia_canonica(aceptacion: Aceptacion) -> dict[str, Any]:
    return {
        "cotizacionId": str(aceptacion.cotizacion_id),
        "ofertaId": None if aceptacion.oferta_id is None else str(aceptacion.oferta_id),
        "decisionId": str(aceptacion.decision_id),
        "huellaCalculo": aceptacion.huella_calculo,
        "vigenteHasta": instante(aceptacion.vigente_hasta),
        "versionDeclaraciones": aceptacion.version_declaraciones,
        "declaraciones": declaraciones_canonicas(aceptacion.declaraciones),
        "clienteId": str(aceptacion.cliente_id),
        "actorId": aceptacion.actor_id,
        "canal": aceptacion.canal.value,
        "aceptadaEn": instante(aceptacion.aceptada_en),
    }


def huella_evidencia(aceptacion: Aceptacion) -> str:
    """La huella no depende del campo `huella_evidencia` de la propia aceptación."""
    return huella(evidencia_canonica(aceptacion))
