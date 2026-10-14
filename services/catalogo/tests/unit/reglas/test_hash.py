import hashlib
import json
from datetime import UTC, datetime
from typing import Any

import pytest
from catalogo.domain.reglas.hash import hash_canonico
from catalogo.domain.reglas.modelo import ModeloPublicado


def _invertir_claves(valor: Any) -> Any:
    if isinstance(valor, dict):
        return {k: _invertir_claves(valor[k]) for k in reversed(list(valor))}
    if isinstance(valor, list):
        return [_invertir_claves(v) for v in valor]
    return valor


def test_formato_sha256_del_json_canonico(definicion_candidata: dict[str, Any]) -> None:
    canonico = json.dumps(
        definicion_candidata, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    esperado = "sha256:" + hashlib.sha256(canonico.encode("utf-8")).hexdigest()
    assert hash_canonico(definicion_candidata) == esperado


def test_estable_aunque_cambie_el_orden_de_las_claves(
    definicion_candidata: dict[str, Any],
) -> None:
    invertida = _invertir_claves(definicion_candidata)
    assert json.dumps(invertida) != json.dumps(definicion_candidata)
    assert hash_canonico(invertida) == hash_canonico(definicion_candidata)


def test_distinto_si_cambia_un_umbral(definicion_candidata: dict[str, Any]) -> None:
    original = hash_canonico(definicion_candidata)
    definicion_candidata["niveles"][0]["desde"] = "80"
    assert hash_canonico(definicion_candidata) != original


def test_el_modelo_publicado_es_inmutable(definicion_candidata: dict[str, Any]) -> None:
    modelo = ModeloPublicado(
        version="2026.10.1",
        producto_id="vida-hipotecario",
        vigente_desde=datetime(2026, 10, 1, tzinfo=UTC),
        definicion=definicion_candidata,
        hash=hash_canonico(definicion_candidata),
        publicado_por="migracion",
    )
    with pytest.raises(AttributeError):
        modelo.version = "2026.10.2"  # type: ignore[misc]
