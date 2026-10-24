"""CT-W13-02 y CT-W15-01 (eventos): los esquemas publicados de DecisionSuscripcionRegistrada.v1 y
OfertaAceptada.v1 son los que genera el código, y cada evento cumple el suyo: la decisión sin
montos ni datos personales, la aceptación sin datos personales ni de medios de pago.

Si cambia el evento, regenerar con: uv run python -m polizas.adapters.outbound.eventos
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from jsonschema import Draft202012Validator
from polizas.adapters.outbound.eventos import ESQUEMAS
from polizas.adapters.outbound.outbox_pg import evento_de, evento_de_aceptacion
from polizas.application.suscripcion.solicitar_suscripcion import (
    ComandoSuscripcion,
    ConfigSuscripcion,
    SolicitarSuscripcion,
)
from polizas.domain.aceptacion.evidencia import huella_evidencia
from polizas.domain.aceptacion.modelos import Aceptacion, CanalAceptacion, Declaraciones
from polizas.domain.suscripcion.modelos import Actor, Decision, RolActor
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


async def _decision_aprobada(dobles: Any, sinteticos: Any) -> Decision:
    caso_uso = SolicitarSuscripcion(
        dobles.insumos,
        dobles.reglas,
        dobles.decisiones,
        dobles.codigos,
        dobles.reloj,
        ConfigSuscripcion(),
    )
    comando = ComandoSuscripcion(
        UUID(sinteticos.cotizacion_id("S01")), uuid4(), Actor("sub-1", RolActor.ASESOR), "corr"
    )
    decision, _ = await caso_uso.ejecutar(comando)
    return decision


def _aceptacion(decision: Decision, oferta_id: UUID | None) -> Aceptacion:
    return Aceptacion(
        id=uuid4(),
        cotizacion_id=decision.cotizacion_id,
        oferta_id=oferta_id,
        decision_id=decision.id,
        cliente_id=decision.cliente_id,
        huella_calculo=decision.insumos.huella_calculo,
        vigente_hasta=decision.insumos.vigente_hasta,
        version_declaraciones="2026.10.1",
        declaraciones=Declaraciones(True, True, False, True),
        canal=CanalAceptacion.PORTAL_ASESOR,
        actor_id="sub-asesor-1",
        aceptada_en=datetime(2026, 10, 24, 15, 0, tzinfo=UTC),
        idempotency_key=uuid4(),
        huella_solicitud="huella-solicitud",
        huella_evidencia="",
        correlation_id="corr-aceptacion",
    )


@pytest.mark.anyio
@pytest.mark.parametrize("con_oferta", [True, False], ids=["con-oferta", "anterior-a-w04"])
async def test_el_evento_de_la_aceptacion_cumple_el_esquema(
    dobles: Any, sinteticos: Any, con_oferta: bool
) -> None:
    decision = await _decision_aprobada(dobles, sinteticos)
    aceptacion = _aceptacion(decision, decision.oferta_id if con_oferta else None)
    sobre = evento_de_aceptacion(aceptacion, decision).model_dump(mode="json", by_alias=True)
    esquema = json.loads((EVENTOS / "OfertaAceptada.v1.schema.json").read_text(encoding="utf-8"))
    assert list(Draft202012Validator(esquema).iter_errors(sobre)) == []

    assert sobre["type"] == "OfertaAceptada"
    assert sobre["version"] == 1
    assert sobre["aggregateId"] == str(aceptacion.id)
    assert sobre["correlationId"] == "corr-aceptacion"
    payload = sobre["payload"]
    assert payload["claveRecaudo"] == payload["aceptacionId"] == str(aceptacion.id)
    assert payload["ofertaId"] == (str(decision.oferta_id) if con_oferta else None)
    assert payload["prima"] == {
        "valor": sinteticos.caso("S01")["insumos"]["primaTotal"],
        "moneda": "COP",
        "periodicidad": decision.insumos.periodicidad,
    }
    assert payload["canal"] == "PORTAL_ASESOR"
    assert payload["vigenteHasta"].startswith("2026-10-26T14:00:00")


@pytest.mark.anyio
async def test_el_evento_de_la_aceptacion_no_lleva_datos_personales_ni_de_pago(
    dobles: Any, sinteticos: Any
) -> None:
    decision = await _decision_aprobada(dobles, sinteticos)
    aceptacion = _aceptacion(decision, decision.oferta_id)
    payload = evento_de_aceptacion(aceptacion, decision).payload.model_dump(by_alias=True)
    prohibidos = {
        "nombre",
        "nombreCompleto",
        "clienteNombre",
        "documento",
        "fechaNacimiento",
        "edad",
        "correo",
        "telefono",
        "sumaAsegurada",
        "ingresos",
        "insumos",
        "declaraciones",
        "medioPago",
        "cuenta",
        "tarjeta",
        "huellaEvidencia",
    }
    assert not prohibidos & set(payload)
    assert not prohibidos & set(payload["prima"])
    # La evidencia no viaja en el evento: su huella se recalcula en polizas.
    assert huella_evidencia(aceptacion).startswith("sha256:")
