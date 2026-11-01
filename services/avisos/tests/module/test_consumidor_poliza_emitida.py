"""N2 de HU-M11: avisos consume `PolizaEmitida` y envía el push, con PostgreSQL, LocalStack (cola con
DLQ, `maxReceiveCount` = 5) y el simulador push (WireMock con los perfiles reales de
`simulador-aliados`) detrás de `adaptador-firma` real (en proceso).

El consumidor y el barrido son los de producción (`main.armar_consumidor`, `main.armar_envio`). Los
envíos se cuentan en el journal del simulador: ningún duplicado, reentrega ni barrido envía dos veces
el mismo aviso. Tokens: solo los sintéticos del simulador; cualquier otro responde 404
`token_no_reconocido` (el token vencido).
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from adaptador_firma.main import crear_app as crear_app_firma
from avisos.adapters.outbound.dispositivos_pg import DispositivosPg
from avisos.adapters.outbound.push_http import EnviadorPushHttp
from avisos.adapters.outbound.reloj import RelojSistema
from avisos.application.entregas import enviar as modulo_enviar
from avisos.application.entregas.barrido import Barrido
from avisos.application.entregas.enviar import MENSAJE_TOKEN_INVALIDO
from avisos.config import Settings
from avisos.domain.avisos.modelos import (
    FALLO_DISPOSITIVO_DADO_DE_BAJA,
    FALLO_DISPOSITIVO_REASIGNADO,
    FALLO_REINTENTOS_AGOTADOS,
    MOTIVO_TOKEN_RECHAZADO,
    hash_token,
)
from avisos.main import armar_consumidor, armar_envio
from psycopg_pool import AsyncConnectionPool
from solventa_common import correlation
from solventa_common.inbox import ConsumidorInbox
from solventa_common.resiliencia import SinProteccion

from .conftest import Simulador

pytestmark = pytest.mark.anyio

ENTREGADO = "token-sintetico-entregado"
CAIDO = "token-sintetico-caido"
VENCIDO = "token-sintetico-vencido"  # fuera del catálogo: 404 token_no_reconocido
SETTINGS = Settings(
    entregas_max_intentos=3,
    entregas_espera_inicial_s=0.3,
    entregas_espera_max_s=0.6,
    entregas_lease_s=60,
)


def filas(url: str, sql: str, parametros: Any = None) -> list[tuple[Any, ...]]:
    with psycopg.connect(url, autocommit=True) as conexion:
        cursor = conexion.execute(sql, parametros)
        return cursor.fetchall() if cursor.description else []


def entregas(url: str) -> dict[str, tuple[str, int, str | None]]:
    """token → (estado, intentos, último fallo)."""
    return {
        token: (estado, intentos, fallo)
        for token, estado, intentos, fallo in filas(
            url,
            "SELECT d.token, e.estado, e.intentos, e.ultimo_fallo FROM entrega e"
            " JOIN dispositivo d ON d.id = e.dispositivo_id",
        )
    }


def dispositivo(url: str, token: str) -> tuple[str, str | None]:
    [fila] = filas(
        url, "SELECT estado, motivo_invalidez FROM dispositivo WHERE token = %s", (token,)
    )
    return fila[0], fila[1]


class Sistema:
    """Consumidor y barrido de producción sobre el simulador, vía `adaptador-firma`."""

    def __init__(self, pool: Any, enviador: EnviadorPushHttp) -> None:
        self.pool = pool
        self.enviador = enviador
        self.reloj = RelojSistema()
        self.dispositivos = DispositivosPg(pool)

    def consumidor(self, cola: Any, max_mensajes: int = 2) -> ConsumidorInbox[Any]:
        return armar_consumidor(
            self.pool, cola.receptor(max_mensajes), self.enviador, self.reloj, SETTINGS
        )

    async def barrer(self) -> int:
        _, enviar = armar_envio(self.pool, self.enviador, self.reloj, SETTINGS)
        return await Barrido(enviar, 10).ejecutar_una_vez()

    async def registrar(self, cliente: UUID, token: str) -> UUID:
        return await self.dispositivos.registrar(cliente, token, datetime.now(UTC))


@pytest.fixture
async def sistema(
    base: str, simulador: Simulador, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Sistema]:
    filas(base, "TRUNCATE entrega, dispositivo, inbox CASCADE")
    monkeypatch.setenv("SOLVENTA_ALLY_PUSH_URL", f"{simulador.url}/push")
    firma = crear_app_firma()
    async with (
        firma.router.lifespan_context(firma),
        correlation.cliente_async(transport=httpx.ASGITransport(app=firma)) as cliente,
        AsyncConnectionPool(base, min_size=1, max_size=8, open=False) as pool,
    ):
        enviador = EnviadorPushHttp(
            cliente, "http://adaptador-firma", 3000, proteccion=SinProteccion(corte_duro_ms=3000)
        )
        yield Sistema(pool, enviador)


@pytest.fixture
def metricas(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Las métricas `aviso.push` registradas: (operación, resultado)."""
    registradas: list[tuple[str, str]] = []
    original = modulo_enviar.registrar

    def registrar(nombre: str, duracion_ms: float, resultado: str, **extra: Any) -> None:
        registradas.append((nombre, resultado))
        original(nombre, duracion_ms, resultado, **extra)

    monkeypatch.setattr(modulo_enviar, "registrar", registrar)
    return registradas


async def drenar(consumidor: ConsumidorInbox[Any], procesados: int, limite_s: float = 30) -> None:
    """Ciclos hasta procesar `procesados` mensajes (nuevos y duplicados)."""
    total = 0
    limite = time.monotonic() + limite_s
    while total < procesados:
        assert time.monotonic() < limite, f"sin procesar a tiempo ({total} de {procesados})"
        total += await consumidor.ejecutar_una_vez()


def evento(sobre_poliza: Callable[..., dict[str, Any]], cliente: UUID, **sobre: Any) -> Any:
    import json

    return json.dumps(sobre_poliza(sobre or None, clienteId=str(cliente)))


async def test_un_dispositivo_recibe_un_aviso(
    sistema: Sistema,
    simulador: Simulador,
    nueva_cola: Callable[..., Any],
    base: str,
    sobre_poliza: Callable[..., dict[str, Any]],
) -> None:
    cola = nueva_cola()
    cliente = uuid4()
    await sistema.registrar(cliente, ENTREGADO)
    cola.enviar(evento(sobre_poliza, cliente))
    await drenar(sistema.consumidor(cola), 1)

    assert len(simulador.envios(ENTREGADO)) == 1
    assert entregas(base) == {ENTREGADO: ("ENVIADO", 1, None)}
    assert cola.mensajes(cola.url) == 0 and cola.mensajes(cola.dlq) == 0


async def test_el_mismo_evento_dos_veces_envia_un_solo_aviso(
    sistema: Sistema,
    simulador: Simulador,
    nueva_cola: Callable[..., Any],
    base: str,
    sobre_poliza: Callable[..., dict[str, Any]],
) -> None:
    cola = nueva_cola()
    cliente = uuid4()
    await sistema.registrar(cliente, ENTREGADO)
    cuerpo = evento(sobre_poliza, cliente)
    cola.enviar(cuerpo)
    cola.enviar(cuerpo)
    await drenar(sistema.consumidor(cola), 2)

    assert len(simulador.envios()) == 1
    assert entregas(base) == {ENTREGADO: ("ENVIADO", 1, None)}
    assert len(filas(base, "SELECT 1 FROM inbox")) == 1
    assert cola.mensajes(cola.url) == 0


async def test_token_vencido_invalida_el_dispositivo_y_confirma_el_evento_sin_dlq(
    sistema: Sistema,
    simulador: Simulador,
    nueva_cola: Callable[..., Any],
    base: str,
    sobre_poliza: Callable[..., dict[str, Any]],
    metricas: list[tuple[str, str]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    cola = nueva_cola()
    cliente = uuid4()
    await sistema.registrar(cliente, VENCIDO)
    cola.enviar(evento(sobre_poliza, cliente))
    await drenar(sistema.consumidor(cola), 1)

    assert dispositivo(base, VENCIDO) == ("INVALIDO", MOTIVO_TOKEN_RECHAZADO)
    assert entregas(base)[VENCIDO][0] == "TOKEN_INVALIDO"
    assert ("aviso.push", "rechazado") in metricas
    assert ("aviso.push", "error") not in metricas
    assert MENSAJE_TOKEN_INVALIDO in caplog.text
    assert VENCIDO not in caplog.text and hash_token(VENCIDO) not in caplog.text
    assert len(simulador.envios(VENCIDO)) == 1
    assert cola.mensajes(cola.url) == 0 and cola.mensajes(cola.dlq) == 0


async def test_proveedor_caido_reintenta_con_espera_y_termina_fallido_sin_dlq(
    sistema: Sistema,
    simulador: Simulador,
    nueva_cola: Callable[..., Any],
    base: str,
    sobre_poliza: Callable[..., dict[str, Any]],
    metricas: list[tuple[str, str]],
) -> None:
    cola = nueva_cola()
    cliente = uuid4()
    await sistema.registrar(cliente, CAIDO)
    cola.enviar(evento(sobre_poliza, cliente))
    await drenar(sistema.consumidor(cola), 1)

    # El primer intento falló: queda PENDIENTE con espera; el barrido aún no la toma.
    assert entregas(base)[CAIDO][:2] == ("PENDIENTE", 1)
    assert await sistema.barrer() == 0
    limite = time.monotonic() + 20
    while entregas(base)[CAIDO][0] != "FALLIDO":
        assert time.monotonic() < limite, "la entrega no llegó a FALLIDO"
        await sistema.barrer()
        await asyncio.sleep(0.1)

    assert entregas(base)[CAIDO] == ("FALLIDO", 3, FALLO_REINTENTOS_AGOTADOS)
    assert metricas.count(("aviso.push", "error")) == 3
    assert ("aviso.push", "ok") not in metricas
    assert cola.mensajes(cola.url) == 0 and cola.mensajes(cola.dlq) == 0


async def test_con_un_dispositivo_invalido_el_otro_recibe_su_aviso(
    sistema: Sistema,
    simulador: Simulador,
    nueva_cola: Callable[..., Any],
    base: str,
    sobre_poliza: Callable[..., dict[str, Any]],
) -> None:
    cola = nueva_cola()
    cliente = uuid4()
    await sistema.registrar(cliente, VENCIDO)
    await sistema.registrar(cliente, ENTREGADO)
    cola.enviar(evento(sobre_poliza, cliente))
    await drenar(sistema.consumidor(cola), 1)

    assert entregas(base) == {
        VENCIDO: ("TOKEN_INVALIDO", 1, MOTIVO_TOKEN_RECHAZADO),
        ENTREGADO: ("ENVIADO", 1, None),
    }
    assert dispositivo(base, VENCIDO)[0] == "INVALIDO"
    assert dispositivo(base, ENTREGADO)[0] == "ACTIVO"
    assert len(simulador.envios(ENTREGADO)) == 1
    assert cola.mensajes(cola.url) == 0 and cola.mensajes(cola.dlq) == 0


async def test_poliza_emitida_v2_va_a_la_dlq_tras_los_reintentos_de_la_cola(
    sistema: Sistema,
    simulador: Simulador,
    nueva_cola: Callable[..., Any],
    base: str,
    sobre_poliza: Callable[..., dict[str, Any]],
) -> None:
    cola = nueva_cola()
    cliente = uuid4()
    await sistema.registrar(cliente, ENTREGADO)
    cuerpo = evento(sobre_poliza, cliente, version=2)
    cola.enviar(cuerpo)
    consumidor = sistema.consumidor(cola)
    limite = time.monotonic() + 40
    while cola.mensajes(cola.dlq) == 0:
        assert time.monotonic() < limite, "el mensaje no llegó a la DLQ"
        assert await consumidor.ejecutar_una_vez() == 0

    assert simulador.envios() == []
    assert filas(base, "SELECT 1 FROM inbox") == [] and entregas(base) == {}
    assert cola.mensajes(cola.url) == 0
    [en_dlq] = cola.sqs.receive_message(QueueUrl=cola.dlq, WaitTimeSeconds=1)["Messages"]
    assert en_dlq["Body"] == cuerpo


async def test_entrega_en_envio_con_lease_vencido_la_retoma_el_barrido_sin_segundo_aviso(
    sistema: Sistema, simulador: Simulador, base: str
) -> None:
    dispositivo_id = await sistema.registrar(uuid4(), ENTREGADO)
    ahora = datetime.now(UTC)
    # Una tarea reclamó la entrega y murió antes de enviar: EN_ENVIO con el lease ya vencido.
    filas(
        base,
        "INSERT INTO entrega (event_id, dispositivo_id, cliente_id, tipo, poliza_id, estado, intentos,"
        " proximo_intento, reclamada_hasta, creada_en, actualizada_en)"
        " SELECT %s, id, cliente_id, 'POLIZA_EMITIDA', %s, 'EN_ENVIO', 1, now(),"
        " now() - interval '1 minute', %s, %s FROM dispositivo WHERE id = %s",
        (uuid4(), uuid4(), ahora, ahora, dispositivo_id),
    )
    await asyncio.gather(sistema.barrer(), sistema.barrer())
    await sistema.barrer()

    assert entregas(base) == {ENTREGADO: ("ENVIADO", 2, None)}
    assert len(simulador.envios(ENTREGADO)) == 1


@pytest.mark.parametrize(
    ("accion", "fallo"),
    [("baja", FALLO_DISPOSITIVO_DADO_DE_BAJA), ("reasignacion", FALLO_DISPOSITIVO_REASIGNADO)],
)
async def test_baja_o_reasignacion_con_entrega_pendiente_no_envia_al_dispositivo(
    sistema: Sistema,
    simulador: Simulador,
    nueva_cola: Callable[..., Any],
    base: str,
    sobre_poliza: Callable[..., dict[str, Any]],
    accion: str,
    fallo: str,
) -> None:
    cola = nueva_cola()
    cliente = uuid4()
    dispositivo_id = await sistema.registrar(cliente, CAIDO)
    cola.enviar(evento(sobre_poliza, cliente))
    await drenar(sistema.consumidor(cola), 1)
    assert entregas(base)[CAIDO][:2] == ("PENDIENTE", 1)
    enviados = len(simulador.envios(CAIDO))

    if accion == "baja":
        await sistema.dispositivos.dar_de_baja(dispositivo_id, cliente, datetime.now(UTC))
    else:
        await sistema.registrar(uuid4(), CAIDO)
    await asyncio.sleep(SETTINGS.entregas_espera_inicial_s + 0.2)
    assert await sistema.barrer() == 0

    assert entregas(base)[CAIDO][0] == "FALLIDO" and entregas(base)[CAIDO][2] == fallo
    assert len(simulador.envios(CAIDO)) == enviados
