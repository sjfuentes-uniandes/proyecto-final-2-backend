from typing import Any

import pytest
from catalogo.adapters.inbound.http.esquema_modelo import DefinicionModelo, a_dict
from pydantic import ValidationError


def test_a_dict_conserva_las_claves_del_contrato(definicion_candidata: dict[str, Any]) -> None:
    definicion = a_dict(DefinicionModelo.model_validate(definicion_candidata))
    assert definicion == definicion_candidata
    assert "productoId" in definicion
    assert set(definicion["confianza"]) == {"penalizacionNoAutorizada", "penalizacionNoObtenida"}


@pytest.mark.parametrize(
    "alterar",
    [
        lambda d: d.update(extra=1),
        lambda d: d.update(producto_id=d.pop("productoId")),
        lambda d: d["confianza"].update(penalizacion_no_obtenida="0.15"),
        lambda d: d["fuentes"]["FA_HISTORIAL_PAGOS_12M"].update(proveedor="x"),
        lambda d: d["factores"][0].update(nota="x"),
        lambda d: d["derivados"]["edad_fin_credito"]["args"][1].update(extra="x"),
    ],
    ids=["raiz", "snake-case", "confianza", "fuente", "factor", "expresion-anidada"],
)
def test_un_campo_extra_o_sin_alias_se_rechaza(
    definicion_candidata: dict[str, Any], alterar: Any
) -> None:
    alterar(definicion_candidata)
    with pytest.raises(ValidationError):
        DefinicionModelo.model_validate(definicion_candidata)


@pytest.mark.parametrize(
    "alterar",
    [
        lambda d: d["factores"][0].update(peso=0.3),
        lambda d: d["factores"][0].update(peso="0,30"),
        lambda d: d["factores"][0]["reglas"][0].update(direccion="FAVORABLE"),
        lambda d: d["factores"][0]["reglas"][0]["si"][0].__setitem__(1, "=~"),
        lambda d: d["factores"][0]["reglas"][0]["si"][0].__setitem__(2, "cero"),
        lambda d: d["factores"][0]["reglas"][0]["si"][0].append("1"),
        lambda d: d["niveles"][0].update(nivel="MUY_BAJO"),
        lambda d: d["fuentes"]["DA_REGISTROS_PUBLICOS"].update(tipo="DECLARADO"),
        lambda d: d["ajustes"][0].update(efecto="SUMAR"),
        lambda d: d["derivados"]["edad_fin_credito"].update(op="pow"),
    ],
    ids=[
        "peso-numero",
        "peso-coma",
        "direccion",
        "operador",
        "valor",
        "condicion-larga",
        "nivel",
        "tipo-fuente",
        "efecto",
        "op",
    ],
)
def test_valores_fuera_del_esquema_se_rechazan(
    definicion_candidata: dict[str, Any], alterar: Any
) -> None:
    alterar(definicion_candidata)
    with pytest.raises(ValidationError):
        DefinicionModelo.model_validate(definicion_candidata)
