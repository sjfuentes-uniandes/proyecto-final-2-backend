"""CT-W21-01: el esquema publicado de `EventoParametricoObservado.v1` es el que genera el modelo, y
cada evento que arma el servicio lo cumple, sin el cuerpo ni la firma (HU-W21, W21-B02).

Si cambia el evento, regenerar con: uv run python -m adaptador_telemetria.adapters.outbound.eventos
"""

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from adaptador_telemetria.adapters.outbound.eventos import ESQUEMAS, sobre_de
from adaptador_telemetria.domain.telemetria.contrato import validar_contrato
from adaptador_telemetria.domain.telemetria.modelos import clave_archivo
from jsonschema import Draft202012Validator

EVENTOS = Path(__file__).parents[4] / "contracts" / "events"
ARCHIVO = "EventoParametricoObservado.v1.schema.json"
RECIBIDO = datetime(2026, 11, 10, 14, 0, 5, tzinfo=UTC)


def _publicado() -> dict[str, Any]:
    esquema: dict[str, Any] = json.loads((EVENTOS / ARCHIVO).read_text(encoding="utf-8"))
    return esquema


def _errores(sobre: dict[str, Any]) -> list[str]:
    validador = Draft202012Validator(
        _publicado(), format_checker=Draft202012Validator.FORMAT_CHECKER
    )
    return [error.message for error in validador.iter_errors(sobre)]


def _sobre_de_un_caso(caso: dict[str, Any]) -> dict[str, Any]:
    evento = validar_contrato(json.dumps(caso["envio"]["cuerpo"]).encode(), caso["envio"]["fuente"])
    clave = clave_archivo("telemetria/", RECIBIDO, evento.evento_id)
    return sobre_de(evento, RECIBIDO, clave, "corr-contrato").model_dump(mode="json", by_alias=True)


def test_el_esquema_publicado_coincide_con_el_modelo() -> None:
    assert list(ESQUEMAS) == [ARCHIVO]
    assert _publicado() == ESQUEMAS[ARCHIVO].model_json_schema(by_alias=True)


def test_el_sobre_fija_el_tipo_y_la_version() -> None:
    propiedades = _publicado()["properties"]
    assert propiedades["type"]["const"] == "EventoParametricoObservado"
    assert propiedades["version"]["const"] == 1


def test_cada_evento_aceptado_del_archivo_de_datos_cumple_el_esquema(
    datos: dict[str, Any],
) -> None:
    aceptados = [c for c in datos["eventos"] if c["esperado"]["http"]["estado"] == 202]
    assert len(aceptados) == 8
    for caso in aceptados:
        sobre = _sobre_de_un_caso(caso)
        assert _errores(sobre) == [], caso["clave"]
        # eventId = aggregateId = eventoId (DS3-16): el reproceso de W24 reutiliza la identidad.
        assert sobre["eventId"] == sobre["aggregateId"] == sobre["payload"]["eventoId"]
        assert sobre["eventId"] == caso["esperado"]["eventoId"]
        assert sobre["payload"]["archivoRef"].endswith(f"/{sobre['eventId']}.json")


def test_el_evento_no_lleva_el_cuerpo_ni_la_firma_ni_la_ubicacion(
    casos: dict[str, dict[str, Any]],
) -> None:
    sobre = _sobre_de_un_caso(casos["valido"])
    texto = json.dumps(sobre)
    assert "ubicacion" not in texto and "4.711" not in texto
    assert "versionContrato" not in texto
    assert "sha256=" not in texto and "firma" not in texto.lower()


def test_el_payload_exige_todos_sus_campos(casos: dict[str, dict[str, Any]]) -> None:
    ejemplo = _sobre_de_un_caso(casos["valido"])
    for campo in ejemplo["payload"]:
        sobre = copy.deepcopy(ejemplo)
        del sobre["payload"][campo]
        assert _errores(sobre), f"aceptó el payload sin {campo}"


@pytest.mark.parametrize(
    ("campo", "valor"),
    [
        ("tipoEvento", "GRANIZO"),
        ("unidad", "km"),
        ("valorObservado", "135,5"),
        ("valorObservado", 135),
        ("zona", "bogota"),
        ("fuente", "ideam-sim"),
        ("archivoRef", "https://bucket.s3.amazonaws.com/x.json?X-Amz-Signature=abc"),
        ("archivoRef", "telemetria/2026/11/10/no-es-un-uuid.json"),
        ("recibidoEn", "2026-11-10T14:00:05"),
        ("eventoId", "no-es-un-uuid"),
    ],
)
def test_el_esquema_rechaza_cada_desvio(
    casos: dict[str, dict[str, Any]], campo: str, valor: object
) -> None:
    sobre = _sobre_de_un_caso(casos["valido"])
    sobre["payload"][campo] = valor
    assert _errores(sobre)


@pytest.mark.parametrize(
    ("campo", "valor"), [("type", "EventoParametricoEvaluado"), ("version", 2)]
)
def test_el_esquema_rechaza_otro_tipo_u_otra_version(
    casos: dict[str, dict[str, Any]], campo: str, valor: object
) -> None:
    sobre = _sobre_de_un_caso(casos["valido"])
    sobre[campo] = valor
    assert _errores(sobre)
