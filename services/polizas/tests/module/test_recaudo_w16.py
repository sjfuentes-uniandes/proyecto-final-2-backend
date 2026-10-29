"""MT-W16-13 (16.13): polizas proyecta el estado del recaudo desde `polizas-recaudos` y lo expone,
con PostgreSQL y LocalStack reales.

Una suscripción y una aceptación por la API de polizas; un `RecaudoConfirmado` o `RecaudoRechazado`
publicado en el tópico (como el relay de dinero) llega a la cola filtrada por el prefijo `Recaudo`;
el consumidor de la app lo registra en el inbox y lo proyecta en la misma transacción, y
`GET …/recaudo` lo devuelve. Un duplicado tiene un solo efecto; un envenenado acaba en la DLQ. Sin
evento, el recaudo está PENDIENTE con la prima de la instantánea. Más la migración 003 hacia
adelante, hacia atrás y otra vez hacia adelante.
"""

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from decimal import Decimal
from importlib import resources
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from jsonschema import Draft202012Validator
from polizas.adapters.outbound.decisiones_pg import migrar

docker = pytest.importorskip("docker")
pytestmark = pytest.mark.anyio
MIGRACIONES = resources.files("polizas.adapters.outbound").joinpath("migraciones")
EVENTOS = Path(__file__).parents[4] / "contracts/events"
ASESOR = {"X-Actor-Id": "sub-asesor-1", "X-Actor-Rol": "ASESOR"}
TODAS = {
    "leidoCondiciones": True,
    "informacionVeraz": True,
    "debitoAutomatico": True,
    "avisosElectronicos": True,
}
RESUELTO_EN = "2026-10-24T15:00:01.250000Z"


def _filas(url: str, sql: str, parametros: Any = None) -> list[tuple[Any, ...]]:
    with psycopg.connect(url, autocommit=True) as conexion:
        return conexion.execute(sql, parametros).fetchall()


def _sobre(tipo: str, clave: str) -> dict[str, Any]:
    """Un sobre que cumple el esquema publicado por dinero (se comprueba aquí mismo)."""
    recaudo_id = str(uuid4())
    datos: dict[str, Any] = {
        "recaudoId": recaudo_id,
        "claveRecaudo": clave,
        "cotizacionId": str(uuid4()),
    }
    if tipo == "RecaudoConfirmado":
        datos |= {
            "monto": {"valor": "64638.10", "moneda": "COP"},
            "transaccionId": "TX-SINTETICA-0001",
            "confirmadoEn": RESUELTO_EN,
        }
    else:
        datos |= {"motivo": "FONDOS_INSUFICIENTES", "rechazadoEn": RESUELTO_EN}
    sobre = {
        "eventId": str(uuid4()),
        "aggregateId": recaudo_id,
        "type": tipo,
        "version": 1,
        "occurredAt": RESUELTO_EN,
        "correlationId": "corr-recaudo-modulo",
        "payload": datos,
    }
    esquema = json.loads((EVENTOS / f"{tipo}.v1.schema.json").read_text(encoding="utf-8"))
    assert list(Draft202012Validator(esquema).iter_errors(sobre)) == []
    return sobre


async def _aceptar(api: Any, sinteticos: Any) -> dict[str, Any]:
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
    resultado: dict[str, Any] = aceptacion.json()
    return resultado


async def _estado(api: Any, cotizacion_id: str) -> dict[str, Any]:
    respuesta = await api.get(f"/v1/cotizaciones/{cotizacion_id}/recaudo", headers=ASESOR)
    assert respuesta.status_code == 200, respuesta.json()
    cuerpo: dict[str, Any] = respuesta.json()
    return cuerpo


async def _esperar(condicion: Callable[[], Awaitable[bool]], limite_s: float = 20) -> None:
    fin = time.monotonic() + limite_s
    while not await condicion():
        assert time.monotonic() < fin, "la condición no se cumplió a tiempo"
        await asyncio.sleep(0.2)


async def _esperar_estado(api: Any, cotizacion_id: str, estado: str) -> dict[str, Any]:
    async def listo() -> bool:
        return (await _estado(api, cotizacion_id))["estado"] == estado

    await _esperar(listo)
    return await _estado(api, cotizacion_id)


async def _sin_pendientes(cola: Any, url: str) -> bool:
    return await asyncio.to_thread(cola.pendientes, url) == 0


async def test_sin_evento_el_recaudo_esta_pendiente_con_la_prima_de_la_instantanea(
    api: Any, sinteticos: Any
) -> None:
    aceptacion = await _aceptar(api, sinteticos)
    insumos = sinteticos.caso("S01")["insumos"]
    assert await _estado(api, aceptacion["cotizacionId"]) == {
        "estado": "PENDIENTE",
        "claveRecaudo": aceptacion["id"],
        "monto": insumos["primaTotal"],
        "moneda": insumos["moneda"],
        "transaccionId": None,
        "motivo": None,
        "actualizadoEn": aceptacion["aceptadaEn"],
    }
    sin_aceptacion = await api.get(f"/v1/cotizaciones/{uuid4()}/recaudo", headers=ASESOR)
    assert (sin_aceptacion.status_code, sin_aceptacion.json()["codigo"]) == (
        404,
        "ACEPTACION_NO_ENCONTRADA",
    )


async def test_recaudo_confirmado_en_la_cola_se_proyecta_y_la_ruta_lo_devuelve(
    abrir_api_con_consumidor: Any, cola_recaudos: Any, sinteticos: Any, limpia: str
) -> None:
    async with abrir_api_con_consumidor() as api:
        aceptacion = await _aceptar(api, sinteticos)
        sobre = _sobre("RecaudoConfirmado", aceptacion["id"])
        await asyncio.to_thread(cola_recaudos.publicar, sobre)
        estado = await _esperar_estado(api, aceptacion["cotizacionId"], "CONFIRMADO")
        await _esperar(lambda: _sin_pendientes(cola_recaudos, cola_recaudos.cola))

    assert estado == {
        "estado": "CONFIRMADO",
        "claveRecaudo": aceptacion["id"],
        "monto": "64638.10",
        "moneda": "COP",
        "transaccionId": "TX-SINTETICA-0001",
        "motivo": None,
        "actualizadoEn": RESUELTO_EN,
    }
    # Inbox y proyección en la misma base, una fila cada uno; el monto como numeric sin float.
    assert _filas(limpia, "SELECT event_id::text, event_type FROM inbox") == [
        (sobre["eventId"], "RecaudoConfirmado")
    ]
    [(estado_fila, monto, recaudo_id)] = _filas(
        limpia, "SELECT estado, monto, recaudo_id::text FROM recaudos_aceptacion"
    )
    assert (estado_fila, monto, recaudo_id) == (
        "CONFIRMADO",
        Decimal("64638.10"),
        sobre["payload"]["recaudoId"],
    )
    assert await asyncio.to_thread(cola_recaudos.pendientes, cola_recaudos.dlq) == 0


async def test_recaudo_rechazado_queda_rechazado_con_motivo(
    abrir_api_con_consumidor: Any, cola_recaudos: Any, sinteticos: Any
) -> None:
    async with abrir_api_con_consumidor() as api:
        aceptacion = await _aceptar(api, sinteticos)
        await asyncio.to_thread(
            cola_recaudos.publicar, _sobre("RecaudoRechazado", aceptacion["id"])
        )
        estado = await _esperar_estado(api, aceptacion["cotizacionId"], "RECHAZADO")

    insumos = sinteticos.caso("S01")["insumos"]
    assert estado == {
        "estado": "RECHAZADO",
        "claveRecaudo": aceptacion["id"],
        "monto": insumos["primaTotal"],
        "moneda": insumos["moneda"],
        "transaccionId": None,
        "motivo": "FONDOS_INSUFICIENTES",
        "actualizadoEn": RESUELTO_EN,
    }


async def test_el_mismo_evento_dos_veces_tiene_un_solo_efecto(
    abrir_api_con_consumidor: Any, cola_recaudos: Any, sinteticos: Any, limpia: str
) -> None:
    async with abrir_api_con_consumidor() as api:
        aceptacion = await _aceptar(api, sinteticos)
        sobre = _sobre("RecaudoConfirmado", aceptacion["id"])
        await asyncio.to_thread(cola_recaudos.publicar, sobre)
        await asyncio.to_thread(cola_recaudos.publicar, sobre)
        # Los dos se consumen y se borran (el segundo como duplicado), y nada va a la DLQ.
        await _esperar(lambda: _sin_pendientes(cola_recaudos, cola_recaudos.cola))
        estado = await _estado(api, aceptacion["cotizacionId"])

    assert estado["estado"] == "CONFIRMADO"
    assert _filas(limpia, "SELECT count(*) FROM inbox") == [(1,)]
    assert _filas(limpia, "SELECT count(*) FROM recaudos_aceptacion") == [(1,)]
    assert await asyncio.to_thread(cola_recaudos.pendientes, cola_recaudos.dlq) == 0


@pytest.mark.parametrize(
    ("tipo", "cambio"),
    [
        ("RecaudoConfirmado", {"transaccionId": None}),
        ("RecaudoPendiente", {}),
    ],
    ids=["sin_transaccion", "type_ajeno_con_el_prefijo"],
)
async def test_un_envenenado_va_a_la_dlq_sin_efecto(
    abrir_api_con_consumidor: Any,
    cola_recaudos: Any,
    sinteticos: Any,
    limpia: str,
    tipo: str,
    cambio: dict[str, Any],
) -> None:
    async with abrir_api_con_consumidor() as api:
        aceptacion = await _aceptar(api, sinteticos)
        sobre = _sobre("RecaudoConfirmado", aceptacion["id"])
        sobre["type"] = tipo
        sobre["payload"] |= cambio
        await asyncio.to_thread(cola_recaudos.publicar, sobre)

        async def en_dlq() -> bool:
            return await asyncio.to_thread(cola_recaudos.pendientes, cola_recaudos.dlq) == 1

        await _esperar(en_dlq, limite_s=30)
        estado = await _estado(api, aceptacion["cotizacionId"])

    assert estado["estado"] == "PENDIENTE"
    assert _filas(limpia, "SELECT count(*) FROM inbox") == [(0,)]
    assert _filas(limpia, "SELECT count(*) FROM recaudos_aceptacion") == [(0,)]
    assert await asyncio.to_thread(cola_recaudos.pendientes, cola_recaudos.cola) == 0


async def test_una_clave_sin_aceptacion_se_guarda_sin_llave(
    abrir_api_con_consumidor: Any, cola_recaudos: Any, limpia: str
) -> None:
    # La QA de la 16.12 publica eventos sintéticos con claves que polizas no conoce: con una llave
    # hacia aceptaciones irían a la DLQ.
    clave = str(uuid4())
    async with abrir_api_con_consumidor():
        await asyncio.to_thread(cola_recaudos.publicar, _sobre("RecaudoRechazado", clave))

        async def proyectada() -> bool:
            return _filas(limpia, "SELECT count(*) FROM recaudos_aceptacion") == [(1,)]

        await _esperar(proyectada)

    assert _filas(limpia, "SELECT clave_recaudo::text, estado FROM recaudos_aceptacion") == [
        (clave, "RECHAZADO")
    ]
    assert await asyncio.to_thread(cola_recaudos.pendientes, cola_recaudos.dlq) == 0


async def test_migracion_003_adelante_atras_y_adelante(base: str) -> None:
    """Revertir 003 (dos veces) deja las aceptaciones; volver a aplicar recrea el inbox y la
    proyección, con sus restricciones de forma."""
    reversa = MIGRACIONES.joinpath("down", "003_recaudos.sql").read_text(encoding="utf-8")
    with psycopg.connect(base, autocommit=True) as conexion:
        conexion.execute(reversa)
        conexion.execute(reversa)
    existe = "SELECT to_regclass('{}') IS NOT NULL"
    assert _filas(base, existe.format("inbox")) == [(False,)]
    assert _filas(base, existe.format("recaudos_aceptacion")) == [(False,)]
    assert _filas(base, existe.format("aceptaciones")) == [(True,)]
    migrar(base)
    migrar(base)
    assert _filas(base, existe.format("inbox")) == [(True,)]
    assert _filas(base, existe.format("recaudos_aceptacion")) == [(True,)]
    # Sin llaves hacia aceptaciones.
    llaves = (
        "SELECT count(*) FROM pg_constraint WHERE contype = 'f' "
        "AND conrelid = 'recaudos_aceptacion'::regclass"
    )
    assert _filas(base, llaves) == [(0,)]
    # Forma: CONFIRMADO con transacción, monto y moneda; RECHAZADO con motivo y sin ellos.
    insertar = (
        "INSERT INTO recaudos_aceptacion (clave_recaudo, recaudo_id, estado, monto, moneda, "
        "transaccion_id, motivo, ocurrido_en) VALUES (%s, %s, %s, %s, %s, %s, %s, now())"
    )
    invalidas = [
        ("CONFIRMADO", Decimal(1), "COP", None, None),
        ("CONFIRMADO", None, "COP", "TX", None),
        ("CONFIRMADO", Decimal(1), "COP", "TX", "MOTIVO"),
        ("RECHAZADO", None, None, None, None),
        ("RECHAZADO", None, None, "TX", "MOTIVO"),
        ("RECHAZADO", Decimal(1), "COP", None, "MOTIVO"),
        ("PENDIENTE", None, None, None, None),
    ]
    with psycopg.connect(base, autocommit=True) as conexion:
        for fila in invalidas:
            with pytest.raises(psycopg.errors.CheckViolation):
                conexion.execute(insertar, (uuid4(), uuid4(), *fila))
        conexion.execute(
            insertar, (uuid4(), uuid4(), "CONFIRMADO", Decimal("1.5"), "COP", "T", None)
        )
        conexion.execute(insertar, (uuid4(), uuid4(), "RECHAZADO", None, None, None, "MOTIVO"))
        conexion.execute("TRUNCATE recaudos_aceptacion")
