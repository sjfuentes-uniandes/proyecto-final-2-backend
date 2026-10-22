"""`vigenteHasta` en la respuesta de la cotización (HU-W13, DS2-20): aditivo y opcional, el mismo
valor al calcular y al leer, y según el parámetro de vigencia."""

from collections.abc import Callable, Iterator
from typing import Any
from uuid import uuid4

import pytest
from cotizacion.config import Settings
from cotizacion.main import Puertos, crear_app
from fastapi.testclient import TestClient

ASESOR = {"X-Actor-Id": "asesor-sintetico", "X-Actor-Rol": "ASESOR"}
C01 = {
    "clienteId": "00000000-0000-4000-8000-000000000001",
    "productoId": "vida-hipotecario",
    "datosAsegurado": {
        "fechaNacimiento": "1990-04-18",
        "plazoCreditoMeses": 180,
        "sumaAsegurada": 180000000,
    },
}


@pytest.fixture
def api(escenario: Any, escenario_cotizacion: Any) -> Iterator[Callable[..., TestClient]]:
    abiertos: list[TestClient] = []

    def crear(**ajustes: Any) -> TestClient:
        puertos = Puertos(
            identidad=escenario_cotizacion.identidad,
            consentimiento=escenario.consentimiento,
            modelos=escenario.modelos,
            ejecutor=escenario.ejecutor,
            perfiles=escenario_cotizacion.perfiles,
            reloj=escenario_cotizacion.reloj,
            productos=escenario_cotizacion.productos,
            cotizaciones=escenario_cotizacion.repo,
        )
        cliente = TestClient(crear_app(Settings(**ajustes), puertos=puertos))
        abiertos.append(cliente.__enter__())
        return cliente

    yield crear
    for cliente in abiertos:
        cliente.__exit__(None, None, None)


def _cotizar(api: TestClient) -> dict[str, Any]:
    respuesta = api.post(
        "/v1/cotizaciones", json=C01, headers={**ASESOR, "Idempotency-Key": str(uuid4())}
    )
    assert respuesta.status_code == 201
    creada: dict[str, Any] = respuesta.json()
    return creada


def test_la_cotizacion_trae_vigente_hasta(api: Callable[..., TestClient]) -> None:
    cliente = api()
    creada = _cotizar(cliente)
    assert creada["calculadoEn"] == "2026-10-15T15:00:00Z"
    assert creada["vigenteHasta"] == "2026-10-19T15:00:00Z"
    leida = cliente.get(f"/v1/cotizaciones/{creada['id']}", headers=ASESOR).json()
    assert leida["vigenteHasta"] == creada["vigenteHasta"]


def test_con_otro_parametro_de_vigencia(api: Callable[..., TestClient]) -> None:
    creada = _cotizar(api(oferta_vigencia_horas=24))
    assert creada["vigenteHasta"] == "2026-10-16T15:00:00Z"
