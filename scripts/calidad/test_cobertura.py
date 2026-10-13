import importlib.util
import json
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "cobertura", Path(__file__).with_name("cobertura.py")
)
assert _spec is not None and _spec.loader is not None
cobertura = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cobertura)


def _reporte(lineas: float, ramas_cubiertas: int, ramas: int) -> dict:
    return {
        "totals": {
            "percent_covered": lineas,
            "covered_branches": ramas_cubiertas,
            "num_branches": ramas,
        }
    }


def _correr(tmp_path: Path, servicio: str, reporte: dict) -> int:
    ruta = tmp_path / "cobertura.json"
    ruta.write_text(json.dumps(reporte), encoding="utf-8")
    return cobertura.main([servicio, str(ruta)])


def test_exito_cuando_cumple_lineas_y_ramas(tmp_path: Path) -> None:
    assert _correr(tmp_path, "cotizacion", _reporte(92.5, 90, 100)) == 0


def test_falla_por_lineas_insuficientes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _correr(tmp_path, "adaptador-datos", _reporte(89.99, 0, 0)) == 1
    assert "líneas" in capsys.readouterr().err


def test_falla_por_ramas_insuficientes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _correr(tmp_path, "catalogo", _reporte(95, 84, 100)) == 1
    assert "ramas" in capsys.readouterr().err


def test_servicio_sin_umbral_siempre_pasa(tmp_path: Path) -> None:
    assert _correr(tmp_path, "api-socios", _reporte(0, 0, 10)) == 0


def test_sin_ramas_cuenta_como_cien() -> None:
    assert cobertura.porcentajes(_reporte(91, 0, 0)) == (91.0, 100.0)


def test_umbral_de_bff_web_solo_lineas() -> None:
    assert cobertura.evaluar("bff-web", _reporte(85, 0, 10)) == []
    assert cobertura.evaluar("bff-web", _reporte(84.9, 10, 10)) != []


def test_argumentos_incorrectos() -> None:
    assert cobertura.main([]) == 2
