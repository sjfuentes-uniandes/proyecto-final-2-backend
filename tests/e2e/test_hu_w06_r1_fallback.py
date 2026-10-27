"""N3 R1 (HU-W06) contra un ambiente desplegado: respuesta degradada permitida.

Por el borde real del portal (`/web`, usuario E2E del back-office en el grupo `asesores`), con los
clientes sintéticos que `clientes` siembra en `int` y el simulador de aliados
(contracts/datos-sinteticos/clientes-perfilamiento.json):

1. Nominal: C01 (todas las fuentes responden) → oferta definitiva, `degradada: false`, sin
   faltantes.
2. Degradado: C07 (`FA_INGRESOS_AGREGADOS` caída, opcional) → oferta provisional con
   `degradada: true`; al consultar la cotización, el dato faltante, el motivo y la regla aplicada
   (FB-003 de las reglas de fallback 2026.10.1 de catalogo) (AC1, AC3).
3. Pendiente: C13 (`FA_HISTORIAL_PAGOS_12M` lenta, obligatoria) → 503 DEPENDENCIA_NO_DISPONIBLE sin
   oferta, con `accionSugerida: RECALCULAR_PERFIL` y la decisión (AC2).

Sin carga. Configuración y precondiciones en infra/DESPLIEGUE.md (sección 7, "Pruebas E2E (N3)").
"""

import os
import time
import uuid
from typing import Any

import httpx
import pytest

HISTORIA = "HU-W06 (R1)"
TIMEOUT_S = 30
CLIENTE_NOMINAL = os.getenv("E2E_W06_CLIENTE_NOMINAL", "00000000-0000-4000-8000-000000000001")
CLIENTE_DEGRADADO = os.getenv("E2E_W06_CLIENTE_DEGRADADO", "00000000-0000-4000-8000-000000000007")
CLIENTE_PENDIENTE = os.getenv("E2E_W06_CLIENTE_PENDIENTE", "00000000-0000-4000-8000-00000000000d")
# Datos declarados de C01, C07 y C13 (los mismos en los datos sintéticos).
DATOS = {"fechaNacimiento": "1990-04-18", "plazoCreditoMeses": 180, "sumaAsegurada": 180000000}
VERSION_REGLAS_FALLBACK = os.getenv("E2E_W06_VERSION_REGLAS", "2026.10.1")
FALTANTE_C07 = {
    "fuente": "FA_INGRESOS_AGREGADOS",
    "dato": "ingreso_mensual_estimado",
    "motivo": "NO_DISPONIBLE",
    "regla": {"id": "FB-003", "version": VERSION_REGLAS_FALLBACK},
}


class Portal:
    def __init__(self, base: str, token: str) -> None:
        self._base = f"{base}/web/v1"
        self._cabeceras = {"Authorization": f"Bearer {token}"}

    def post(self, ruta: str, cuerpo: dict[str, Any], correlacion: str) -> httpx.Response:
        respuesta = httpx.post(
            f"{self._base}{ruta}",
            json=cuerpo,
            headers={
                **self._cabeceras,
                "Idempotency-Key": str(uuid.uuid4()),
                "X-Correlation-Id": correlacion,
            },
            timeout=TIMEOUT_S,
        )
        assert respuesta.status_code != 403, "el usuario E2E debe estar en el grupo asesores"
        return respuesta

    def get(self, ruta: str) -> httpx.Response:
        return httpx.get(f"{self._base}{ruta}", headers=self._cabeceras, timeout=TIMEOUT_S)


@pytest.fixture(scope="module")
def portal(ambiente: Any) -> Portal:
    config = ambiente.requerir(
        HISTORIA, {"canales": ambiente.urls["canales"], "admin_token": ambiente.token_admin()}
    )
    return Portal(config["canales"], config["admin_token"])


def _recorrido(portal: Portal, cliente: str) -> dict[str, Any]:
    """Perfil del cliente y la cotización con ese perfil, con una sola correlación."""
    correlacion = f"e2e-hu-w06-{uuid.uuid4().hex[:12]}"
    perfil = portal.post(
        "/perfiles",
        {"clienteId": cliente, "productoId": "vida-hipotecario", "datosDeclarados": DATOS},
        correlacion,
    )
    assert perfil.status_code == 201, perfil.text
    inicio = time.perf_counter()
    cotizacion = portal.post(
        "/cotizaciones",
        {
            "clienteId": cliente,
            "productoId": "vida-hipotecario",
            "perfilId": perfil.json()["id"],
            "datosAsegurado": DATOS,
        },
        correlacion,
    )
    ms = (time.perf_counter() - inicio) * 1000
    print(f"\n{HISTORIA} {correlacion}: cotización {cotizacion.status_code} en {ms:.0f} ms")
    return {"correlacion": correlacion, "perfil": perfil.json(), "cotizacion": cotizacion}


@pytest.fixture(scope="module")
def nominal(portal: Portal) -> dict[str, Any]:
    return _recorrido(portal, CLIENTE_NOMINAL)


@pytest.fixture(scope="module")
def degradado(portal: Portal) -> dict[str, Any]:
    return _recorrido(portal, CLIENTE_DEGRADADO)


def test_hu_w06_r1_recorrido_nominal_sin_degradar(portal: Portal, nominal: dict[str, Any]) -> None:
    assert nominal["perfil"]["estado"] == "CALCULADO"
    respuesta = nominal["cotizacion"]
    assert respuesta.status_code == 201, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["oferta"] is not None
    assert (cuerpo["degradada"], cuerpo["faltantes"], cuerpo["versionReglasFallback"]) == (
        False,
        [],
        None,
    )
    leida = portal.get(f"/cotizaciones/{cuerpo['id']}").json()
    assert (leida["degradada"], leida["faltantes"]) == (False, [])


def test_hu_w06_r1_ac1_ac3_fuente_opcional_caida_da_oferta_degradada(
    portal: Portal, degradado: dict[str, Any]
) -> None:
    perfil = degradado["perfil"]
    assert perfil["estado"] == "DEGRADADO"
    estados = {f["fuente"]: f["estado"] for f in perfil["fuentes"]}
    assert estados["FA_INGRESOS_AGREGADOS"] == "NO_DISPONIBLE"
    respuesta = degradado["cotizacion"]
    assert respuesta.status_code == 201, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["oferta"] is not None and cuerpo["degradada"] is True
    assert cuerpo["faltantes"] == [FALTANTE_C07]
    assert cuerpo["versionReglasFallback"] == VERSION_REGLAS_FALLBACK
    # AC3: la decisión y la regla aplicada se consultan después, igual.
    leida = portal.get(f"/cotizaciones/{cuerpo['id']}")
    assert leida.status_code == 200, leida.text
    assert (leida.json()["degradada"], leida.json()["faltantes"]) == (True, [FALTANTE_C07])
    # Nunca un valor inventado: el dato faltante no aparece con ningún valor.
    assert "6500000" not in respuesta.text and "ingresoMensualEstimado" not in respuesta.text


def test_hu_w06_r1_ac2_fuente_obligatoria_lenta_queda_pendiente(portal: Portal) -> None:
    pendiente = _recorrido(portal, CLIENTE_PENDIENTE)
    assert pendiente["perfil"]["estado"] == "INFORMACION_INSUFICIENTE"
    respuesta = pendiente["cotizacion"]
    assert respuesta.status_code == 503, respuesta.text
    error = respuesta.json()
    assert error["codigo"] == "DEPENDENCIA_NO_DISPONIBLE"
    detalles = error["detalles"]
    assert (detalles["dependencia"], detalles["accionSugerida"]) == (
        "perfil.fuentes",
        "RECALCULAR_PERFIL",
    )
    assert [(f["fuente"], f["motivo"], f["regla"]["id"]) for f in detalles["faltantes"]] == [
        ("FA_HISTORIAL_PAGOS_12M", "TIEMPO_AGOTADO", "FB-002")
    ]
    assert uuid.UUID(detalles["decisionId"])
