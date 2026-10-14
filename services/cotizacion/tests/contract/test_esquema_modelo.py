"""CT-W11-01: la copia del consumidor del esquema del modelo es igual al contrato publicado.

catalogo genera contracts/modelos/modelo-riesgo.v1.schema.json; si cambia, se ajusta
DefinicionModeloCotizacion (cotizacion.adapters.outbound.catalogo_http) hasta que coincida.
"""

import json
from pathlib import Path

from cotizacion.adapters.outbound.catalogo_http import DefinicionModeloCotizacion

ESQUEMA = Path(__file__).parents[4] / "contracts" / "modelos" / "modelo-riesgo.v1.schema.json"


def test_ct_w11_01_esquema_del_consumidor_igual_al_publicado() -> None:
    publicado = json.loads(ESQUEMA.read_text(encoding="utf-8"))
    assert DefinicionModeloCotizacion.model_json_schema() == publicado
