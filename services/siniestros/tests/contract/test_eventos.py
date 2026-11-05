"""Esquemas v1 de los eventos de siniestros (HU-W19): los publicados son los que genera el código,
y cada evento cumple el suyo sin URL, clave de objeto ni nombres.

Si cambia un evento, regenerar con: uv run python -m siniestros.adapters.outbound.eventos
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator
from pydantic import BaseModel
from siniestros.adapters.outbound.eventos import (
    ACCESO_SINIESTRO_DENEGADO,
    ESQUEMAS,
    EVIDENCIA_CONSULTADA,
    SINIESTRO_CONSULTADO,
    SINIESTRO_EN_EVALUACION,
    AccesoSiniestroDenegadoV1,
    EvidenciaConsultadaV1,
    SiniestroConsultadoV1,
    SiniestroEnEvaluacionV1,
    SobreEvento,
)

EVENTOS = Path(__file__).parents[4] / "contracts" / "events"
AHORA = datetime(2026, 11, 5, 15, 0, tzinfo=UTC)
SINIESTRO = uuid4()
EVIDENCIA = uuid4()
ANALISTA = uuid4()


def _sobre(tipo: str, payload: BaseModel, aggregate_id: Any = SINIESTRO) -> dict[str, Any]:
    sobre = SobreEvento[type(payload)](  # type: ignore[misc]
        event_id=uuid4(),
        aggregate_id=aggregate_id,
        type=tipo,
        version=1,
        occurred_at=AHORA,
        correlation_id="corr-w19",
        payload=payload,
    )
    return sobre.model_dump(mode="json", by_alias=True)


def _validador(tipo: str) -> Draft202012Validator:
    esquema = json.loads((EVENTOS / f"{tipo}.v1.schema.json").read_text(encoding="utf-8"))
    return Draft202012Validator(esquema, format_checker=Draft202012Validator.FORMAT_CHECKER)


EJEMPLOS = [
    (
        SINIESTRO_EN_EVALUACION,
        SiniestroEnEvaluacionV1(
            siniestro_id=SINIESTRO,
            codigo="88-2026-0442",
            poliza_id=uuid4(),
            analista_id=ANALISTA,
            motivo="ASIGNACION",
        ),
    ),
    (
        SINIESTRO_CONSULTADO,
        SiniestroConsultadoV1(
            siniestro_id=SINIESTRO, codigo="88-2026-0442", actor_id=str(ANALISTA)
        ),
    ),
    (
        EVIDENCIA_CONSULTADA,
        EvidenciaConsultadaV1(
            siniestro_id=SINIESTRO,
            evidencia_id=EVIDENCIA,
            actor_id=str(ANALISTA),
            expira_en=AHORA,
        ),
    ),
    (
        ACCESO_SINIESTRO_DENEGADO,
        AccesoSiniestroDenegadoV1(
            actor_id=str(ANALISTA),
            rol=None,
            recurso="BANDEJA",
            recurso_id=None,
            motivo="SIN_GRUPO",
            origen="BFF_WEB",
        ),
    ),
]


def test_los_cuatro_eventos_se_publican() -> None:
    assert sorted(ESQUEMAS) == [
        "AccesoSiniestroDenegado.v1.schema.json",
        "EvidenciaConsultada.v1.schema.json",
        "SiniestroConsultado.v1.schema.json",
        "SiniestroEnEvaluacion.v1.schema.json",
    ]


@pytest.mark.parametrize(("archivo", "modelo"), ESQUEMAS.items())
def test_esquema_publicado_coincide(archivo: str, modelo: type[BaseModel]) -> None:
    publicado = json.loads((EVENTOS / archivo).read_text(encoding="utf-8"))
    assert publicado == modelo.model_json_schema(by_alias=True)


@pytest.mark.parametrize(("tipo", "payload"), EJEMPLOS)
def test_cada_evento_cumple_su_esquema(tipo: str, payload: BaseModel) -> None:
    sobre = _sobre(tipo, payload)
    assert list(_validador(tipo).iter_errors(sobre)) == []
    assert sobre["type"] == tipo
    assert sobre["version"] == 1


def test_la_evidencia_consultada_no_lleva_url_ni_clave_de_objeto() -> None:
    payload = _sobre(*EJEMPLOS[2])["payload"]
    assert set(payload) == {"siniestroId", "evidenciaId", "actorId", "expiraEn"}


@pytest.mark.parametrize(
    ("tipo", "cambio"),
    [
        (SINIESTRO_EN_EVALUACION, {"motivo": "PLAZO_VENCIDO"}),
        (SINIESTRO_EN_EVALUACION, {"codigo": "99-2026-0442"}),
        (SINIESTRO_CONSULTADO, {"actorId": ""}),
        (EVIDENCIA_CONSULTADA, {"expiraEn": "mañana"}),
        (ACCESO_SINIESTRO_DENEGADO, {"origen": "PORTAL"}),
        (ACCESO_SINIESTRO_DENEGADO, {"motivo": "OTRO"}),
        (ACCESO_SINIESTRO_DENEGADO, {"recurso": "NOMINA"}),
    ],
)
def test_un_payload_fuera_del_contrato_se_rechaza(tipo: str, cambio: dict[str, Any]) -> None:
    payload = next(p for t, p in EJEMPLOS if t == tipo)
    sobre = _sobre(tipo, payload)
    sobre["payload"].update(cambio)
    assert list(_validador(tipo).iter_errors(sobre)) != []
