from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from siniestros.application.siniestro.borradores import Borradores
from siniestros.main import crear_app

CLIENTE_ID = UUID("00000000-0000-4000-8000-000000000001")
POLIZA_ID = UUID("00000000-0000-4000-8000-000000000002")
BORRADOR_ID = UUID("00000000-0000-4000-8000-000000000003")
CLAVE = UUID("00000000-0000-4000-8000-000000000004")
ACTOR = "11111111-1111-4111-8111-111111111111"


@pytest.mark.parametrize(
    ("estado", "vigente", "http"),
    [
        ("ACTIVA", True, 201),
        ("ACTIVA", False, 409),
        ("EMITIDA", True, 409),
        ("CANCELADA", True, 409),
    ],
)
def test_creacion_valida_estado_y_vigencia_de_la_copia(
    estado: str, vigente: bool, http: int
) -> None:
    copia: dict[str, Any] = {
        "polizaId": str(POLIZA_ID),
        "numero": "POL-1",
        "estado": estado,
        "producto": "Producto",
        "ramo": "AUTO",
        "vigencia": {"desde": "2026-01-01", "hasta": "2026-12-31"},
        "vigente": vigente,
        "prima": "100.00",
        "sumaAsegurada": "10000.00",
        "moneda": "COP",
        "periodicidad": "MENSUAL",
        "coberturas": [{"codigo": "COLISION", "nombre": "Colision", "obligatoria": True}],
    }
    casos = AsyncMock(spec=Borradores)
    casos.crear.return_value = BORRADOR_ID
    app = crear_app()
    app.state.borradores = casos
    cliente = TestClient(app)
    respuesta = cliente.post(
        "/v1/siniestros/borradores",
        headers={
            "X-Actor-Id": ACTOR,
            "X-Actor-Rol": "CLIENTE",
            "X-Correlation-Id": "corr-borrador",
            "Idempotency-Key": str(CLAVE),
        },
        json={
            "clienteId": str(CLIENTE_ID),
            "descripcion": "Impacto en la puerta.",
            "copiaPoliza": copia,
        },
    )

    assert respuesta.status_code == http, respuesta.text
    if http == 201:
        assert respuesta.json() == {
            "borradorId": str(BORRADOR_ID),
            "polizaId": str(POLIZA_ID),
            "estado": "ABIERTO",
        }
        casos.crear.assert_awaited_once_with(
            CLIENTE_ID, POLIZA_ID, "Impacto en la puerta.", copia, CLAVE, ACTOR, "corr-borrador"
        )
    else:
        assert respuesta.json()["codigo"] == "POLIZA_NO_VIGENTE"
        casos.crear.assert_not_awaited()
