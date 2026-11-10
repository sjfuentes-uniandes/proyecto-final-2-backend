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
    SINIESTRO_APROBADO,
    SINIESTRO_CONSULTADO,
    SINIESTRO_EN_EVALUACION,
    SINIESTRO_RECHAZADO,
    SINIESTRO_SOPORTE_SOLICITADO,
    AccesoSiniestroDenegadoV1,
    EvidenciaConsultadaV1,
    SiniestroAprobadoV1,
    SiniestroConsultadoV1,
    SiniestroEnEvaluacionV1,
    SiniestroRechazadoV1,
    SiniestroSoporteSolicitadoV1,
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
    (
        SINIESTRO_APROBADO,
        SiniestroAprobadoV1(
            siniestro_id=SINIESTRO,
            codigo="88-2026-0442",
            origen="MOVIL",
            poliza_id=uuid4(),
            cliente_id=uuid4(),
            valor_indemnizar="2500000.00",
            moneda="COP",
            referencia_pago="IND-88-2026-0442-01",
            beneficiario_ref="ben-sint-001",
            aprobado_en=AHORA,
            decidido_por=ANALISTA,
        ),
    ),
    (
        SINIESTRO_RECHAZADO,
        SiniestroRechazadoV1(
            siniestro_id=SINIESTRO,
            codigo="88-2026-0437",
            poliza_id=uuid4(),
            causal="POLIZA_NO_VIGENTE",
            decidido_por=ANALISTA,
            decidido_en=AHORA,
        ),
    ),
    (
        SINIESTRO_SOPORTE_SOLICITADO,
        SiniestroSoporteSolicitadoV1(
            siniestro_id=SINIESTRO,
            codigo="88-2026-0442",
            poliza_id=uuid4(),
            documentos=["CERTIFICADO_REPARACION", "PERITAJE"],
            canal="PUSH_Y_CORREO",
            plazo_vence_en=AHORA,
            decidido_por=ANALISTA,
        ),
    ),
]


def test_los_siete_eventos_se_publican() -> None:
    assert sorted(ESQUEMAS) == [
        "AccesoSiniestroDenegado.v1.schema.json",
        "EvidenciaConsultada.v1.schema.json",
        "SiniestroAprobado.v1.schema.json",
        "SiniestroConsultado.v1.schema.json",
        "SiniestroEnEvaluacion.v1.schema.json",
        "SiniestroRechazado.v1.schema.json",
        "SiniestroSoporteSolicitado.v1.schema.json",
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
        (SINIESTRO_APROBADO, {"referenciaPago": "IND-0442-01"}),
        (SINIESTRO_APROBADO, {"valorIndemnizar": "-5"}),
        (SINIESTRO_APROBADO, {"origen": "WEB"}),
        (SINIESTRO_APROBADO, {"beneficiarioRef": ""}),
        (SINIESTRO_RECHAZADO, {"causal": "OTRA"}),
        (SINIESTRO_RECHAZADO, {"decididoPor": "no-es-uuid"}),
        (SINIESTRO_SOPORTE_SOLICITADO, {"documentos": []}),
        (SINIESTRO_SOPORTE_SOLICITADO, {"documentos": ["FACTURA_COMPRA"]}),
        (SINIESTRO_SOPORTE_SOLICITADO, {"canal": "SMS"}),
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


def test_el_motivo_de_en_evaluacion_admite_asignacion_y_plazo_vencido() -> None:
    base = next(p for t, p in EJEMPLOS if t == SINIESTRO_EN_EVALUACION)
    for motivo in ("ASIGNACION", "PLAZO_SOPORTE_VENCIDO"):
        sobre = _sobre(SINIESTRO_EN_EVALUACION, base)
        sobre["payload"]["motivo"] = motivo
        assert list(_validador(SINIESTRO_EN_EVALUACION).iter_errors(sobre)) == []


# --- CT-W20-01: los eventos de la decisión no llevan lo que no deben --------------------------------

PROHIBIDAS = (
    "explicacion",
    "mensaje",
    "justificacion",
    "cuenta",
    "nombre",
    "deducible",
    "liquidacion",
    "url",
)


def _claves(valor: Any) -> set[str]:
    if isinstance(valor, dict):
        return set(valor) | {k for v in valor.values() for k in _claves(v)}
    if isinstance(valor, list):
        return {k for v in valor for k in _claves(v)}
    return set()


@pytest.mark.parametrize(
    "tipo", [SINIESTRO_APROBADO, SINIESTRO_RECHAZADO, SINIESTRO_SOPORTE_SOLICITADO]
)
def test_los_eventos_de_decision_no_llevan_texto_al_cliente_cuenta_ni_nombres(tipo: str) -> None:
    payload = next(p for t, p in EJEMPLOS if t == tipo)
    claves = {k.lower() for k in _claves(_sobre(tipo, payload)["payload"])}
    assert not [k for k in claves if any(p in k for p in PROHIBIDAS)]


def test_las_claves_exactas_de_los_tres_eventos_de_decision() -> None:
    claves = {
        tipo: set(_sobre(tipo, payload)["payload"])
        for tipo, payload in EJEMPLOS
        if tipo in (SINIESTRO_APROBADO, SINIESTRO_RECHAZADO, SINIESTRO_SOPORTE_SOLICITADO)
    }
    assert claves[SINIESTRO_APROBADO] == {
        "siniestroId",
        "codigo",
        "origen",
        "polizaId",
        "clienteId",
        "valorIndemnizar",
        "moneda",
        "referenciaPago",
        "beneficiarioRef",
        "aprobadoEn",
        "decididoPor",
    }
    assert claves[SINIESTRO_RECHAZADO] == {
        "siniestroId",
        "codigo",
        "polizaId",
        "causal",
        "decididoPor",
        "decididoEn",
    }
    assert claves[SINIESTRO_SOPORTE_SOLICITADO] == {
        "siniestroId",
        "codigo",
        "polizaId",
        "documentos",
        "canal",
        "plazoVenceEn",
        "decididoPor",
    }


def test_un_paramétrico_aprobado_no_tiene_quien_decidió() -> None:
    payload = next(p for t, p in EJEMPLOS if t == SINIESTRO_APROBADO)
    sobre = _sobre(SINIESTRO_APROBADO, payload)
    sobre["payload"].update({"origen": "PARAMETRICO", "decididoPor": None})
    assert list(_validador(SINIESTRO_APROBADO).iter_errors(sobre)) == []
    # Pero el campo no puede faltar: un consumidor siempre lo encuentra.
    del sobre["payload"]["decididoPor"]
    assert list(_validador(SINIESTRO_APROBADO).iter_errors(sobre)) != []


def test_el_sobre_de_aprobado_lleva_la_referencia_del_pago_unico() -> None:
    payload = next(p for t, p in EJEMPLOS if t == SINIESTRO_APROBADO)
    assert _sobre(SINIESTRO_APROBADO, payload)["payload"]["referenciaPago"] == (
        "IND-88-2026-0442-01"
    )
