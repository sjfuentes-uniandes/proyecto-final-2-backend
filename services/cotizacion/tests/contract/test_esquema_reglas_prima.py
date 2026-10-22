"""CT-W03-01: la copia del consumidor del esquema de las reglas de prima es igual al publicado.

catalogo genera contracts/modelos/reglas-prima.v1.schema.json; si cambia, se ajusta
DefinicionReglasPrimaCotizacion (cotizacion.adapters.outbound.productos_catalogo) hasta que
coincida.
"""

import json
from pathlib import Path

from cotizacion.adapters.outbound.productos_catalogo import DefinicionReglasPrimaCotizacion

ESQUEMA = Path(__file__).parents[4] / "contracts" / "modelos" / "reglas-prima.v1.schema.json"


def test_ct_w03_01_esquema_del_consumidor_igual_al_publicado() -> None:
    publicado = json.loads(ESQUEMA.read_text(encoding="utf-8"))
    assert DefinicionReglasPrimaCotizacion.model_json_schema() == publicado
