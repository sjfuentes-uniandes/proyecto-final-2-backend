"""Eventos `RecaudoConfirmado` y `RecaudoRechazado` v1 y su escritura en el outbox (HU-W16, 16.10).

El sobre es el de los eventos existentes; el relay lo arma desde las columnas. Sin `pagadorRef` ni
ningún dato del pagador; el monto, texto decimal exacto.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from dinero.adapters.outbound.outbox_pg import OutboxRecaudosPg, evento_de
from dinero.domain.recaudo.modelos import Recaudo
from psycopg.types.json import Jsonb

pytestmark = pytest.mark.anyio
DESPUES = datetime(2026, 10, 28, 15, 0, 3, tzinfo=UTC)
Fabrica = Callable[..., Recaudo]
PROHIBIDOS = {"pagadorRef", "pagador", "clienteId", "cuenta", "tarjeta", "medioPago"}


def test_confirmado(pendiente: Fabrica) -> None:
    recaudo = pendiente(monto=Decimal("64638.07")).confirmar("TX-SINT-C01", DESPUES)
    sobre = evento_de(recaudo).model_dump(mode="json", by_alias=True)
    assert UUID(sobre["eventId"]) != recaudo.recaudo_id
    assert {k: v for k, v in sobre.items() if k not in ("eventId", "payload")} == {
        "aggregateId": str(recaudo.recaudo_id),
        "type": "RecaudoConfirmado",
        "version": 1,
        "occurredAt": "2026-10-28T15:00:03Z",
        "correlationId": "corr-w16",
    }
    assert sobre["payload"] == {
        "recaudoId": str(recaudo.recaudo_id),
        "claveRecaudo": str(recaudo.clave_recaudo),
        "cotizacionId": str(recaudo.cotizacion_id),
        "monto": {"valor": "64638.07", "moneda": "COP"},
        "transaccionId": "TX-SINT-C01",
        "confirmadoEn": "2026-10-28T15:00:03Z",
    }


def test_rechazado(pendiente: Fabrica) -> None:
    recaudo = pendiente().rechazar("FONDOS_INSUFICIENTES", DESPUES, transaccion_id="TX-R")
    sobre = evento_de(recaudo).model_dump(mode="json", by_alias=True)
    assert (sobre["type"], sobre["aggregateId"], sobre["occurredAt"]) == (
        "RecaudoRechazado",
        str(recaudo.recaudo_id),
        "2026-10-28T15:00:03Z",
    )
    assert sobre["payload"] == {
        "recaudoId": str(recaudo.recaudo_id),
        "claveRecaudo": str(recaudo.clave_recaudo),
        "cotizacionId": str(recaudo.cotizacion_id),
        "motivo": "FONDOS_INSUFICIENTES",
        "rechazadoEn": "2026-10-28T15:00:03Z",
    }


@pytest.mark.parametrize(
    ("monto", "texto"),
    [
        (Decimal(64638), "64638"),
        (Decimal("64638.00"), "64638.00"),
        (Decimal("64638.1"), "64638.1"),
        (Decimal("0.01"), "0.01"),
        (Decimal("999999999999.99"), "999999999999.99"),
        (Decimal("1E+3"), "1000"),
    ],
)
def test_el_monto_del_evento_es_el_decimal_exacto(
    pendiente: Fabrica, monto: Decimal, texto: str
) -> None:
    recaudo = pendiente(monto=monto).confirmar("TX", DESPUES)
    payload = evento_de(recaudo).payload.model_dump(mode="json", by_alias=True)
    assert payload["monto"]["valor"] == texto
    assert Decimal(payload["monto"]["valor"]) == monto


def test_ningun_evento_lleva_datos_del_pagador(pendiente: Fabrica) -> None:
    for recaudo in (
        pendiente().confirmar("TX", DESPUES),
        pendiente().rechazar("FONDOS_INSUFICIENTES", DESPUES),
    ):
        sobre = evento_de(recaudo).model_dump(mode="json", by_alias=True)
        assert not PROHIBIDOS & set(sobre["payload"])
        assert str(recaudo.pagador_ref) not in str(sobre)


def test_un_pendiente_no_tiene_evento(pendiente: Fabrica) -> None:
    with pytest.raises(ValueError, match="PENDIENTE"):
        evento_de(pendiente().registrar_incierto("TIEMPO_AGOTADO", DESPUES))


class Conexion:
    def __init__(self) -> None:
        self.sentencias: list[tuple[str, Any]] = []

    async def execute(self, sql: str, parametros: Any = None) -> None:
        self.sentencias.append((sql, parametros))


class Tx:
    def __init__(self) -> None:
        self.conexion = Conexion()


async def test_agregar_inserta_el_sobre_en_la_transaccion_dada(pendiente: Fabrica) -> None:
    recaudo = pendiente(monto=Decimal("64638.07")).confirmar("TX-SINT-C01", DESPUES)
    tx = Tx()
    await OutboxRecaudosPg().agregar(recaudo, tx)  # type: ignore[arg-type]
    [(sql, parametros)] = tx.conexion.sentencias
    assert sql.startswith("INSERT INTO outbox (event_id, aggregate_id, event_type, event_version")
    event_id, aggregate_id, tipo, version, ocurrido, correlacion, payload = parametros
    assert isinstance(event_id, UUID)
    assert (aggregate_id, tipo, version, ocurrido, correlacion) == (
        recaudo.recaudo_id,
        "RecaudoConfirmado",
        1,
        DESPUES,
        "corr-w16",
    )
    assert isinstance(payload, Jsonb)
    assert payload.obj["monto"] == {"valor": "64638.07", "moneda": "COP"}
    # published_at, attempts y last_error los deja a sus valores por defecto: solo el relay marca.
    assert "published_at" not in sql
