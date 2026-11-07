"""HU-W24 (N1): arranque y parada del consumidor de `auditoria`."""

import asyncio
import io
import json
from collections.abc import Callable
from typing import Any

import pytest
from auditoria import main
from auditoria.config import Settings
from fastapi.testclient import TestClient


def _settings(**valores: Any) -> Settings:
    return Settings(**valores)


def test_sin_cola_ni_bucket_arranca_sin_consumidor() -> None:
    with TestClient(main.crear_app(_settings())) as cliente:
        assert cliente.get("/health").json() == {"status": "ok"}
        assert not hasattr(cliente.app.state, "consumidor")  # type: ignore[attr-defined]


def test_sin_base_lo_registra_y_arranca(capturar_logs: Callable[..., io.StringIO]) -> None:
    settings = _settings(
        SQS_AUDITORIA_URL="https://sqs.us-east-1.amazonaws.com/0/auditoria",
        AUDIT_BUCKET="solventa-int-auditoria-0",
    )
    app = main.crear_app(settings)
    salida = capturar_logs()  # después de instrumentar, que configura el logging

    with TestClient(app) as cliente:
        assert cliente.get("/health").status_code == 200
    registros = [json.loads(linea) for linea in salida.getvalue().splitlines() if linea.strip()]
    assert [(r["level"], r["message"]) for r in registros if r["level"] == "ERROR"] == [
        ("ERROR", main.MENSAJE_CONSUMIDOR_SIN_CONFIGURACION)
    ]


class ConsumidorFalso:
    def __init__(self) -> None:
        self.parado = False

    async def ejecutar(self, parada: asyncio.Event) -> None:
        await parada.wait()
        self.parado = True


def test_con_cola_bucket_y_base_corre_el_consumidor_y_lo_para_al_cerrar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    falso = ConsumidorFalso()
    armado: dict[str, Any] = {}

    def armar(pool: Any, receptor: Any, almacen: Any, reloj: Any, lote: int) -> ConsumidorFalso:
        armado.update(receptor=receptor, almacen=almacen, lote=lote)
        return falso

    monkeypatch.setattr(main, "armar_consumidor", armar)
    settings = _settings(
        SQS_AUDITORIA_URL="https://sqs.us-east-1.amazonaws.com/0/auditoria",
        AUDIT_BUCKET="solventa-int-auditoria-0",
    )

    with TestClient(main.crear_app(settings, pool=object())):  # type: ignore[arg-type]
        assert not falso.parado
    assert falso.parado
    assert armado["lote"] == 10
    assert armado["receptor"].max_mensajes == 10
    assert armado["receptor"].espera_larga_s == 20
