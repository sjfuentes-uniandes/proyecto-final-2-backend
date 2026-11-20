"""Eventos `IndemnizacionConfirmada` e `IndemnizacionRechazada` v1 y su escritura en el outbox
(HU-W23; ADR 0015).

El sobre es el de los eventos existentes; el relay lo arma desde las columnas. Sin la cuenta, sin el
`beneficiarioRef` ni cuerpos de la pasarela; el monto, texto decimal exacto.
"""

import json
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from dinero.adapters.outbound.outbox_pg import OutboxIndemnizacionesPg, evento_de_indemnizacion
from psycopg.types.json import Jsonb

pytestmark = pytest.mark.anyio
PROHIBIDOS = {"beneficiarioRef", "beneficiario", "cuenta", "tarjeta", "pagador", "clienteId"}


def test_confirmada(d: Any) -> None:
    i = d.nueva(monto=Decimal("2850000.50")).confirmar("TRX-1", d.DESPUES).registrar_duplicado()
    sobre = evento_de_indemnizacion(i).model_dump(mode="json", by_alias=True)
    assert UUID(sobre["eventId"]) != i.indemnizacion_id
    assert {k: v for k, v in sobre.items() if k not in ("eventId", "payload")} == {
        "aggregateId": str(i.indemnizacion_id),
        "type": "IndemnizacionConfirmada",
        "version": 1,
        "occurredAt": "2026-11-10T15:00:02Z",
        "correlationId": "corr-w23",
    }
    assert sobre["payload"] == {
        "indemnizacionId": str(i.indemnizacion_id),
        "siniestroId": str(i.siniestro_id),
        "referencia": "IND-88-2026-0442-01",
        "monto": "2850000.50",
        "moneda": "COP",
        "transaccionId": "TRX-1",
        "intentos": 1,
        "duplicadosDescartados": 1,
        "confirmadaEn": "2026-11-10T15:00:02Z",
    }


def test_rechazada(d: Any) -> None:
    i = d.nueva().rechazar("BENEFICIARIO_NO_HABILITADO", d.DESPUES, transaccion_id="TRX-R")
    sobre = evento_de_indemnizacion(i).model_dump(mode="json", by_alias=True)
    assert (sobre["type"], sobre["aggregateId"], sobre["occurredAt"]) == (
        "IndemnizacionRechazada",
        str(i.indemnizacion_id),
        "2026-11-10T15:00:02Z",
    )
    assert sobre["payload"] == {
        "indemnizacionId": str(i.indemnizacion_id),
        "siniestroId": str(i.siniestro_id),
        "referencia": "IND-88-2026-0442-01",
        "motivo": "BENEFICIARIO_NO_HABILITADO",
        "intentos": 1,
        "duplicadosDescartados": 0,
        "rechazadaEn": "2026-11-10T15:00:02Z",
    }


def test_el_monto_va_con_dos_decimales_aunque_el_dominio_traiga_menos(d: Any) -> None:
    i = d.nueva(monto=Decimal(2500000)).confirmar("T", d.DESPUES)
    assert evento_de_indemnizacion(i).payload.monto == "2500000.00"


def test_ningun_evento_lleva_datos_de_cuenta(d: Any) -> None:
    for i in (
        d.nueva(beneficiario_ref="ben-sint-centinela").confirmar("T", d.DESPUES),
        d.nueva(beneficiario_ref="ben-sint-centinela").rechazar("MOTIVO", d.DESPUES),
    ):
        sobre = evento_de_indemnizacion(i).model_dump(mode="json", by_alias=True)
        assert not PROHIBIDOS & set(sobre["payload"])
        assert "ben-sint-centinela" not in json.dumps(sobre)


def test_una_pendiente_no_tiene_evento(d: Any) -> None:
    with pytest.raises(ValueError, match="PENDIENTE"):
        evento_de_indemnizacion(d.nueva())


class Conexion:
    def __init__(self) -> None:
        self.sentencias: list[tuple[str, Any]] = []

    async def execute(self, sql: str, parametros: Any = None) -> None:
        self.sentencias.append((sql, parametros))


async def test_el_outbox_inserta_en_la_transaccion_recibida(d: Any) -> None:
    i = d.nueva().confirmar("TRX-1", d.DESPUES)
    conexion = Conexion()
    await OutboxIndemnizacionesPg().agregar(i, SimpleNamespace(conexion=conexion))  # type: ignore[arg-type]
    [(sql, parametros)] = conexion.sentencias
    assert sql.startswith("INSERT INTO outbox")
    event_id, aggregate_id, tipo, version, ocurrido, correlacion, payload = parametros
    assert (aggregate_id, tipo, version, ocurrido, correlacion) == (
        i.indemnizacion_id,
        "IndemnizacionConfirmada",
        1,
        d.DESPUES,
        "corr-w23",
    )
    assert isinstance(event_id, UUID) and isinstance(payload, Jsonb)
    assert payload.obj["referencia"] == "IND-88-2026-0442-01"
