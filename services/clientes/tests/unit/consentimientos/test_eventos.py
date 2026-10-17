"""El generador del esquema publicado de `ConsentimientoOtorgado` v1."""

import json
import runpy
from pathlib import Path

import pytest
from clientes.adapters.outbound.eventos import ESQUEMAS, SobreConsentimientoOtorgado

ARCHIVO = "ConsentimientoOtorgado.v1.schema.json"
PUBLICADO = Path(__file__).parents[5] / "contracts" / "events" / ARCHIVO


def test_el_generador_escribe_el_esquema_publicado(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "contracts" / "events").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)

    with pytest.warns(RuntimeWarning):  # runpy reejecuta un módulo ya importado
        runpy.run_module("clientes.adapters.outbound.eventos", run_name="__main__")

    generado = (tmp_path / "contracts" / "events" / ARCHIVO).read_text(encoding="utf-8")
    assert generado == PUBLICADO.read_text(encoding="utf-8")
    assert ARCHIVO in capsys.readouterr().out


def test_el_unico_esquema_es_el_del_sobre_de_otorgado() -> None:
    assert ESQUEMAS == {ARCHIVO: SobreConsentimientoOtorgado}
    assert json.loads(PUBLICADO.read_text(encoding="utf-8"))["title"] == (
        "SobreConsentimientoOtorgado"
    )
