"""ADR 0013, decisión 6: ningún dato de tarjeta ni de cuenta en los mapeos de pagos.

Se corre con: uv run --with pytest pytest services/simulador-aliados/tests
"""

import json
import re
from pathlib import Path
from typing import Any

PAGOS = Path(__file__).resolve().parents[1] / "mappings" / "pagos"
PAGADORES = {
    "00000000-0000-4000-8000-000000000001",
    "00000000-0000-4000-8000-000000000002",
    "00000000-0000-4000-8000-000000000005",
    "00000000-0000-4000-8000-000000000006",
    "00000000-0000-4000-8000-000000000007",
}
CLAVES_PROHIBIDAS = re.compile(
    r"tarjeta|card|pan|cvv|cvc|expir|vencimiento|cuenta|account|iban|banco|titular", re.IGNORECASE
)
# 13 a 19 dígitos seguidos (con espacios o guiones opcionales): forma de un número de tarjeta.
# Los uuid (pagadores sintéticos) se quitan antes de buscar: sus dígitos y guiones coincidirían.
NUMERO_DE_TARJETA = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
UUID = re.compile(r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b", re.IGNORECASE)


def _claves(valor: Any) -> list[str]:
    if isinstance(valor, dict):
        return [c for k, v in valor.items() for c in (k, *_claves(v))]
    if isinstance(valor, list):
        return [c for v in valor for c in _claves(v)]
    return []


def test_hay_mapeos_por_pagador() -> None:
    texto = "".join(ruta.read_text(encoding="utf-8") for ruta in PAGOS.glob("*.json"))
    assert all(pagador in texto for pagador in PAGADORES)


def test_ningun_mapeo_de_pagos_tiene_claves_de_tarjeta_ni_de_cuenta() -> None:
    for ruta in sorted(PAGOS.glob("*.json")):
        claves = _claves(json.loads(ruta.read_text(encoding="utf-8")))
        assert not [c for c in claves if CLAVES_PROHIBIDAS.search(c)], ruta.name


def test_ningun_mapeo_de_pagos_tiene_numeros_con_forma_de_tarjeta() -> None:
    for ruta in sorted(PAGOS.glob("*.json")):
        texto = UUID.sub("", ruta.read_text(encoding="utf-8"))
        assert not NUMERO_DE_TARJETA.search(texto), ruta.name
