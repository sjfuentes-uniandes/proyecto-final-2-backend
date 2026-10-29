"""MT-W16-06 (16.6): el relay del outbox cableado en polizas, con PostgreSQL y LocalStack reales.

Con la app real de polizas (habilitada), una suscripción y una aceptación por la API dejan dos filas
en el outbox (`DecisionSuscripcionRegistrada` y `OfertaAceptada`). El relay las publica en el
tópico: la cola con el filtro de `dinero-aceptaciones` recibe solo `OfertaAceptada`; la cola sin
filtro (como `auditoria`) recibe las dos; `published_at` queda marcado. Deshabilitado (el valor por
defecto), no llega nada y las filas siguen pendientes.
"""

import asyncio
import json
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from jsonschema import Draft202012Validator

docker = pytest.importorskip("docker")
pytestmark = pytest.mark.anyio
EVENTOS = Path(__file__).parents[4] / "contracts/events"
ASESOR = {"X-Actor-Id": "sub-asesor-1", "X-Actor-Rol": "ASESOR"}
TODAS = {
    "leidoCondiciones": True,
    "informacionVeraz": True,
    "debitoAutomatico": True,
    "avisosElectronicos": True,
}


def _esquema(tipo: str) -> Draft202012Validator:
    return Draft202012Validator(
        json.loads((EVENTOS / f"{tipo}.v1.schema.json").read_text(encoding="utf-8"))
    )


def _filas(url: str, sql: str) -> list[tuple[Any, ...]]:
    with psycopg.connect(url, autocommit=True) as conexion:
        return conexion.execute(sql).fetchall()


async def _decidir_y_aceptar(api: Any, sinteticos: Any) -> str:
    """Suscripción y aceptación del caso S01 por la API; devuelve el correlationId de la
    aceptación."""
    decision = await api.post(
        "/v1/suscripciones",
        json={"cotizacionId": sinteticos.cotizacion_id("S01")},
        headers={**ASESOR, "Idempotency-Key": str(uuid4())},
    )
    assert decision.status_code == 201, decision.json()
    cuerpo = decision.json()
    aceptacion = await api.post(
        f"/v1/cotizaciones/{cuerpo['cotizacionId']}/aceptacion",
        json={
            "decisionId": cuerpo["id"],
            "versionDeclaraciones": "2026.10.1",
            "declaraciones": TODAS,
        },
        headers={**ASESOR, "Idempotency-Key": str(uuid4())},
    )
    assert aceptacion.status_code == 201, aceptacion.json()
    correlacion: str = aceptacion.headers["X-Correlation-Id"]
    return correlacion


async def _esperar_publicados(url: str, cantidad: int, limite_s: float = 15) -> None:
    # El relay publica y luego hace commit: el mensaje puede llegar antes que la marca.
    fin = time.monotonic() + limite_s
    sql = "SELECT count(*) FROM outbox WHERE published_at IS NOT NULL"
    while _filas(url, sql)[0][0] < cantidad:
        assert time.monotonic() < fin, "el relay no marcó published_at a tiempo"
        await asyncio.sleep(0.1)


def _tipo(mensaje: dict[str, Any]) -> str:
    tipo: str = mensaje["MessageAttributes"]["eventType"]["StringValue"]
    return tipo


async def test_habilitado_enruta_por_event_type_y_marca_published_at(
    abrir_api_con_relay: Any, mensajeria: Any, sinteticos: Any, limpia: str
) -> None:
    async with abrir_api_con_relay(True) as api:
        correlacion = await _decidir_y_aceptar(api, sinteticos)
        auditoria = await asyncio.to_thread(mensajeria.recibir, mensajeria.auditoria, 2)
        aceptaciones = await asyncio.to_thread(mensajeria.recibir, mensajeria.aceptaciones, 1)
        await _esperar_publicados(limpia, 2)
        # Nada más llega a la cola filtrada (la decisión no pasa el filtro).
        assert await asyncio.to_thread(mensajeria.vacia, mensajeria.aceptaciones)

    assert sorted(_tipo(m) for m in auditoria) == [
        "DecisionSuscripcionRegistrada",
        "OfertaAceptada",
    ]
    [oferta] = aceptaciones
    assert _tipo(oferta) == "OfertaAceptada"
    sobre = json.loads(oferta["Body"])
    assert list(_esquema("OfertaAceptada").iter_errors(sobre)) == []
    assert sobre["type"] == "OfertaAceptada" and sobre["correlationId"] == correlacion
    for mensaje in auditoria:
        cuerpo = json.loads(mensaje["Body"])
        assert cuerpo["type"] == _tipo(mensaje)
        assert list(_esquema(cuerpo["type"]).iter_errors(cuerpo)) == []

    filas = _filas(limpia, "SELECT event_id, event_type, published_at, attempts FROM outbox")
    assert {(str(e), t) for e, t, _, _ in filas} == {
        (json.loads(m["Body"])["eventId"], _tipo(m)) for m in auditoria
    }
    assert all(publicado is not None and intentos == 0 for _, _, publicado, intentos in filas)


async def test_deshabilitado_no_publica_y_las_filas_siguen_pendientes(
    abrir_api_con_relay: Any, mensajeria: Any, sinteticos: Any, limpia: str
) -> None:
    async with abrir_api_con_relay(False) as api:
        await _decidir_y_aceptar(api, sinteticos)
        # Con la app abierta: si el relay corriera (intervalo 0,1 s), publicaría en esta espera.
        assert await asyncio.to_thread(mensajeria.vacia, mensajeria.auditoria)
        assert await asyncio.to_thread(mensajeria.vacia, mensajeria.aceptaciones)

    filas = _filas(limpia, "SELECT event_type, published_at FROM outbox")
    assert sorted(t for t, _ in filas) == ["DecisionSuscripcionRegistrada", "OfertaAceptada"]
    assert all(publicado is None for _, publicado in filas)
