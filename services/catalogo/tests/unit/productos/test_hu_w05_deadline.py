"""N1 de HU-W05 en catalogo: producto y reglas vigentes respetan X-Deadline-Ms (cotizacion)."""

import asyncio
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

BASE = "/v1/productos"
TOLERANCIA_MS = 80


class ConsultaLenta:
    """ConsultarProductos que tarda más que cualquier deadline razonable."""

    async def producto(self, producto_id: str) -> Any:
        await asyncio.sleep(2)

    async def reglas_vigentes(self, producto_id: str, fecha: Any) -> Any:
        await asyncio.sleep(2)


@pytest.mark.parametrize("ruta", ["/vida-hipotecario", "/vida-hipotecario/reglas-prima/vigente"])
def test_con_deadline_corta_y_responde_504(api: TestClient, ruta: str) -> None:
    api.app.state.consultar_productos = ConsultaLenta()  # type: ignore[attr-defined]
    inicio = time.perf_counter()
    respuesta = api.get(f"{BASE}{ruta}", headers={"X-Deadline-Ms": "120"})
    duracion_ms = (time.perf_counter() - inicio) * 1000
    assert respuesta.status_code == 504, respuesta.text
    assert respuesta.json()["codigo"] == "TIEMPO_AGOTADO"
    # timeout_efectivo_ms(500, 120) = 100: deadline menos el margen de 20 ms.
    assert duracion_ms <= 100 + TOLERANCIA_MS, duracion_ms


def test_sin_deadline_corta_en_el_tope_configurado(api: TestClient) -> None:
    api.app.state.consultar_productos = ConsultaLenta()  # type: ignore[attr-defined]
    api.app.state.consulta_timeout_ms = 150  # type: ignore[attr-defined]
    inicio = time.perf_counter()
    respuesta = api.get(f"{BASE}/vida-hipotecario")
    assert respuesta.status_code == 504
    assert (time.perf_counter() - inicio) * 1000 <= 150 + TOLERANCIA_MS


def test_con_deadline_holgado_responde_normalmente(api: TestClient) -> None:
    respuesta = api.get(f"{BASE}/vida-hipotecario", headers={"X-Deadline-Ms": "600"})
    assert respuesta.status_code == 200
    assert respuesta.json()["id"] == "vida-hipotecario"


@pytest.mark.parametrize("valor", ["0", "abc"])
def test_deadline_invalido_es_422(api: TestClient, valor: str) -> None:
    respuesta = api.get(f"{BASE}/vida-hipotecario", headers={"X-Deadline-Ms": valor})
    assert respuesta.status_code == 422
