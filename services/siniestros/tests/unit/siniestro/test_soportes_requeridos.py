"""La tabla de soportes por amparo (DS3-05) que se publica con el servicio."""

import json
from importlib import resources
from pathlib import Path

from siniestros.domain.siniestro.modelos import TipoSoporte


def test_cada_amparo_exige_soportes_del_catalogo_cerrado() -> None:
    texto = resources.files("siniestros.semillas").joinpath("soportes_requeridos.json")
    amparos = json.loads(texto.read_text(encoding="utf-8"))["amparos"]
    assert set(amparos) == {"DANO_ACCIDENTAL", "ROBO", "PERDIDA_EQUIPAJE", "INCAPACIDAD_TOTAL"}
    for requeridos in amparos.values():
        assert requeridos, "un amparo sin soportes requeridos no es evaluable"
        assert len(set(requeridos)) == len(requeridos)
        assert {TipoSoporte(tipo) for tipo in requeridos}


def test_la_tabla_publicada_coincide_con_los_datos_de_r7() -> None:
    publicada = json.loads(
        resources.files("siniestros.semillas")
        .joinpath("soportes_requeridos.json")
        .read_text(encoding="utf-8")
    )["amparos"]
    r7 = Path(__file__).parents[5] / "contracts" / "datos-sinteticos" / "siniestros-r7.json"
    assert publicada == json.loads(r7.read_text(encoding="utf-8"))["soportesRequeridosPorAmparo"]
