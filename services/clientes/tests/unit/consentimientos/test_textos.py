"""Textos legales versionados: carga, texto vigente y rotulado de ejemplo (ADR 0005)."""

import copy
import json
from typing import Any

import pytest
from clientes.adapters.outbound.textos_json import TextosJson


def _documento(**cambios: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "proposito": "PERFILAMIENTO_PRECIO",
        "version": "v1",
        "vigente": True,
        "textos": {
            "es-CO": {"titulo": "t", "cuerpo": "c", "aviso": "a"},
            "en-US": {"titulo": "t", "cuerpo": "c", "aviso": "a"},
        },
    }
    return copy.deepcopy(base) | cambios


def test_los_archivos_del_repo_cargan_y_estan_rotulados_como_ejemplo() -> None:
    textos = TextosJson.cargar()
    es = textos.vigente("PERFILAMIENTO_PRECIO", "es-CO")
    en = textos.vigente("PERFILAMIENTO_PRECIO", "en-US")
    assert es.version == en.version
    assert "sin validez legal" in es.aviso
    assert "no legal validity" in en.aviso
    assert es.vigencias_meses == (6, 12)
    assert es.vigencia_defecto_meses == 12
    assert es.titulo and es.cuerpo


def test_los_archivos_del_repo_son_json_valido_en_utf8() -> None:
    texto = TextosJson.cargar().vigente("PERFILAMIENTO_PRECIO", "es-CO")
    assert "Autorización" in texto.titulo
    assert json.dumps(texto.cuerpo)


def test_un_texto_no_vigente_se_ignora() -> None:
    textos = TextosJson([_documento(version="v1", vigente=False), _documento(version="v2")])
    assert textos.vigente("PERFILAMIENTO_PRECIO", "es-CO").version == "v2"


def test_dos_textos_vigentes_del_mismo_proposito_fallan() -> None:
    with pytest.raises(ValueError, match="Más de un texto vigente"):
        TextosJson([_documento(version="v1"), _documento(version="v2")])


def test_falta_un_idioma_del_texto_vigente() -> None:
    documento = _documento()
    del documento["textos"]["en-US"]
    with pytest.raises(ValueError, match="Falta el texto vigente"):
        TextosJson([documento])


def test_sin_texto_vigente_falla() -> None:
    with pytest.raises(ValueError, match="Falta el texto vigente"):
        TextosJson([_documento(vigente=False)])
