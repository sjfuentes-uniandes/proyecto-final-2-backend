"""Soportes que exige cada amparo (DS3-05), leídos de `semillas/soportes_requeridos.json`. Pueden
migrar a `catalogo` más adelante: el puerto `SoportesRequeridos` no cambia."""

import json
from importlib import resources

from siniestros.domain.siniestro.modelos import TipoSoporte


class SoportesRequeridosJson:
    def __init__(self, requeridos: dict[str, tuple[TipoSoporte, ...]]) -> None:
        self._requeridos = requeridos

    @classmethod
    def cargar(cls) -> "SoportesRequeridosJson":
        texto = (
            resources.files("siniestros.semillas")
            .joinpath("soportes_requeridos.json")
            .read_text(encoding="utf-8")
        )
        amparos = json.loads(texto)["amparos"]
        return cls({a: tuple(TipoSoporte(t) for t in tipos) for a, tipos in amparos.items()})

    def para(self, amparo: str) -> tuple[TipoSoporte, ...]:
        return self._requeridos.get(amparo, ())
