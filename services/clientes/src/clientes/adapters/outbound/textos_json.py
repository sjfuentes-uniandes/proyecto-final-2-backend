"""Textos legales de ejemplo, versionados en archivos del repo (ADR 0005)."""

import json
from collections.abc import Iterable
from importlib import resources
from typing import Any

from clientes.domain.consentimientos.modelos import (
    IDIOMAS,
    PROPOSITOS,
    VIGENCIA_DEFECTO_MESES,
    VIGENCIAS_MESES,
)
from clientes.domain.consentimientos.textos import TextoLegal


class TextosJson:
    """Implementa CatalogoTextos. Falla al arrancar si falta el texto vigente de un propósito
    en algún idioma o si hay más de uno: un texto ambiguo no debe llegar a un otorgamiento."""

    def __init__(self, documentos: Iterable[dict[str, Any]]) -> None:
        vigentes: dict[tuple[str, str], TextoLegal] = {}
        for documento in documentos:
            if not documento.get("vigente"):
                continue
            for idioma, texto in documento["textos"].items():
                clave = (documento["proposito"], idioma)
                if clave in vigentes:
                    raise ValueError(f"Más de un texto vigente para {clave}")
                vigentes[clave] = TextoLegal(
                    proposito=documento["proposito"],
                    idioma=idioma,
                    version=documento["version"],
                    titulo=texto["titulo"],
                    cuerpo=texto["cuerpo"],
                    aviso=texto["aviso"],
                    vigencias_meses=VIGENCIAS_MESES,
                    vigencia_defecto_meses=VIGENCIA_DEFECTO_MESES,
                )
        faltantes = [(p, i) for p in sorted(PROPOSITOS) for i in IDIOMAS if (p, i) not in vigentes]
        if faltantes:
            raise ValueError(f"Falta el texto vigente de {faltantes}")
        self._vigentes = vigentes

    @classmethod
    def cargar(cls) -> "TextosJson":
        carpeta = resources.files("clientes.adapters.outbound").joinpath("textos")
        return cls(
            json.loads(archivo.read_text(encoding="utf-8"))
            for archivo in carpeta.iterdir()
            if archivo.name.endswith(".json")
        )

    def vigente(self, proposito: str, idioma: str) -> TextoLegal:
        return self._vigentes[(proposito, idioma)]
