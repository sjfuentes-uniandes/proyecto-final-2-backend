"""Lo que comparten los dos consumidores de `pagos-parametricos` (HU-W21)."""

import logging
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError
from pydantic.alias_generators import to_camel
from solventa_common.inbox import EventoInvalido

CAMPO_VERSION = "aggregateVersion"

logger = logging.getLogger("pagos_parametricos.consumo")


class Lector(BaseModel):
    """Lector tolerante: solo los campos que usa; los desconocidos se ignoran (v1 solo crece de
    forma aditiva)."""

    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, frozen=True, extra="ignore"
    )


def campos_invalidos(error: ValidationError) -> list[str]:
    """Las rutas de los campos inválidos, nunca sus valores."""
    return sorted({".".join(str(parte) for parte in e["loc"]) for e in error.errors()})


def leer_aggregate_version(sobre: Mapping[str, Any]) -> int | None:
    """El `aggregateVersion` opcional del sobre. A diferencia de `versionado.leer_version`, su
    ausencia no es un error (`polizas` no lo publica, CF-S3-11): se ordena por `occurredAt`. Si
    viene, debe ser un entero ≥ 1 (si no, `EventoInvalido`)."""
    version = sobre.get(CAMPO_VERSION)
    if version is None:
        return None
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise EventoInvalido
    return version
