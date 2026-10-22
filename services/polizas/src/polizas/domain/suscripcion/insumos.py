"""Forma canónica de los insumos de la oferta y su huella (HU-W13).

Los insumos se guardan con la decisión en esta forma (las claves del contrato de `cotizacion`,
decimales como texto e instantes UTC con `Z`) y se reconstruyen desde ella: la reevaluación usa
exactamente lo que se evaluó. La huella es SHA-256 del JSON canónico (claves ordenadas, sin
espacios), como la de las reglas: el mismo insumo da siempre la misma huella.
"""

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from polizas.domain.suscripcion.modelos import (
    Cobertura,
    EstadoIdentidad,
    InsumosSuscripcion,
    NivelRiesgo,
    PerfilInsumos,
)


def texto(valor: Decimal) -> str:
    """Decimal como texto sin exponente (DS2-16)."""
    return format(valor, "f")


def instante(valor: datetime) -> str:
    return valor.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _uuid(valor: UUID | None) -> str | None:
    return None if valor is None else str(valor)


def insumos_canonicos(insumos: InsumosSuscripcion) -> dict[str, Any]:
    perfil = insumos.perfil
    return {
        "cotizacionId": str(insumos.cotizacion_id),
        "ofertaId": _uuid(insumos.oferta_id),
        "codigo": insumos.codigo,
        "clienteId": str(insumos.cliente_id),
        "estadoIdentidad": insumos.estado_identidad.value,
        "socioId": insumos.socio_id,
        "productoId": insumos.producto_id,
        "versionProducto": insumos.version_producto,
        "moneda": insumos.moneda,
        "periodicidad": insumos.periodicidad,
        "primaTotal": texto(insumos.prima_total),
        "versionReglasPrima": insumos.version_reglas_prima,
        "huellaCalculo": insumos.huella_calculo,
        "coberturas": [
            {"codigo": c.codigo, "nombre": c.nombre, "obligatoria": c.obligatoria}
            for c in insumos.coberturas
        ],
        "sumaAsegurada": texto(insumos.suma_asegurada),
        "plazoCreditoMeses": insumos.plazo_credito_meses,
        "edad": insumos.edad,
        "calculadoEn": instante(insumos.calculado_en),
        "vigenteHasta": instante(insumos.vigente_hasta),
        "perfil": None
        if perfil is None
        else {
            "id": str(perfil.id),
            "estado": perfil.estado,
            "nivelRiesgo": perfil.nivel_riesgo.value if perfil.nivel_riesgo else None,
            "confianza": texto(perfil.confianza),
            "fuentesConsultadas": perfil.fuentes_consultadas,
            "fuentesDeclaradas": perfil.fuentes_declaradas,
        },
        "coincidenciasListasRestrictivas": insumos.coincidencias_listas_restrictivas,
    }


def insumos_de(datos: Mapping[str, Any]) -> InsumosSuscripcion:
    """Insumos desde su forma canónica (la de la base y la del contrato de cotizacion)."""
    perfil = datos["perfil"]
    oferta = datos["ofertaId"]
    return InsumosSuscripcion(
        cotizacion_id=UUID(datos["cotizacionId"]),
        oferta_id=UUID(oferta) if oferta else None,
        codigo=datos["codigo"],
        cliente_id=UUID(datos["clienteId"]),
        estado_identidad=EstadoIdentidad(datos["estadoIdentidad"]),
        socio_id=datos["socioId"],
        producto_id=datos["productoId"],
        version_producto=datos["versionProducto"],
        moneda=datos["moneda"],
        periodicidad=datos["periodicidad"],
        prima_total=Decimal(datos["primaTotal"]),
        version_reglas_prima=datos["versionReglasPrima"],
        huella_calculo=datos["huellaCalculo"],
        coberturas=tuple(
            Cobertura(c["codigo"], c["nombre"], c["obligatoria"]) for c in datos["coberturas"]
        ),
        suma_asegurada=Decimal(datos["sumaAsegurada"]),
        plazo_credito_meses=datos["plazoCreditoMeses"],
        edad=datos["edad"],
        calculado_en=datetime.fromisoformat(datos["calculadoEn"]),
        vigente_hasta=datetime.fromisoformat(datos["vigenteHasta"]),
        perfil=None
        if perfil is None
        else PerfilInsumos(
            id=UUID(perfil["id"]),
            estado=perfil["estado"],
            nivel_riesgo=NivelRiesgo(perfil["nivelRiesgo"]) if perfil["nivelRiesgo"] else None,
            confianza=Decimal(perfil["confianza"]),
            fuentes_consultadas=perfil["fuentesConsultadas"],
            fuentes_declaradas=perfil["fuentesDeclaradas"],
        ),
        coincidencias_listas_restrictivas=datos["coincidenciasListasRestrictivas"],
    )


def huella(contenido: Mapping[str, Any]) -> str:
    canonico = json.dumps(contenido, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonico.encode("utf-8")).hexdigest()


def hash_insumos(insumos: InsumosSuscripcion) -> str:
    return huella(insumos_canonicos(insumos))
