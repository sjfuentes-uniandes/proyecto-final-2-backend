"""N3 R1 (HU-W04) contra un ambiente desplegado: la oferta del socio por el borde real.

Socio → WAF → API Gateway (token client_credentials, API key y plan de uso) → api-socios →
cotizacion → catalogo y clientes. El cliente es C01, que `clientes` siembra en `int`
(`CLIENTES_SEMILLA_SINTETICA`); otro con `E2E_W04_CLIENTE_ID`.

Los socios de Terraform (`socio-a`, `socio-b`) solo llegan a `/health`: api-socios autentica con
los socios que da de alta su administración (HU-W01) y además están en el plan `prueba` (5
solicitudes al día). Por eso la prueba da de alta dos socios con permiso `cotizar` en el plan
`basico` (como el N3 de HU-W01) y los desactiva al terminar.

- AC1/AC2: `POST /cotizaciones` → 201 con exactamente los campos del contrato v1, sin campos
  internos; `GET /cotizaciones/{id}` devuelve la misma oferta.
- AC3: el producto de QA `vida-hipotecario-qa-oferta-corta` (`CATALOGO_PRODUCTOS_QA` en
  `infra/envs/int/apps.tfvars`) crea ofertas de un minuto: vencida, `GET` → 410 OFERTA_VENCIDA con
  `detalles.accion = NUEVA_COTIZACION`.
- Seguridad: otro socio no lee la oferta (404 OFERTA_NO_ENCONTRADA, como una inexistente).

Configuración y precondiciones en infra/DESPLIEGUE.md (sección 7, "Pruebas E2E (N3)").
"""

import os
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

HISTORIA = "HU-W04 (R1)"
TIMEOUT_S = 30
CLIENTE = os.getenv("E2E_W04_CLIENTE_ID", "00000000-0000-4000-8000-000000000001")
PRODUCTO_QA = "vida-hipotecario-qa-oferta-corta"
CAMPOS_V1 = {
    "id",
    "codigo",
    "cotizacionId",
    "productoId",
    "precio",
    "moneda",
    "periodicidad",
    "coberturas",
    "exclusionesResumidas",
    "vigenteHasta",
    "estado",
    "version",
    "versionReglas",
    "creadaEn",
}
INTERNOS = {"huellaCalculo", "hashReglas", "entradas", "desglose", "perfilId", "socioId", "edad"}


def _solicitud(producto: str = "vida-hipotecario") -> dict[str, Any]:
    return {
        "clienteId": CLIENTE,
        "productoId": producto,
        # Datos sintéticos de C01 (contracts/datos-sinteticos).
        "datosAsegurado": {
            "fechaNacimiento": "1990-04-18",
            "plazoCreditoMeses": 180,
            "sumaAsegurada": 180000000,
        },
        "coberturasOpcionales": ["ENFERMEDADES_GRAVES"] if producto == "vida-hipotecario" else [],
    }


def _claves(valor: Any) -> set[str]:
    if isinstance(valor, dict):
        return set(valor) | {c for v in valor.values() for c in _claves(v)}
    if isinstance(valor, list):
        return {c for v in valor for c in _claves(v)}
    return set()


class Socio:
    """Un socio dado de alta por la administración, con su token y su API key."""

    def __init__(self, ambiente: Any, config: dict[str, str], datos: dict[str, Any]) -> None:
        self.id = datos["id"]
        self._base = config["socios"]
        token = ambiente.hasta(
            lambda: ambiente.token(config["token_url"], datos["clientId"], datos["clientSecret"]),
            200,
        )
        assert token.status_code == 200, token.text
        self.acceso = {
            "Authorization": f"Bearer {token.json()['access_token']}",
            "x-api-key": datos["apiKey"],
        }

    def cotizar(self, producto: str = "vida-hipotecario") -> httpx.Response:
        return httpx.post(
            f"{self._base}/cotizaciones",
            json=_solicitud(producto),
            headers={**self.acceso, "Idempotency-Key": str(uuid.uuid4())},
            timeout=TIMEOUT_S,
        )

    def consultar(self, cotizacion_id: str) -> httpx.Response:
        return httpx.get(
            f"{self._base}/cotizaciones/{cotizacion_id}", headers=self.acceso, timeout=TIMEOUT_S
        )


@pytest.fixture(scope="module")
def socios(ambiente: Any) -> Iterator[tuple[Socio, Socio]]:
    config = ambiente.requerir(HISTORIA, {"admin_token": ambiente.token_admin(), **ambiente.urls})
    admin = {"Authorization": f"Bearer {config['admin_token']}"}
    sello = time.strftime("%Y%m%d%H%M%S")
    altas: list[str] = []
    try:
        creados = []
        for letra in ("A", "B"):
            alta = httpx.post(
                f"{config['canales']}/web/socios",
                headers=admin,
                timeout=TIMEOUT_S,
                json={
                    "nombre": f"E2E HU-W04 {letra} {sello}",
                    "canal": "plataforma_digital",
                    "plan": "basico",
                    "permisos": ["cotizar"],
                },
            )
            assert alta.status_code == 201, alta.text
            altas.append(alta.json()["id"])
            creados.append(Socio(ambiente, config, alta.json()))
        # El borde y api-socios ven la credencial nueva de forma eventual.
        ambiente.hasta(
            lambda: httpx.get(
                f"{config['socios']}/socio/me", headers=creados[0].acceso, timeout=TIMEOUT_S
            ),
            200,
        )
        yield creados[0], creados[1]
    finally:
        for socio_id in altas:
            httpx.post(
                f"{config['canales']}/web/socios/{socio_id}/desactivar",
                headers=admin,
                timeout=TIMEOUT_S,
            )


def test_hu_w04_r1_oferta_con_token_y_api_key(socios: tuple[Socio, Socio]) -> None:
    socio, otro = socios
    respuesta = socio.cotizar()
    assert respuesta.status_code == 201, respuesta.text
    assert respuesta.headers.get("x-correlation-id")
    oferta = respuesta.json()
    # AC1 y AC2: todos los campos del contrato v1 y ninguno interno.
    assert set(oferta) == CAMPOS_V1
    assert not INTERNOS & _claves(oferta)
    assert oferta["estado"] == "VIGENTE" and oferta["moneda"] == "COP"
    assert oferta["codigo"].startswith("COT-") and oferta["coberturas"]
    assert oferta["exclusionesResumidas"], "las reglas vigentes (2026.10.1) traen exclusiones"
    hasta = datetime.fromisoformat(oferta["vigenteHasta"])
    assert hasta > datetime.now(UTC)
    leida = socio.consultar(oferta["cotizacionId"])
    assert (leida.status_code, leida.json()) == (200, oferta)
    # Seguridad: otro socio no la ve, igual que una inexistente.
    ajena = otro.consultar(oferta["cotizacionId"])
    inexistente = otro.consultar(str(uuid.uuid4()))
    assert (ajena.status_code, ajena.json()["codigo"]) == (404, "OFERTA_NO_ENCONTRADA")
    assert inexistente.json()["codigo"] == ajena.json()["codigo"]


def test_hu_w04_r1_oferta_vencida_es_410(socios: tuple[Socio, Socio]) -> None:
    socio, _ = socios
    respuesta = socio.cotizar(PRODUCTO_QA)
    if respuesta.status_code == 422 and respuesta.json()["codigo"] == "PRODUCTO_NO_ENCONTRADO":
        pytest.skip(f"{PRODUCTO_QA} no está publicado: falta CATALOGO_PRODUCTOS_QA en catalogo")
    assert respuesta.status_code == 201, respuesta.text
    oferta = respuesta.json()
    hasta = datetime.fromisoformat(oferta["vigenteHasta"])
    # Un minuto de vigencia: se espera a que venza (el reloj es el de cotizacion).
    time.sleep(max(0.0, (hasta - datetime.now(UTC)).total_seconds()) + 3)
    vencida = socio.consultar(oferta["cotizacionId"])
    assert vencida.status_code == 410, vencida.text
    cuerpo = vencida.json()
    assert cuerpo["codigo"] == "OFERTA_VENCIDA"
    assert cuerpo["detalles"]["accion"] == "NUEVA_COTIZACION"
    assert cuerpo["detalles"]["cotizacionId"] == oferta["cotizacionId"]
    assert oferta["precio"] not in vencida.text
