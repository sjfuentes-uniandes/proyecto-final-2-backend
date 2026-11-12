"""CT-W21-01 (N2): los esquemas de `EventoParametricoObservado.v1` y `EventoParametricoEvaluado.v1`
y el consumo de `PolizaEmitida.v1` con `coberturaParametrica` (HU-W21, W21-B02, DS3-20 opción A).

Los lectores de este servicio son tolerantes (solo los campos que usan), pero sus nombres y formatos
salen del esquema publicado: si el productor los cambia, esta prueba lo avisa.

Regenerar el evento evaluado con: uv run python -m pagos_parametricos.adapters.outbound.eventos
"""

import copy
import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from jsonschema import Draft202012Validator
from pagos_parametricos.adapters.inbound.sqs.eventos import (
    EventoObservadoV1,
    SobreEventoObservadoV1,
)
from pagos_parametricos.adapters.inbound.sqs.polizas import (
    CoberturaParametricaV1,
    SobrePolizaEmitidaV1,
)
from pagos_parametricos.adapters.outbound.evaluaciones_pg import sobre_de
from pagos_parametricos.adapters.outbound.eventos import ESQUEMAS
from pagos_parametricos.domain.parametrico.evaluacion import evaluar
from pagos_parametricos.domain.parametrico.modelos import (
    Cobertura,
    EstadoCobertura,
    EventoObservado,
    Regla,
)

EVENTOS = Path(__file__).parents[4] / "contracts" / "events"
EVALUADO = "EventoParametricoEvaluado.v1.schema.json"
OBSERVADO = "EventoParametricoObservado.v1.schema.json"
POLIZA_EMITIDA = "PolizaEmitida.v1.schema.json"
AHORA = datetime(2026, 11, 10, 14, 0, 6, tzinfo=UTC)


def _esquema(archivo: str) -> dict[str, Any]:
    contenido: dict[str, Any] = json.loads((EVENTOS / archivo).read_text(encoding="utf-8"))
    return contenido


def _errores(archivo: str, sobre: dict[str, Any]) -> list[str]:
    validador = Draft202012Validator(
        _esquema(archivo), format_checker=Draft202012Validator.FORMAT_CHECKER
    )
    return [error.message for error in validador.iter_errors(sobre)]


def _evento() -> EventoObservado:
    return EventoObservado(
        evento_id=UUID("bafc2b26-7c88-5ae3-b5ac-26fdf5427a09"),
        fuente="IDEAM-SIM",
        id_externo="IDEAM-2026-1110-0001",
        evento_origen="EVT-2026-0913",
        tipo_evento="LLUVIA_ACUMULADA",
        valor_observado=Decimal(135),
        unidad="mm",
        zona="11001",
        observado_en=datetime(2026, 11, 10, 14, tzinfo=UTC),
    )


def _regla() -> Regla:
    return Regla(
        "LLUVIA_ACUMULADA", "11001", Decimal(120), "mm", Decimal("1400000.00"), "COP", "1",
        datetime(2026, 9, 1, 5, tzinfo=UTC), datetime(2027, 9, 1, 5, tzinfo=UTC),
    )  # fmt: skip


def _sobre_evaluado(candidatas: list[Cobertura]) -> dict[str, Any]:
    sobre = sobre_de(_evento(), evaluar(_evento(), candidatas), AHORA, "corr-contrato")
    return sobre.model_dump(mode="json", by_alias=True)


# --- EventoParametricoEvaluado.v1 (lo publica este servicio) ---------------------------------------
def test_el_esquema_publicado_del_evento_evaluado_coincide_con_el_modelo() -> None:
    assert EVALUADO in ESQUEMAS
    assert _esquema(EVALUADO) == ESQUEMAS[EVALUADO].model_json_schema(by_alias=True)


def test_cada_decision_del_evento_evaluado_cumple_el_esquema() -> None:
    activa = Cobertura(UUID(int=1), EstadoCobertura.ACTIVA, _regla(), AHORA)
    sin_polizas = _sobre_evaluado([])
    con_activa = _sobre_evaluado([activa])
    assert _errores(EVALUADO, sin_polizas) == [] and _errores(EVALUADO, con_activa) == []
    assert sin_polizas["payload"]["decision"] == "SIN_POLIZAS"
    assert sin_polizas["payload"]["razon"] == "SIN_POLIZAS_EN_ZONA"
    assert con_activa["payload"]["decision"] == "ACTIVA" and con_activa["payload"]["razon"] is None
    # NO_ACTIVA: la misma póliza con un valor bajo el umbral.
    bajo_umbral = replace(_evento(), valor_observado=Decimal(119))
    bajo = sobre_de(bajo_umbral, evaluar(bajo_umbral, [activa]), AHORA, "c").model_dump(
        mode="json", by_alias=True
    )
    assert _errores(EVALUADO, bajo) == [] and bajo["payload"]["decision"] == "NO_ACTIVA"


def test_el_evento_evaluado_fija_tipo_version_e_identidad() -> None:
    sobre = _sobre_evaluado([])
    propiedades = _esquema(EVALUADO)["properties"]
    assert propiedades["type"]["const"] == "EventoParametricoEvaluado"
    assert propiedades["version"]["const"] == 1
    assert sobre["aggregateId"] == sobre["payload"]["eventoId"]
    assert sobre["eventId"] != sobre["aggregateId"]


def test_el_evento_evaluado_exige_todos_sus_campos_y_no_lleva_valores_ni_polizas() -> None:
    sobre = _sobre_evaluado([])
    for campo in sobre["payload"]:
        incompleto = copy.deepcopy(sobre)
        del incompleto["payload"][campo]
        assert _errores(EVALUADO, incompleto), f"aceptó el payload sin {campo}"
    texto = json.dumps(sobre)
    assert "135" not in texto.replace("2026", "") and "polizaId" not in texto
    assert "valorObservado" not in texto


@pytest.mark.parametrize(
    ("campo", "valor"),
    [
        ("decision", "TAL_VEZ"),
        ("razon", "BAJO_UMBRAL"),
        ("activaciones", -1),
        ("evaluadas", "2"),
        ("fuente", "ideam-sim"),
        ("versionRegla", ""),
        ("evaluadoEn", "2026-11-10T14:00:06"),
    ],
)
def test_el_esquema_rechaza_cada_desvio_del_evento_evaluado(campo: str, valor: object) -> None:
    sobre = _sobre_evaluado([])
    sobre["payload"][campo] = valor
    assert _errores(EVALUADO, sobre)


# --- EventoParametricoObservado.v1 (lo consume) --------------------------------------------------
def test_el_lector_del_evento_observado_usa_los_nombres_del_esquema_publicado() -> None:
    propiedades = _esquema(OBSERVADO)["$defs"]["EventoParametricoObservadoV1"]["properties"]
    obligatorios = {
        campo.alias or nombre
        for nombre, campo in EventoObservadoV1.model_fields.items()
        if nombre != "poliza_id"
    }
    assert obligatorios <= set(propiedades)
    assert set(propiedades) == obligatorios  # el lector lee todo lo que el productor publica
    assert SobreEventoObservadoV1.model_fields["type"].annotation is not None


def test_cada_evento_observado_del_archivo_de_datos_cumple_el_esquema_y_lo_lee_el_consumidor(
    datos: dict[str, Any], observado: Callable[[dict[str, Any]], dict[str, Any]]
) -> None:
    aceptados = [c for c in datos["eventos"] if c["esperado"]["http"]["estado"] == 202]
    assert len(aceptados) == 8
    for caso in aceptados:
        sobre = observado(caso)
        assert _errores(OBSERVADO, sobre) == [], caso["clave"]
        leido = SobreEventoObservadoV1.model_validate(sobre)
        assert leido.payload.evento_id == UUID(caso["esperado"]["eventoId"])


def test_el_mensaje_envenenado_del_archivo_de_datos_incumple_el_esquema_y_el_lector(
    datos: dict[str, Any],
) -> None:
    envenenado = next(m for m in datos["mensajesCola"] if m["clave"] == "envenenadoSinZona")
    assert _errores(OBSERVADO, envenenado["sobre"])
    with pytest.raises(ValueError):
        SobreEventoObservadoV1.model_validate(envenenado["sobre"])


# --- PolizaEmitida.v1 con coberturaParametrica (lo consume) --------------------------------------
def test_cada_cobertura_sintetica_cumple_el_esquema_de_polizaemitida_y_la_lee_el_consumidor(
    datos: dict[str, Any],
) -> None:
    assert len(datos["coberturas"]) == 10
    for entrada in datos["coberturas"]:
        assert _errores(POLIZA_EMITIDA, entrada["sobre"]) == [], entrada["clave"]
        leido = SobrePolizaEmitidaV1.model_validate(entrada["sobre"])
        assert leido.payload.cobertura_parametrica is not None


def test_el_lector_de_la_cobertura_usa_los_nombres_del_esquema_publicado() -> None:
    definicion = _esquema(POLIZA_EMITIDA)["$defs"]["CoberturaParametricaV1"]
    campos = {
        campo.alias or nombre for nombre, campo in CoberturaParametricaV1.model_fields.items()
    }
    assert campos == set(definicion["properties"]) == set(definicion["required"])


def test_una_emision_sin_cobertura_o_con_cobertura_nula_cumple_el_esquema_y_se_lee(
    datos: dict[str, Any],
) -> None:
    base = copy.deepcopy(datos["coberturas"][0]["sobre"])
    for variante in ("ausente", "nula"):
        sobre = copy.deepcopy(base)
        if variante == "ausente":
            del sobre["payload"]["coberturaParametrica"]
        else:
            sobre["payload"]["coberturaParametrica"] = None
        assert _errores(POLIZA_EMITIDA, sobre) == []
        assert SobrePolizaEmitidaV1.model_validate(sobre).payload.cobertura_parametrica is None
