"""`siniestros` verifica como proveedor el pacto de `pagos-parametricos` (HU-W22, CT-W22-01, W22-B04).

La app real sobre PostgreSQL real. Cada interacción pide un estado del proveedor:

- «no existe un siniestro para ese evento y esa póliza»: la base queda vacía y la creación responde
  201 con el siniestro nuevo (su `id` y su `codigo` los genera el servicio).
- «ya existe el siniestro paramétrico de ese evento y esa póliza»: se siembra con el `id` y el
  `codigo` exactos del pacto, como lo dejó la primera solicitud; la repetición responde 200 con el
  **mismo** siniestro y la misma clave con otro cuerpo, 422 `IDEMPOTENCIA_CONFLICTO`.

El pacto no se edita aquí: lo genera `pagos-parametricos` (`make test-contract
SERVICE=pagos-parametricos`; hasta B3, `docs/sprint_3/evidencias/hu-w22/prototipos/generar_pacto.py`).
"""

import asyncio
import json
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
import pytest
import uvicorn
from fastapi import FastAPI
from pact import Verifier
from siniestros.adapters.outbound.siniestros_pg import SiniestrosPg
from siniestros.application.siniestro.crear_parametrico import PREFIJO_CLAVE, huella_de
from siniestros.config import Settings
from siniestros.domain.siniestro.modelos import EventoParametrico, Vigencia
from siniestros.domain.siniestro.parametrico import DatosParametrico, nuevo_siniestro
from siniestros.main import crear_app

PACTO = Path(__file__).parents[4] / "contracts" / "pact" / "pagos-parametricos-siniestros.json"
SIN_SINIESTRO = "no existe un siniestro para ese evento y esa póliza"
CON_SINIESTRO = "ya existe el siniestro paramétrico de ese evento y esa póliza"
SINIESTRO_ID = UUID("5a000000-0000-4000-8000-000000000022")
CODIGO = "88-2026-1001"
TABLAS = "borrador_evidencias, borradores, siniestros, evidencias, historial_siniestro, decisiones_siniestro, outbox, inbox, idempotencia, secuencia_codigo"


@contextmanager
def _servir(app: FastAPI) -> Iterator[str]:
    zocalo = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    zocalo.bind(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{zocalo.getsockname()[1]}"
    servidor = uvicorn.Server(uvicorn.Config(app, log_level="warning"))

    def correr() -> None:
        asyncio.run(servidor.serve(sockets=[zocalo]), loop_factory=asyncio.SelectorEventLoop)

    hilo = threading.Thread(target=correr, daemon=True)
    hilo.start()
    limite = time.monotonic() + 30
    while not servidor.started:
        assert hilo.is_alive(), "siniestros no arrancó (ver el log del servidor)"
        assert time.monotonic() < limite, "siniestros no arrancó a tiempo"
        time.sleep(0.05)
    try:
        yield url
    finally:
        servidor.should_exit = True
        hilo.join(timeout=15)


def _datos_del_pacto() -> DatosParametrico:
    """El cuerpo que el pacto describe, tal como lo guardó la primera solicitud."""
    cuerpo: dict[str, Any] = json.loads(PACTO.read_text(encoding="utf-8"))["interactions"][0][
        "request"
    ]["body"]["content"]
    e = cuerpo["evento"]
    return DatosParametrico(
        evento=EventoParametrico(
            evento_id=UUID(cuerpo["eventoId"]),
            fuente=e["fuente"],
            tipo_evento=e["tipoEvento"],
            valor_observado=e["valorObservado"],
            umbral=e["umbral"],
            unidad=e["unidad"],
            zona=e["zona"],
            observado_en=datetime.fromisoformat(e["observadoEn"]),
            evento_origen=e["eventoOrigen"],
            regla=cuerpo["regla"],
            version_regla=cuerpo["versionRegla"],
        ),
        poliza_id=UUID(cuerpo["polizaId"]),
        cliente_id=UUID(cuerpo["clienteId"]),
        numero=cuerpo["poliza"]["numero"],
        ramo=cuerpo["poliza"]["ramo"],
        vigencia=Vigencia(
            datetime.fromisoformat(cuerpo["poliza"]["vigencia"]["desde"]).date(),
            datetime.fromisoformat(cuerpo["poliza"]["vigencia"]["hasta"]).date(),
        ),
        moneda=cuerpo["poliza"]["moneda"],
        valor=Decimal(cuerpo["valor"]),
    )


def _sembrar_el_siniestro_existente(base: str) -> None:
    """Lo que dejó la primera solicitud: el siniestro (con el `id` y el `codigo` del pacto) y la
    clave de idempotencia con la huella del cuerpo original."""
    from psycopg.types.json import Jsonb

    datos = _datos_del_pacto()
    ahora = datetime(2026, 11, 10, 14, 5, tzinfo=UTC)
    caso = nuevo_siniestro(
        datos, siniestro_id=SINIESTRO_ID, codigo=CODIGO, ahora=ahora, correlation_id="corr-pacto"
    )

    async def sembrar() -> None:
        from psycopg_pool import AsyncConnectionPool

        async with (
            AsyncConnectionPool(base, min_size=1, max_size=2, open=False) as pool,
            SiniestrosPg(pool).transaccion() as tx,
        ):
            assert await tx.insertar_parametrico(caso)  # type: ignore[attr-defined]

    asyncio.run(sembrar(), loop_factory=asyncio.SelectorEventLoop)
    with psycopg.connect(base, autocommit=True) as conexion:
        conexion.execute(
            "INSERT INTO idempotencia (clave, huella_solicitud, respuesta, creada_en) "
            "VALUES (%s, %s, %s, %s)",
            (
                f"{PREFIJO_CLAVE}{datos.clave}",
                huella_de(datos),
                Jsonb({"siniestroId": str(SINIESTRO_ID)}),
                ahora,
            ),
        )


def test_siniestros_cumple_el_pacto_de_pagos_parametricos(
    limpia: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert PACTO.exists(), "Falta contracts/pact/pagos-parametricos-siniestros.json (B1 lo genera)"
    monkeypatch.setenv("SOLVENTA_DATABASE_URL", limpia)
    vistos: list[str] = []

    def estado(state: str, action: str, parameters: dict[str, Any] | None = None) -> None:
        """Un estado por interacción; uno desconocido falla la verificación."""
        if action != "setup":
            return
        with psycopg.connect(limpia, autocommit=True) as conexion:
            conexion.execute(f"TRUNCATE {TABLAS}")
        if state == CON_SINIESTRO:
            _sembrar_el_siniestro_existente(limpia)
        elif state != SIN_SINIESTRO:
            raise AssertionError(f"estado del proveedor sin simular: {state!r}")
        vistos.append(state)

    with _servir(crear_app(Settings())) as url:
        Verifier("siniestros", host="127.0.0.1").add_source(PACTO).add_transport(
            url=url
        ).state_handler(estado, teardown=False).verify()

    assert sorted(vistos) == sorted([SIN_SINIESTRO, CON_SINIESTRO, CON_SINIESTRO])
    # La creación del pacto dejó un siniestro nuevo; la repetición y el conflicto, ninguno más.
    with psycopg.connect(limpia, autocommit=True) as conexion:
        (cuantos,) = conexion.execute("SELECT count(*) FROM siniestros").fetchone() or (0,)
    assert cuantos == 1
