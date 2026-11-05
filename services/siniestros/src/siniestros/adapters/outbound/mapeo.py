"""Conversión entre el dominio y las columnas `jsonb` de siniestros (HU-W19). La copia de la póliza
usa las claves de `CopiaPolizaDto` (camelCase) para que la semilla y los datos reales coincidan."""

from datetime import date
from typing import Any

from siniestros.domain.siniestro.modelos import (
    Amparo,
    Beneficiario,
    CopiaPoliza,
    Ubicacion,
    Vigencia,
)


def ubicacion_a_json(ubicacion: Ubicacion | None) -> dict[str, Any] | None:
    if ubicacion is None:
        return None
    return {"descripcion": ubicacion.descripcion, "lat": ubicacion.lat, "lon": ubicacion.lon}


def ubicacion_de_json(datos: dict[str, Any] | None) -> Ubicacion | None:
    if datos is None:
        return None
    return Ubicacion(datos.get("descripcion"), datos.get("lat"), datos.get("lon"))


def copia_a_json(copia: CopiaPoliza) -> dict[str, Any]:
    datos: dict[str, Any] = {
        "numero": copia.numero,
        "ramo": copia.ramo,
        "moneda": copia.moneda,
        "amparos": [
            {"codigo": a.codigo, "nombre": a.nombre, "sumaAsegurada": a.suma_asegurada}
            for a in copia.amparos
        ],
        "carenciaDias": copia.carencia_dias,
        "plazoAvisoDias": copia.plazo_aviso_dias,
        "primasAlDia": copia.primas_al_dia,
    }
    if copia.vigencia is not None:
        datos["vigencia"] = {
            "desde": copia.vigencia.desde.isoformat(),
            "hasta": copia.vigencia.hasta.isoformat(),
        }
    if copia.beneficiario is not None:
        datos["beneficiario"] = {
            "beneficiarioRef": copia.beneficiario.beneficiario_ref,
            "cuentaMascarada": copia.beneficiario.cuenta_mascarada,
        }
    return datos


def copia_de_json(datos: dict[str, Any]) -> CopiaPoliza:
    vigencia = datos.get("vigencia")
    beneficiario = datos.get("beneficiario")
    return CopiaPoliza(
        numero=datos["numero"],
        ramo=datos["ramo"],
        moneda=datos["moneda"],
        amparos=tuple(
            Amparo(a["codigo"], a["nombre"], a["sumaAsegurada"]) for a in datos.get("amparos", [])
        ),
        vigencia=(
            Vigencia(date.fromisoformat(vigencia["desde"]), date.fromisoformat(vigencia["hasta"]))
            if vigencia
            else None
        ),
        carencia_dias=datos.get("carenciaDias"),
        plazo_aviso_dias=datos.get("plazoAvisoDias"),
        primas_al_dia=datos.get("primasAlDia"),
        beneficiario=(
            Beneficiario(beneficiario["beneficiarioRef"], beneficiario["cuentaMascarada"])
            if beneficiario
            else None
        ),
    )
