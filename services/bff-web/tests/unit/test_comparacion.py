"""HU-W12: comparar(anterior, actual) del BFF (UT-W12-18 y 19)."""

import pytest
from bff_web.application.perfilamiento import comparar

ANTERIOR = {"id": "00000000-0000-4000-a000-000000000001", "estado": "DEGRADADO"}
ACTUAL = {
    "id": "00000000-0000-4000-a000-000000000002",
    "estado": "CALCULADO",
    "perfilAnteriorId": ANTERIOR["id"],
}


def test_ut_w12_18_compone_anterior_y_actual() -> None:
    assert comparar(ANTERIOR, ACTUAL) == {"anterior": ANTERIOR, "actual": ACTUAL}


@pytest.mark.parametrize("perfil_anterior_id", [None, "00000000-0000-4000-a000-000000000009"])
def test_ut_w12_19_ids_que_no_coinciden(perfil_anterior_id: str | None) -> None:
    with pytest.raises(ValueError):
        comparar(ANTERIOR, {**ACTUAL, "perfilAnteriorId": perfil_anterior_id})
