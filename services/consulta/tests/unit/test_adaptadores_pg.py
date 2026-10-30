from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from consulta.adapters.outbound.polizas_pg import COLUMNAS, ProyeccionPolizasPg, fila_de
from consulta.adapters.outbound.reloj import RelojSistema
from consulta.config import Settings
from consulta.domain.polizas.modelos import CoberturaProyectada, PolizaProyectada, Vigencia
from consulta.main import crear_app
from fastapi.testclient import TestClient

pytestmark = pytest.mark.anyio


def _poliza() -> PolizaProyectada:
    return PolizaProyectada(
        poliza_id=uuid4(),
        cliente_id=uuid4(),
        numero="100-2026-000001",
        estado="ACTIVA",
        producto_id="vida-deudor",
        producto_version="1.0.0",
        producto_nombre="Vida deudor",
        ramo="VIDA",
        vigencia=Vigencia(date(2026, 10, 25), date(2027, 10, 24)),
        prima=Decimal("64638.10"),
        suma_asegurada=Decimal(50000000),
        moneda="COP",
        periodicidad="ANUAL",
        coberturas=(CoberturaProyectada("MUERTE", "Muerte", True),),
        documento_huella="h",
        firma_id="f",
        emitida_en=datetime(2026, 10, 24, tzinfo=UTC),
        proyectada_en=datetime(2026, 10, 25, tzinfo=UTC),
    )


def test_fila_de_sigue_el_orden_de_las_columnas() -> None:
    fila = fila_de(_poliza())
    assert len(fila) == len(COLUMNAS)
    assert fila[10] == Decimal("64638.10")


class _Cursor:
    def __init__(self, fila: object) -> None:
        self._fila = fila

    async def fetchone(self) -> object:
        return self._fila


class _Conexion:
    def __init__(self, fila: object) -> None:
        self._fila = fila

    async def execute(self, *_: Any) -> _Cursor:
        return _Cursor(self._fila)


class _Tx:
    def __init__(self, fila: object) -> None:
        self.conexion = _Conexion(fila)


async def test_insertar_informa_si_inserto() -> None:
    adaptador = ProyeccionPolizasPg()
    assert await adaptador.insertar(_poliza(), _Tx(("x",))) is True  # type: ignore[arg-type]
    assert await adaptador.insertar(_poliza(), _Tx(None)) is False  # type: ignore[arg-type]


def test_reloj_sistema_devuelve_hora_utc() -> None:
    assert RelojSistema().ahora().tzinfo is not None


def test_sin_cola_la_app_arranca() -> None:
    app = crear_app(Settings(), catalogo=object())  # type: ignore[arg-type]
    with TestClient(app) as cliente:
        assert cliente.get("/health").status_code in (200, 503)
