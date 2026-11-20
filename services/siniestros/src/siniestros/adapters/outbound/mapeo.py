"""Conversión entre el dominio y las columnas `jsonb` de siniestros (HU-W19). La copia de la póliza
usa las claves de `CopiaPolizaDto` (camelCase) para que la semilla y los datos reales coincidan."""

from datetime import date, datetime
from typing import Any
from uuid import UUID

from siniestros.domain.siniestro.modelos import (
    Amparo,
    Beneficiario,
    CopiaPoliza,
    EventoParametrico,
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


def regla_a_json(evento: EventoParametrico) -> dict[str, Any]:
    """La regla aplicada (DS3-21): su código y su versión."""
    return {"codigo": evento.regla, "version": evento.version_regla}


def medicion_a_json(evento: EventoParametrico) -> dict[str, Any]:
    """La medición del evento (HU-W22); el `eventoId` va en su propia columna."""
    return {
        "fuente": evento.fuente,
        "tipoEvento": evento.tipo_evento,
        "valorObservado": evento.valor_observado,
        "umbral": evento.umbral,
        "unidad": evento.unidad,
        "zona": evento.zona,
        "observadoEn": evento.observado_en.isoformat(),
        "eventoOrigen": evento.evento_origen,
    }


def evento_de_json(
    evento_id: UUID | None, regla: dict[str, Any] | None, medicion: dict[str, Any] | None
) -> EventoParametrico | None:
    """Null si el siniestro no tiene medición (otro origen, o un paramétrico anterior a W22)."""
    if evento_id is None or regla is None or medicion is None:
        return None
    return EventoParametrico(
        evento_id=evento_id,
        fuente=medicion["fuente"],
        tipo_evento=medicion["tipoEvento"],
        valor_observado=medicion["valorObservado"],
        umbral=medicion["umbral"],
        unidad=medicion["unidad"],
        zona=medicion["zona"],
        observado_en=datetime.fromisoformat(medicion["observadoEn"]),
        evento_origen=medicion["eventoOrigen"],
        regla=regla["codigo"],
        version_regla=regla["version"],
    )
