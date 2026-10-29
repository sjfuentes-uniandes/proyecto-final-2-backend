"""Esquemas de `RecaudoConfirmado` y `RecaudoRechazado` v1 (HU-W16, 16.10): los publicados son los
que genera el código, y cada evento que escribe dinero cumple el suyo, sin datos del pagador.

Si cambia un evento, regenerar con: uv run python -m dinero.adapters.outbound.eventos
"""

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from dinero.adapters.outbound.eventos import ESQUEMAS
from dinero.adapters.outbound.outbox_pg import evento_de
from dinero.domain.recaudo.modelos import Recaudo
from jsonschema import Draft202012Validator
from pydantic import BaseModel

EVENTOS = Path(__file__).parents[4] / "contracts" / "events"
AHORA = datetime(2026, 10, 28, 15, 0, tzinfo=UTC)
PAGADOR = uuid4()


def _pendiente() -> Recaudo:
    return Recaudo.pendiente(
        clave_recaudo=uuid4(),
        cotizacion_id=uuid4(),
        pagador_ref=PAGADOR,
        monto=Decimal("64638.07"),
        moneda="COP",
        periodicidad="MENSUAL",
        correlation_id="corr-contrato",
        ahora=AHORA,
    )


def _validador(tipo: str) -> Draft202012Validator:
    esquema = json.loads((EVENTOS / f"{tipo}.v1.schema.json").read_text(encoding="utf-8"))
    return Draft202012Validator(esquema, format_checker=Draft202012Validator.FORMAT_CHECKER)


def test_los_dos_eventos_se_publican() -> None:
    assert sorted(ESQUEMAS) == [
        "RecaudoConfirmado.v1.schema.json",
        "RecaudoRechazado.v1.schema.json",
    ]


@pytest.mark.parametrize(("archivo", "modelo"), ESQUEMAS.items())
def test_esquema_publicado_coincide(archivo: str, modelo: type[BaseModel]) -> None:
    publicado = json.loads((EVENTOS / archivo).read_text(encoding="utf-8"))
    assert publicado == modelo.model_json_schema(by_alias=True)


@pytest.mark.parametrize(
    ("tipo", "resolver"),
    [
        ("RecaudoConfirmado", lambda r: r.confirmar("TX-SINT-C01", AHORA)),
        ("RecaudoRechazado", lambda r: r.rechazar("FONDOS_INSUFICIENTES", AHORA)),
        ("RecaudoRechazado", lambda r: r.rechazar("MOTIVO_NO_INFORMADO", AHORA, "TX-R")),
    ],
)
def test_cada_evento_cumple_su_esquema_sin_datos_del_pagador(tipo: str, resolver: object) -> None:
    recaudo = resolver(_pendiente())  # type: ignore[operator]
    sobre = evento_de(recaudo).model_dump(mode="json", by_alias=True)
    assert list(_validador(tipo).iter_errors(sobre)) == []
    assert sobre["type"] == tipo
    assert str(PAGADOR) not in json.dumps(sobre)
    assert not {"pagadorRef", "pagador", "clienteId"} & set(sobre["payload"])


@pytest.mark.parametrize(
    ("tipo", "cambio"),
    [
        ("RecaudoConfirmado", {"monto": {"valor": 64638.07, "moneda": "COP"}}),
        ("RecaudoConfirmado", {"monto": {"valor": "64638.001", "moneda": "COP"}}),
        ("RecaudoConfirmado", {"transaccionId": ""}),
        ("RecaudoRechazado", {"motivo": "texto libre"}),
    ],
)
def test_el_esquema_rechaza_montos_numericos_y_textos_libres(
    tipo: str, cambio: dict[str, object]
) -> None:
    recaudo = _pendiente()
    resuelto = (
        recaudo.confirmar("TX", AHORA)
        if tipo == "RecaudoConfirmado"
        else recaudo.rechazar("FONDOS_INSUFICIENTES", AHORA)
    )
    sobre = evento_de(resuelto).model_dump(mode="json", by_alias=True)
    sobre["payload"].update(cambio)
    assert list(_validador(tipo).iter_errors(sobre)) != []
