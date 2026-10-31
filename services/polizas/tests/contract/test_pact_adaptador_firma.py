"""Pacto consumidor `polizas` → proveedor `adaptador-firma` para la firma de la huella del documento
de la póliza (HU-W17, 17.8).

Lo genera `make test-contract SERVICE=polizas` en `contracts/pact/polizas-adaptador-firma.json`. La
interacción ejercita el adaptador real (`AdaptadorFirmaHttp.firmar`) contra el mock de Pact: el
pacto describe exactamente lo que polizas envía y lo que sabe leer. `adaptador-firma` lo verifica
como proveedor (`make test-contract SERVICE=adaptador-firma`).

Solo lo que el proveedor promete: `200 FIRMADO` con el mismo `documentoId` y la misma huella. Los
timeouts, los 5xx y los cuerpos fuera de contrato van en `tests/unit/poliza/test_firma_http.py` y
en las de módulo.
"""

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from pact import Pact, match
from polizas.adapters.outbound.firma_http import AdaptadorFirmaHttp
from polizas.application.poliza.puertos import FirmaObtenida, SolicitudFirma
from solventa_common import correlation

PACTOS = Path(__file__).parents[4] / "contracts" / "pact"
ARCHIVO = "polizas-adaptador-firma.json"
# Cliente sintético C01 (firmado) del simulador de firma (17.3); la póliza es el documento.
C01 = UUID("00000000-0000-4000-8000-000000000001")
POLIZA = UUID("00000000-0000-4000-c000-000000000001")
HUELLA = "sha256:" + "5e" * 32
CORRELACION = "corr-pacto-polizas"
pytestmark = pytest.mark.anyio


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="module", autouse=True)
def pacto_nuevo() -> None:
    (PACTOS / ARCHIVO).unlink(missing_ok=True)


@pytest.fixture
def pacto() -> Iterator[Pact]:
    pacto = Pact("polizas", "adaptador-firma").with_specification("V4")
    yield pacto
    pacto.write_file(PACTOS)


async def test_firma_de_la_huella_del_documento(pacto: Pact) -> None:
    (
        pacto.upon_receiving("la firma de la huella del documento de una póliza")
        .given("el proveedor de firma firma la huella del documento")
        .with_request("POST", "/v1/firmas")
        .with_header("X-Correlation-Id", CORRELACION)
        .with_body(
            {"documentoId": str(POLIZA), "huella": HUELLA, "firmanteRef": str(C01)},
            content_type="application/json",
        )
        .will_respond_with(200)
        .with_body(
            {
                # El mismo documento y la misma huella: polizas no acepta la firma de otros.
                "documentoId": str(POLIZA),
                "estado": "FIRMADO",
                "firmaId": match.like("FIR-SINT-C01"),
                # La hora del proveedor, con zona (el adaptador la conserva).
                "selloTiempo": match.like("2026-01-01T00:00:00Z"),
                "huella": HUELLA,
            },
            content_type="application/json",
        )
    )
    with pacto.serve() as servidor:
        async with correlation.cliente_async() as cliente:
            resultado = await AdaptadorFirmaHttp(cliente, str(servidor.url), 6500).firmar(
                SolicitudFirma(
                    documento_id=POLIZA,
                    huella=HUELLA,
                    firmante_ref=C01,
                    correlation_id=CORRELACION,
                )
            )
    assert resultado == FirmaObtenida(
        documento_id=POLIZA,
        huella=HUELLA,
        firma_id="FIR-SINT-C01",
        sello_tiempo=datetime(2026, 1, 1, tzinfo=UTC),
    )
