"""Schemathesis 4 contra la app con SinProteccion y el proveedor simulado (03, W10-P07)."""

import httpx
import schemathesis
from adaptador_datos.config import Settings
from adaptador_datos.main import crear_app
from fastapi.testclient import TestClient
from hypothesis import settings
from schemathesis.specs.openapi.checks import positive_data_acceptance

NOMINAL = {
    "active-products": {
        "schemaVersion": "3.1",
        "confidenceScore": 0.95,
        "data": {
            "oldestProductYears": 7,
            "totalMonthlyInstallment": {"amount": "1800000.00", "currency": "COP"},
            "creditorCount": 2,
        },
    },
    "payment-history": {"schemaVersion": "3.1", "confidenceScore": 0.91, "data": {"lateCount": 0}},
    "aggregated-income": {
        "schemaVersion": "3.1",
        "confidenceScore": 0.93,
        "data": {"estimatedMonthlyIncome": {"amount": "6500000.00", "currency": "COP"}},
    },
}
REGISTRO = {
    "version": "1.8",
    "nivelConfianza": 0.88,
    "registro": {"listasRestrictivas": {"coincidencias": 0}},
}


def _nominal(request: httpx.Request) -> httpx.Response:
    recurso = request.url.path.rsplit("/", 1)[-1]
    return httpx.Response(200, json=NOMINAL.get(recurso, REGISTRO))


app = crear_app(Settings(), transport=httpx.MockTransport(_nominal))
# Arranca el lifespan una vez: crea los clientes y el caso de uso en app.state.
_ciclo = TestClient(app)
_ciclo.__enter__()

schema = schemathesis.openapi.from_asgi("/openapi.json", app)


# positive_data_acceptance se excluye: CAMPOS_NO_SOPORTADOS (campo de otra fuente) y
# CONSENTIMIENTO_REQUERIDO (consentimientoId nulo) son reglas entre campos que el esquema no
# puede expresar; esos 422 están documentados en el contrato y los cubre test_api.py.
@schema.parametrize()
@settings(max_examples=60, deadline=None)
def test_api_cumple_su_contrato(case: schemathesis.Case) -> None:
    case.call_and_validate(excluded_checks=[positive_data_acceptance])
