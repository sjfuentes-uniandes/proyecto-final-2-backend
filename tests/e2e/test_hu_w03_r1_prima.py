"""N3 R1 (HU-W03) contra un ambiente desplegado: de la solicitud a la prima persistida.

Por el borde real: el portal (`/web`, pool del back-office → `bff-web` → `cotizacion` →
`catalogo` y `clientes`). Usa el usuario E2E del back-office, que debe estar en el grupo
`asesores`, y el cliente sintético C01 que `clientes` siembra en `int`
(`CLIENTES_SEMILLA_SINTETICA`); otro cliente con `E2E_W03_CLIENTE_ID`.

- AC1: la prima sale en la moneda del producto, con la versión de las reglas, y queda persistida
  (la lectura por id la devuelve igual y repetir la clave responde 200 con la misma cotización).
- AC2: el producto vencido de la semilla (`vida-hipotecario-2025`) es un fallo controlado:
  422 PRODUCTO_FUERA_DE_VIGENCIA, también en las condiciones del formulario.
- AC3: la misma solicitud con otra clave da la misma prima, el mismo desglose y la misma huella.

Sin acceso a las bases: la fila y la caché no se ven aquí (las cubre la versión de módulo,
`services/cotizacion/tests/module/test_cotizacion_w03.py`).

Configuración y precondiciones en infra/DESPLIEGUE.md (sección 7, "Pruebas E2E (N3)").
"""

import os
import time
import uuid
from decimal import Decimal
from typing import Any

import httpx
import pytest

HISTORIA = "HU-W03 (R1)"
TIMEOUT_S = 30
CLIENTE = os.getenv("E2E_W03_CLIENTE_ID", "00000000-0000-4000-8000-000000000001")
# Datos declarados sintéticos (los de C01 en contracts/datos-sinteticos).
DATOS = {"fechaNacimiento": "1990-04-18", "plazoCreditoMeses": 180, "sumaAsegurada": 180000000}


class Portal:
    """El portal, por `/web`, con el usuario E2E del back-office (grupo `asesores`)."""

    def __init__(self, base: str, token: str) -> None:
        self._base = f"{base}/web/v1"
        self._cabeceras = {"Authorization": f"Bearer {token}"}

    def cotizar(self, producto: str = "vida-hipotecario", clave: str = "") -> httpx.Response:
        cuerpo = {
            "clienteId": CLIENTE,
            "productoId": producto,
            "datosAsegurado": DATOS,
            "coberturasOpcionales": ["ENFERMEDADES_GRAVES"],
        }
        respuesta = httpx.post(
            f"{self._base}/cotizaciones",
            json=cuerpo,
            headers={**self._cabeceras, "Idempotency-Key": clave or str(uuid.uuid4())},
            timeout=TIMEOUT_S,
        )
        assert respuesta.status_code != 403, (
            "el usuario E2E del back-office debe estar en el grupo asesores (infra/DESPLIEGUE.md)"
        )
        return respuesta

    def get(self, ruta: str) -> httpx.Response:
        return httpx.get(f"{self._base}{ruta}", headers=self._cabeceras, timeout=TIMEOUT_S)


@pytest.fixture(scope="module")
def portal(ambiente: Any) -> Portal:
    config = ambiente.requerir(
        HISTORIA, {"canales": ambiente.urls["canales"], "admin_token": ambiente.token_admin()}
    )
    return Portal(config["canales"], config["admin_token"])


def test_hu_w03_r1_sin_token_el_borde_responde_401(ambiente: Any) -> None:
    config = ambiente.requerir(HISTORIA, {"canales": ambiente.urls["canales"]})
    respuesta = httpx.post(f"{config['canales']}/web/v1/cotizaciones", json={}, timeout=TIMEOUT_S)
    assert respuesta.status_code == 401


def test_hu_w03_r1_condiciones_del_producto(portal: Portal) -> None:
    respuesta = portal.get("/productos/vida-hipotecario/condiciones")
    assert respuesta.status_code == 200, respuesta.text
    condiciones = respuesta.json()
    assert condiciones["moneda"] == "COP"
    opcionales = {c["codigo"] for c in condiciones["coberturas"] if not c["obligatoria"]}
    assert "ENFERMEDADES_GRAVES" in opcionales


def test_hu_w03_r1_solicitud_hasta_prima_persistida(portal: Portal) -> None:
    clave = str(uuid.uuid4())
    inicio = time.perf_counter()
    respuesta = portal.cotizar(clave=clave)
    transcurrido_ms = (time.perf_counter() - inicio) * 1000

    assert respuesta.status_code == 201, respuesta.text
    cotizacion = respuesta.json()
    # AC1: moneda del producto, versión de reglas conservada y desglose que cuadra.
    assert cotizacion["moneda"] == "COP" and cotizacion["versionReglas"]
    assert cotizacion["estado"] == "CALCULADA"
    desglose = cotizacion["desglose"]
    recargos = sum(Decimal(r["valor"]) for r in desglose["recargos"])
    assert Decimal(desglose["subtotal"]) == Decimal(desglose["primaAjustada"]) + recargos
    assert desglose["primaTotal"] == cotizacion["primaTotal"]
    assert "fechaNacimiento" not in respuesta.text
    print(f"\nHU-W03 R1: {transcurrido_ms:.0f} ms de punta a punta; pasos {cotizacion['pasos']}")

    # Persistida: la lectura la devuelve igual y repetir la clave no crea otra.
    leida = portal.get(f"/cotizaciones/{cotizacion['id']}")
    assert leida.status_code == 200, leida.text
    assert leida.json()["desglose"] == desglose
    repetida = portal.cotizar(clave=clave)
    assert (repetida.status_code, repetida.json()["id"]) == (200, cotizacion["id"])

    # AC3: otra clave, misma entrada y versión → mismo resultado.
    otra = portal.cotizar()
    assert otra.status_code == 201, otra.text
    assert otra.json()["id"] != cotizacion["id"]
    if otra.json()["versionReglas"] == cotizacion["versionReglas"]:
        assert otra.json()["desglose"] == desglose
        assert otra.json()["huellaCalculo"] == cotizacion["huellaCalculo"]


def test_hu_w03_r1_producto_vencido_es_un_fallo_controlado(portal: Portal) -> None:
    respuesta = portal.cotizar("vida-hipotecario-2025")
    assert respuesta.status_code == 422, respuesta.text
    assert respuesta.json()["codigo"] == "PRODUCTO_FUERA_DE_VIGENCIA"
    assert respuesta.json()["correlationId"]
    condiciones = portal.get("/productos/vida-hipotecario-2025/condiciones")
    assert condiciones.json()["codigo"] == "PRODUCTO_FUERA_DE_VIGENCIA"
