"""El contrato publicado (contracts/openapi/adaptador-firma.yaml) es el que sirve la app.

El Pact `polizas` → `adaptador-firma` (solo el 200 firmado) llega en la 17.8: su verificación de
proveedor irá en este directorio (`test_pact_polizas.py`), con la app real y el proveedor de firma
simulado, como `adaptador-pagos/tests/contract/test_pact_dinero.py`.
"""

from pathlib import Path

import yaml
from adaptador_firma.main import app

CONTRATO = Path(__file__).parents[4] / "contracts" / "openapi" / "adaptador-firma.yaml"


def test_contrato_publicado_coincide_con_la_app() -> None:
    assert yaml.safe_load(CONTRATO.read_text(encoding="utf-8")) == app.openapi()


def test_contrato_de_la_firma() -> None:
    operacion = app.openapi()["paths"]["/v1/firmas"]["post"]
    assert set(operacion["responses"]) == {"200", "400", "422", "502", "503", "504"}
    assert "parameters" not in operacion
    esquemas = app.openapi()["components"]["schemas"]
    solicitud = esquemas["SolicitudFirma"]
    assert set(solicitud["required"]) == {"documentoId", "huella", "firmanteRef"}
    assert solicitud["properties"]["documentoId"]["format"] == "uuid"
    assert solicitud["properties"]["firmanteRef"]["format"] == "uuid"
    assert solicitud["properties"]["huella"]["type"] == "string"
    assert solicitud["properties"]["huella"]["pattern"] == "^sha256:[0-9a-f]{64}$"


def test_la_respuesta_de_la_firma() -> None:
    operacion = app.openapi()["paths"]["/v1/firmas"]["post"]
    esquema = operacion["responses"]["200"]["content"]["application/json"]["schema"]
    assert esquema == {"$ref": "#/components/schemas/FirmaDto"}
    esquemas = app.openapi()["components"]["schemas"]
    firma = esquemas["FirmaDto"]
    assert set(firma["required"]) == {"documentoId", "estado", "firmaId", "selloTiempo", "huella"}
    assert firma["properties"]["documentoId"]["format"] == "uuid"
    assert firma["properties"]["selloTiempo"]["format"] == "date-time"
    assert esquemas["EstadoFirma"]["enum"] == ["FIRMADO"]
