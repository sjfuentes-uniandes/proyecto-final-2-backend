"""N3 R3 (HU-W13 a HU-W15) contra un ambiente desplegado: de la cotización a la aceptación.

Por el borde real: el portal (`/web`, pool del back-office → `bff-web` → `polizas` → `cotizacion`
y `catalogo`). Usa el usuario E2E del back-office, que debe estar en el grupo `asesores`, y los
clientes sintéticos que `clientes` siembra en `int` (`CLIENTES_SEMILLA_SINTETICA`): C01 (S01,
aprobada) y C04 (S02, 68 años, rechazada). Los datos son los de
`contracts/datos-sinteticos/cotizaciones-suscripcion.json`.

- Feliz (W13 AC1, W14 AC1, W15 AC1 y AC3): perfil → cotización → decisión APROBADA con su
  comunicación (`admiteContratacion`) → aceptación 201 con el recaudo habilitado → la misma clave
  responde 200 con la misma aceptación y la lectura la devuelve igual.
- Fallo controlado (W13 AC2, W14 AC2): la decisión RECHAZADA no admite contratación y un POST
  directo de aceptación responde 409 DECISION_NO_APROBADA.

Requiere EXT-01 (`polizas.calls` += `cotizacion`, `catalogo`) y EXT-02 en `int` (plan del
Sprint 2, R3-06); sin configuración se omite como las demás N3. Sin acceso a las bases: las filas
de `aceptaciones` y `outbox` las cubre MT-W15-01 (`services/polizas/tests/module`).

Configuración y precondiciones en infra/DESPLIEGUE.md (sección 7, "Pruebas E2E (N3)").
"""

import uuid
from typing import Any

import httpx
import pytest

HISTORIA = "HU-W13 a HU-W15 (R3)"
TIMEOUT_S = 30
VERSION_DECLARACIONES = "2026.10.1"
DECLARACIONES = {
    "leidoCondiciones": True,
    "informacionVeraz": True,
    "debitoAutomatico": True,
    "avisosElectronicos": True,
}
# Casos sintéticos S01 (C01, 36 años) y S02 (C04, 68 años, CF-S2-09).
CASOS = {
    "S01": ("00000000-0000-4000-8000-000000000001", "1990-04-18", 180000000),
    "S02": ("00000000-0000-4000-8000-000000000004", "1958-03-10", 90000000),
}


class Portal:
    """El portal, por `/web`, con el usuario E2E del back-office (grupo `asesores`)."""

    def __init__(self, base: str, token: str) -> None:
        self._base = f"{base}/web/v1"
        self._cabeceras = {"Authorization": f"Bearer {token}"}

    def post(self, ruta: str, cuerpo: dict[str, Any], clave: str = "") -> httpx.Response:
        respuesta = httpx.post(
            f"{self._base}{ruta}",
            json=cuerpo,
            headers={**self._cabeceras, "Idempotency-Key": clave or str(uuid.uuid4())},
            timeout=TIMEOUT_S,
        )
        assert respuesta.status_code != 403 or "IDENTIDAD" in respuesta.text, (
            "el usuario E2E del back-office debe estar en el grupo asesores (infra/DESPLIEGUE.md)"
        )
        return respuesta

    def get(self, ruta: str) -> httpx.Response:
        return httpx.get(f"{self._base}{ruta}", headers=self._cabeceras, timeout=TIMEOUT_S)

    def decidir(self, caso: str) -> dict[str, Any]:
        """Perfil, cotización y decisión de suscripción del caso sintético."""
        cliente, nacimiento, suma = CASOS[caso]
        datos = {"fechaNacimiento": nacimiento, "plazoCreditoMeses": 180, "sumaAsegurada": suma}
        perfil = self.post(
            "/perfiles",
            {"clienteId": cliente, "productoId": "vida-hipotecario", "datosDeclarados": datos},
        )
        assert perfil.status_code in (200, 201), perfil.text
        cotizacion = self.post(
            "/cotizaciones",
            {
                "clienteId": cliente,
                "productoId": "vida-hipotecario",
                "datosAsegurado": datos,
                "perfilId": perfil.json()["id"],
            },
        )
        assert cotizacion.status_code == 201, cotizacion.text
        decision = self.post("/suscripciones", {"cotizacionId": cotizacion.json()["id"]})
        assert decision.status_code == 201, decision.text
        cuerpo: dict[str, Any] = decision.json()
        return cuerpo

    def aceptar(self, decision: dict[str, Any], clave: str = "") -> httpx.Response:
        cuerpo = {
            "decisionId": decision["id"],
            "versionDeclaraciones": VERSION_DECLARACIONES,
            "declaraciones": DECLARACIONES,
        }
        return self.post(f"/cotizaciones/{decision['cotizacionId']}/aceptacion", cuerpo, clave)


@pytest.fixture(scope="module")
def portal(ambiente: Any) -> Portal:
    config = ambiente.requerir(
        HISTORIA, {"canales": ambiente.urls["canales"], "admin_token": ambiente.token_admin()}
    )
    return Portal(config["canales"], config["admin_token"])


def test_hu_w15_r3_sin_token_el_borde_responde_401(ambiente: Any) -> None:
    config = ambiente.requerir(HISTORIA, {"canales": ambiente.urls["canales"]})
    respuesta = httpx.post(
        f"{config['canales']}/web/v1/cotizaciones/{uuid.uuid4()}/aceptacion",
        json={},
        timeout=TIMEOUT_S,
    )
    assert respuesta.status_code == 401


def test_hu_w13_w15_r3_aprobada_hasta_la_aceptacion(portal: Portal) -> None:
    decision = portal.decidir("S01")
    assert decision["resultado"] == "APROBADA", decision.get("reglasEvaluadas")
    comunicacion = decision["comunicacion"]
    assert (comunicacion["condicion"], comunicacion["admiteContratacion"]) == (
        "APROBADA_SIN_AJUSTES",
        True,
    )

    clave = str(uuid.uuid4())
    respuesta = portal.aceptar(decision, clave)
    assert respuesta.status_code == 201, respuesta.text
    aceptacion = respuesta.json()
    assert (aceptacion["recaudoHabilitado"], aceptacion["siguientePaso"]) == (True, "FIRMA")
    assert (aceptacion["cotizacionId"], aceptacion["decisionId"]) == (
        decision["cotizacionId"],
        decision["id"],
    )
    assert aceptacion["huellaCalculo"] == decision["condiciones"]["huellaCalculo"]
    assert respuesta.headers.get("X-Correlation-Id")
    print(f"\nHU-W15 R3: aceptación {aceptacion['id']} de la decisión {decision['codigo']}")

    # AC3: la misma clave devuelve la misma aceptación; otra clave, 409 con su identificador.
    repetida = portal.aceptar(decision, clave)
    assert (repetida.status_code, repetida.json()) == (200, aceptacion)
    otra = portal.aceptar(decision)
    assert (otra.status_code, otra.json()["codigo"]) == (409, "OFERTA_YA_ACEPTADA")
    assert otra.json()["detalles"] == {"aceptacionId": aceptacion["id"]}
    leida = portal.get(f"/cotizaciones/{decision['cotizacionId']}/aceptacion")
    assert (leida.status_code, leida.json()) == (200, aceptacion)


def test_hu_w13_w15_r3_rechazada_no_se_acepta(portal: Portal) -> None:
    decision = portal.decidir("S02")
    assert (decision["resultado"], decision["reglaDeterminante"]) == ("RECHAZADA", "SUS-001")
    assert decision["comunicacion"]["admiteContratacion"] is False

    respuesta = portal.aceptar(decision)
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (409, "DECISION_NO_APROBADA")
    assert respuesta.json()["correlationId"]
    sin_aceptacion = portal.get(f"/cotizaciones/{decision['cotizacionId']}/aceptacion")
    assert sin_aceptacion.json()["codigo"] == "ACEPTACION_NO_ENCONTRADA"
