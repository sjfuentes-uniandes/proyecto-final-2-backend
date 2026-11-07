"""HU-W24 (N1): regla de `aggregateVersion` (solo avanza con una versión mayor).

Con PostgreSQL real (atrasados, duplicados y concurrencia), en
`tests/module/test_hu_w24_particiones.py`.
"""

import itertools
import json
from typing import Any
from uuid import uuid4

import pytest
from psycopg import sql
from solventa_common.inbox import EventoInvalido, MensajeSqs, leer_sobre
from solventa_common.versionado import avanza, leer_version, upsert_por_version


def _evento(**campos: Any) -> Any:
    sobre = {
        "eventId": str(uuid4()),
        "aggregateId": str(uuid4()),
        "type": "PolizaCancelada",
        "version": 1,
        "occurredAt": "2026-11-05T14:06:12Z",
        "correlationId": "corr-w24-01",
        "payload": {},
    } | campos
    return leer_sobre(MensajeSqs("m-1", "rh-1", json.dumps(sobre), "PolizaCancelada", 1))


@pytest.mark.parametrize(
    ("actual", "nueva", "esperado"),
    [(None, 1, True), (1, 2, True), (2, 1, False), (2, 2, False), (5, 9, True)],
)
def test_solo_avanza_con_una_version_mayor(actual: int | None, nueva: int, esperado: bool) -> None:
    assert avanza(actual, nueva) is esperado


def _aplicar(eventos: list[tuple[int, str]]) -> str | None:
    """Una proyección en memoria con la regla: el estado de la versión mayor vista."""
    version: int | None = None
    estado: str | None = None
    for nueva, valor in eventos:
        if avanza(version, nueva):
            version, estado = nueva, valor
    return estado


def test_el_estado_final_no_depende_del_orden_ni_de_los_duplicados() -> None:
    historia = [(1, "ACTIVA"), (2, "SUSPENDIDA"), (3, "CANCELADA")]
    entregas = [*historia, historia[0], historia[1]]  # con dos duplicados

    finales = {_aplicar(list(orden)) for orden in itertools.permutations(entregas)}

    assert finales == {"CANCELADA"}


def test_una_cancelacion_atrasada_frente_a_la_emision_deja_la_cobertura_cancelada() -> None:
    assert _aplicar([(2, "CANCELADA"), (1, "ACTIVA")]) == "CANCELADA"


def test_lee_la_version_del_sobre() -> None:
    assert leer_version(_evento(aggregateVersion=3)) == 3


@pytest.mark.parametrize("valor", [None, 0, -1, "2", 2.0, True])
def test_sin_version_valida_el_evento_es_invalido(valor: Any) -> None:
    campos = {} if valor is None else {"aggregateVersion": valor}

    with pytest.raises(EventoInvalido):
        leer_version(_evento(**campos))


def test_la_sentencia_solo_reemplaza_con_una_version_mayor() -> None:
    sentencia = upsert_por_version("coberturas_activas", ["poliza_id"], ["estado", "umbral"])

    texto = sentencia.as_string(None)

    assert texto == (
        'INSERT INTO "coberturas_activas" ("poliza_id", "estado", "umbral", "aggregate_version") '
        "VALUES (%s, %s, %s, %s) "
        'ON CONFLICT ("poliza_id") DO UPDATE SET "estado" = EXCLUDED."estado", '
        '"umbral" = EXCLUDED."umbral", "aggregate_version" = EXCLUDED."aggregate_version" '
        'WHERE "coberturas_activas"."aggregate_version" < EXCLUDED."aggregate_version" '
        'RETURNING "aggregate_version"'
    )
    assert isinstance(sentencia, sql.Composed)


@pytest.mark.parametrize(
    ("clave", "columnas"),
    [([], ["estado"]), (["aggregate_version"], ["estado"]), (["id"], ["aggregate_version"])],
)
def test_la_sentencia_rechaza_una_clave_vacia_o_la_version_repetida(
    clave: list[str], columnas: list[str]
) -> None:
    with pytest.raises(ValueError):
        upsert_por_version("t", clave, columnas)
