"""El generador de los esquemas publicados de `ConsentimientoOtorgado` y
`ConsentimientoRevocado` v1."""

import json
import runpy
from pathlib import Path

import pytest
from clientes.adapters.outbound.eventos import (
    ESQUEMAS,
    SobreConsentimientoOtorgado,
    SobreConsentimientoRevocado,
)

EVENTOS = Path(__file__).parents[5] / "contracts" / "events"
OTORGADO = "ConsentimientoOtorgado.v1.schema.json"
REVOCADO = "ConsentimientoRevocado.v1.schema.json"


def test_el_generador_escribe_el_esquema_publicado(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "contracts" / "events").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)

    with pytest.warns(RuntimeWarning):  # runpy reejecuta un módulo ya importado
        runpy.run_module("clientes.adapters.outbound.eventos", run_name="__main__")

    salida = capsys.readouterr().out
    for archivo in (OTORGADO, REVOCADO):
        generado = (tmp_path / "contracts" / "events" / archivo).read_text(encoding="utf-8")
        assert generado == (EVENTOS / archivo).read_text(encoding="utf-8")
        assert archivo in salida


def test_los_esquemas_son_los_sobres_de_otorgado_y_revocado() -> None:
    assert ESQUEMAS == {
        OTORGADO: SobreConsentimientoOtorgado,
        REVOCADO: SobreConsentimientoRevocado,
    }
    for archivo, modelo in ESQUEMAS.items():
        publicado = json.loads((EVENTOS / archivo).read_text(encoding="utf-8"))
        assert publicado["title"] == modelo.__name__
