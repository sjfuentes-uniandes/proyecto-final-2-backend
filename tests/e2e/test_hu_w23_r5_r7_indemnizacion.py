"""N3 R7 (HU-W23) contra un ambiente desplegado: de la decisión del analista al estado final del pago.

Recorre por el portal (`/web`, usuario E2E del back-office en el grupo `analistas-siniestros`) lo que
la versión de módulo y el recorrido local de B4 ya verificaron con las piezas reales: el analista
aprueba un siniestro EN_EVALUACION, `siniestros` publica `SiniestroAprobado`, `dinero` paga **una**
vez por `adaptador-pagos` (el simulador) y `siniestros` refleja el resultado, que `GET
/v1/siniestros/{id}/pago` (H08) y el detalle (paso «Pago de la indemnización») muestran.

- C01: aprobar el caso → el pago llega a `CONFIRMADO` con una sola transacción (`intentos` 1) → el
  detalle completa el paso del pago y trae la entrada `PAGO_CONFIRMADO` en el historial.
- C02: repetir la decisión con la misma `Idempotency-Key` devuelve el mismo resultado y nada cambia
  en el pago (cero pagos duplicados); otra clave sobre el caso ya aprobado es 409
  `TRANSICION_NO_PERMITIDA`.
- C03: un siniestro que no está aprobado no tiene pago: 404 `INDEMNIZACION_NO_ENCONTRADA`.

El tramo R5 (el siniestro nace aprobado por un evento paramétrico) lo agrega HU-W22. El rechazo
de la pasarela, la pasarela lenta con su conciliación y los eventos duplicados o concurrentes solo se
verifican a nivel de módulo y en el recorrido local de B4: necesitan beneficiarios y escenarios del
simulador que un ambiente compartido no debe consumir (cada caso solo se puede aprobar una vez) y ver
las filas, la cola y el journal de WireMock, a los que esta prueba no tiene acceso.

Requiere el usuario E2E del back-office en `analistas-siniestros` y la semilla sintética de QA de
`siniestros` (HU-W19) con ese usuario como analista (`SEMILLA_SINIESTROS_ANALISTA_ID`); el caso es el
de `E2E_W23_SINIESTRO_ID` o, si falta, el primero EN_EVALUACION de «Mi bandeja». **Aprueba un caso de
verdad**: cada ejecución consume uno de la semilla. Espera al pago `E2E_PAGO_ESPERA_S` (por defecto,
120 s: el relay del Outbox y los dos consumidores). Sin configuración se omite como las demás N3.
Configuración y precondiciones en infra/DESPLIEGUE.md (sección 7, "Pruebas E2E (N3)").
"""

import os
import re
import time
import uuid
from typing import Any

import httpx
import pytest

HISTORIA = "HU-W23 (R7)"
TIMEOUT_S = 30
ESPERA_PAGO_S = int(os.getenv("E2E_PAGO_ESPERA_S", "120"))
JUSTIFICACION = "Daño accidental verificado con la evidencia del caso."
LIQUIDACION = {
    "valorReclamado": "2850000",
    "deducible": "350000",
    "valorIndemnizar": "2500000",
    "moneda": "COP",
}
REFERENCIA = re.compile(r"^IND-88-\d{4}-\d{4,}-01$")
# La clave de la decisión de C01, para que C02 la repita.
CLAVES: dict[str, str] = {}


class Portal:
    """El portal, por `/web`, con el usuario E2E del back-office (grupo `analistas-siniestros`)."""

    def __init__(self, base: str, token: str) -> None:
        self._base = f"{base}/web/v1/siniestros"
        self._cabeceras = {"Authorization": f"Bearer {token}"}

    def get(self, ruta: str = "", **params: str) -> httpx.Response:
        respuesta = httpx.get(
            f"{self._base}{ruta}", params=params, headers=self._cabeceras, timeout=TIMEOUT_S
        )
        assert respuesta.status_code != 403, (
            "el usuario E2E del back-office debe estar en el grupo analistas-siniestros "
            "(infra/DESPLIEGUE.md)"
        )
        return respuesta

    def aprobar(self, siniestro_id: str, clave: str) -> httpx.Response:
        cuerpo = {
            "tipo": "APROBAR",
            "justificacion": JUSTIFICACION,
            "evidenciasConsideradas": [],
            "liquidacion": LIQUIDACION,
        }
        return httpx.post(
            f"{self._base}/{siniestro_id}/decision",
            json=cuerpo,
            headers={**self._cabeceras, "Idempotency-Key": clave},
            timeout=TIMEOUT_S,
        )

    def pago_final(self, siniestro_id: str) -> dict[str, Any]:
        """Espera a que `dinero` y `siniestros` reflejen el resultado y devuelve la vista de H08."""
        limite = time.monotonic() + ESPERA_PAGO_S
        vista: dict[str, Any] = {}
        while time.monotonic() < limite:
            respuesta = self.get(f"/{siniestro_id}/pago")
            if respuesta.status_code == 200:
                vista = respuesta.json()
                indemnizacion = vista.get("indemnizacion")
                if indemnizacion and indemnizacion["estado"] in ("CONFIRMADO", "RECHAZADO"):
                    return vista
            time.sleep(3)
        pytest.fail(
            f"el pago de {siniestro_id} no llegó a un estado final en {ESPERA_PAGO_S} s: {vista}"
        )


@pytest.fixture(scope="module")
def portal(ambiente: Any) -> Portal:
    config = ambiente.requerir(
        HISTORIA, {"canales": ambiente.urls["canales"], "token": ambiente.token_admin()}
    )
    return Portal(config["canales"], config["token"])


@pytest.fixture(scope="module")
def siniestro_a_aprobar(portal: Portal) -> str:
    elegido = os.getenv("E2E_W23_SINIESTRO_ID", "")
    if elegido:
        return elegido
    respuesta = portal.get(bandeja="MIA", estado="EN_EVALUACION", limite="1")
    assert respuesta.status_code == 200, respuesta.text
    items = respuesta.json()["items"]
    if not items:
        pytest.skip(f"{HISTORIA}: «Mi bandeja» no tiene siniestros EN_EVALUACION (¿semilla de QA?)")
    return str(items[0]["id"])


def test_hu_w23_r7_c01_aprobar_paga_una_vez_y_el_detalle_refleja_el_pago(
    portal: Portal, siniestro_a_aprobar: str
) -> None:
    clave = str(uuid.uuid4())
    decision = portal.aprobar(siniestro_a_aprobar, clave)
    assert decision.status_code == 200, decision.text
    assert decision.json()["estadoNuevo"] == "APROBADO"
    assert REFERENCIA.match(decision.json()["referenciaPago"])
    CLAVES["decision"] = clave

    vista = portal.pago_final(siniestro_a_aprobar)
    indemnizacion = vista["indemnizacion"]
    assert vista["estado"] == "APROBADO"
    assert indemnizacion["estado"] == "CONFIRMADO", vista
    assert indemnizacion["referencia"] == decision.json()["referenciaPago"]
    assert indemnizacion["monto"] == "2500000.00"
    assert indemnizacion["intentos"] == 1
    assert indemnizacion["transaccionId"]
    assert indemnizacion["confirmadaEn"]
    # Nunca la cuenta completa: solo la enmascarada.
    assert re.fullmatch(r"\*{4}\w{2,6}", vista["cuentaMascarada"])
    assert [h["accion"] for h in vista["historial"]][-2:] == ["APROBADO", "PAGO_CONFIRMADO"]

    detalle = portal.get(f"/{siniestro_a_aprobar}")
    assert detalle.status_code == 200
    pasos = {p["paso"]: p["estado"] for p in detalle.json()["tramite"]}
    assert pasos["DECISION"] == "COMPLETADO"
    assert pasos["PAGO_INDEMNIZACION"] == "COMPLETADO"
    assert detalle.json()["pago"]["estado"] == "CONFIRMADO"


def test_hu_w23_r7_c02_repetir_la_decision_no_paga_dos_veces(
    portal: Portal, siniestro_a_aprobar: str
) -> None:
    clave = CLAVES.get("decision")
    if clave is None:
        pytest.skip(f"{HISTORIA}: C02 depende de C01")
    antes = portal.pago_final(siniestro_a_aprobar)["indemnizacion"]

    repetida = portal.aprobar(siniestro_a_aprobar, clave)
    assert repetida.status_code == 200, repetida.text
    assert repetida.json()["estadoNuevo"] == "APROBADO"

    otra_clave = portal.aprobar(siniestro_a_aprobar, str(uuid.uuid4()))
    assert otra_clave.status_code == 409, otra_clave.text
    assert otra_clave.json()["codigo"] == "TRANSICION_NO_PERMITIDA"

    despues = portal.pago_final(siniestro_a_aprobar)["indemnizacion"]
    assert despues["intentos"] == 1
    assert despues["transaccionId"] == antes["transaccionId"]
    assert despues["confirmadaEn"] == antes["confirmadaEn"]


def test_hu_w23_r7_c03_un_siniestro_sin_aprobar_no_tiene_pago(portal: Portal) -> None:
    respuesta = portal.get(bandeja="MIA", estado="EN_EVALUACION", limite="1")
    assert respuesta.status_code == 200
    items = respuesta.json()["items"]
    if not items:
        pytest.skip(f"{HISTORIA}: no queda un siniestro EN_EVALUACION que no se haya aprobado")
    pago = portal.get(f"/{items[0]['id']}/pago")
    assert pago.status_code == 404, pago.text
    assert pago.json()["codigo"] == "INDEMNIZACION_NO_ENCONTRADA"
