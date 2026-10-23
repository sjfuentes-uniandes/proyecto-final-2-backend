"""CT-W13-02 (evento): el esquema publicado de DecisionSuscripcionRegistrada.v1 es el que genera
el código, y el evento de cada decisión lo cumple (sin montos ni datos personales).

Si cambia el evento, regenerar con: uv run python -m polizas.adapters.outbound.eventos
"""

import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from jsonschema import Draft202012Validator
from polizas.adapters.outbound.eventos import ESQUEMAS
from polizas.adapters.outbound.outbox_pg import evento_de
from polizas.application.suscripcion.solicitar_suscripcion import (
    ComandoSuscripcion,
    ConfigSuscripcion,
    SolicitarSuscripcion,
)
from polizas.domain.suscripcion.modelos import Actor, RolActor
from pydantic import BaseModel

EVENTOS = Path(__file__).parents[4] / "contracts" / "events"


@pytest.mark.parametrize(("archivo", "modelo"), ESQUEMAS.items())
def test_esquema_publicado_coincide(archivo: str, modelo: type[BaseModel]) -> None:
    publicado = json.loads((EVENTOS / archivo).read_text(encoding="utf-8"))
    assert publicado == modelo.model_json_schema(by_alias=True)


@pytest.mark.anyio
async def test_el_evento_de_cada_decision_cumple_el_esquema(
    id_caso_decidido: str, dobles: Any, sinteticos: Any
) -> None:
    caso_uso = SolicitarSuscripcion(
        dobles.insumos,
        dobles.reglas,
        dobles.decisiones,
        dobles.codigos,
        dobles.reloj,
        ConfigSuscripcion(),
    )
    comando = ComandoSuscripcion(
        UUID(sinteticos.cotizacion_id(id_caso_decidido)),
        uuid4(),
        Actor("sub-1", RolActor.ASESOR),
        "corr-contrato",
    )
    decision, _ = await caso_uso.ejecutar(comando)
    sobre = evento_de(decision).model_dump(mode="json", by_alias=True)
    esquema = json.loads(
        (EVENTOS / "DecisionSuscripcionRegistrada.v1.schema.json").read_text(encoding="utf-8")
    )
    assert list(Draft202012Validator(esquema).iter_errors(sobre)) == []
    payload = sobre["payload"]
    assert not {"sumaAsegurada", "primaTotal", "edad", "insumos"} & set(payload)
    assert payload["resultado"] == sinteticos.caso(id_caso_decidido)["esperado"]["resultado"]
