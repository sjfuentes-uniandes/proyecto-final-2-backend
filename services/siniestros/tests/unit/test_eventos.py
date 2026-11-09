"""Sobres de los eventos de siniestros y su generador de esquemas (HU-W19)."""

import json
import runpy
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError
from siniestros.adapters.outbound.eventos import (
    ESQUEMAS,
    SINIESTRO_CONSULTADO,
    SiniestroConsultadoV1,
    SobreEvento,
)


def test_el_sobre_es_versionado_e_inmutable(ahora: datetime) -> None:
    siniestro_id = uuid4()
    sobre = SobreEvento[SiniestroConsultadoV1](
        event_id=uuid4(),
        aggregate_id=siniestro_id,
        type=SINIESTRO_CONSULTADO,
        version=1,
        occurred_at=ahora,
        correlation_id="corr-w19",
        payload=SiniestroConsultadoV1(
            siniestro_id=siniestro_id, codigo="88-2026-0442", actor_id="a0a0"
        ),
    )
    assert sobre.model_dump(mode="json", by_alias=True)["payload"] == {
        "siniestroId": str(siniestro_id),
        "codigo": "88-2026-0442",
        "actorId": "a0a0",
    }
    with pytest.raises(ValidationError):
        sobre.version = 2  # type: ignore[misc]


def test_solo_existe_la_version_1(ahora: datetime) -> None:
    with pytest.raises(ValidationError):
        SobreEvento[SiniestroConsultadoV1](
            event_id=uuid4(),
            aggregate_id=uuid4(),
            type=SINIESTRO_CONSULTADO,
            version=2,  # type: ignore[arg-type]
            occurred_at=ahora,
            correlation_id="c",
            payload=SiniestroConsultadoV1(
                siniestro_id=uuid4(), codigo="88-2026-0442", actor_id="a"
            ),
        )


def test_el_generador_escribe_los_siete_esquemas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "contracts" / "events").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    runpy.run_module("siniestros.adapters.outbound.eventos", run_name="__main__")
    escritos = sorted(p.name for p in (tmp_path / "contracts" / "events").iterdir())
    assert escritos == sorted(ESQUEMAS)
    for nombre, modelo in ESQUEMAS.items():
        generado = json.loads((tmp_path / "contracts" / "events" / nombre).read_text("utf-8"))
        assert generado == modelo.model_json_schema(by_alias=True)
    assert capsys.readouterr().out.count("schema.json") == 7
