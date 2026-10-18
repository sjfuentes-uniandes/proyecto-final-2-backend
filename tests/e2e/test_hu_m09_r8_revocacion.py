"""N3 R8 (HU-M09) contra un ambiente desplegado: revocación del consentimiento desde la app.

Misma intención que la versión de módulo (`services/bff-movil/tests/module/
test_r8_revocacion_postgres.py`), pero por el borde real: API Gateway (`/movil`, autorizador
del pool de clientes) → `bff-movil` → `clientes` → RDS. Usa los dos titulares de prueba de
HU-M08 (identidad ya `VERIFICADO`). No hay acceso a la base: el outbox y "un solo evento" no se
pueden ver aquí; se comprueba por la API que el estado y el historial no cambian al repetir.

Cada prueba que necesita un consentimiento vigente lo obtiene (lo otorga, o usa el que dejó una
corrida anterior) y lo deja revocado: así la prueba se puede repetir y la feliz de HU-M08 vuelve
a ejercer el alta (201). "Vencido" y "`clientes` apagado o lento" no se reproducen aquí (exigiría
tocar la base o el ambiente): los cubre la versión de módulo.

Configuración y precondiciones en infra/DESPLIEGUE.md (sección 7, "Pruebas E2E (N3)").
"""

import os
import uuid
from typing import Any

import httpx
import pytest

PROPOSITO = "PERFILAMIENTO_PRECIO"
TIMEOUT_S = 30


def _token_cliente(ambiente, prefijo: str) -> str:
    """Access token del pool de clientes por SRP (el flujo que habilita `app-movil`)."""
    if os.getenv(f"{prefijo}_TOKEN"):
        return os.environ[f"{prefijo}_TOKEN"]
    usuario, clave = os.getenv(f"{prefijo}_USUARIO", ""), os.getenv(f"{prefijo}_CLAVE", "")
    pool = ambiente.salidas.get("cognito", {}).get("customers", {})
    pool_id = os.getenv("E2E_CLIENTES_POOL_ID", pool.get("pool_id") or "")
    client_id = os.getenv("E2E_CLIENTES_APP_CLIENT_ID", pool.get("app_client_id") or "")
    if not (usuario and clave and pool_id and client_id):
        return ""
    from pycognito import Cognito

    sesion = Cognito(pool_id, client_id, username=usuario)
    sesion.authenticate(password=clave)
    return sesion.access_token or ""


class Titular:
    def __init__(self, base: str, token: str) -> None:
        self._base = f"{base}/movil"
        self._cabeceras = {"Authorization": f"Bearer {token}"}

    def pedir(self, metodo: str, ruta: str, **kwargs: Any) -> httpx.Response:
        cabeceras = {**self._cabeceras, **kwargs.pop("headers", {})}
        return httpx.request(
            metodo, f"{self._base}{ruta}", headers=cabeceras, timeout=TIMEOUT_S, **kwargs
        )

    def otorgar(self) -> httpx.Response:
        texto = self.pedir(
            "GET",
            "/v1/consentimientos/texto-vigente",
            params={"proposito": PROPOSITO, "idioma": "es-CO"},
        )
        assert texto.status_code == 200, texto.text
        cuerpo = {
            "proposito": PROPOSITO,
            "alcances": ["FA_PRODUCTOS_VIGENTES"],
            "vigenciaMeses": 12,
            "versionTexto": texto.json()["versionTexto"],
            "idioma": "es-CO",
        }
        return self.pedir(
            "POST",
            "/v1/consentimientos",
            json=cuerpo,
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )

    def vigente(self) -> dict[str, Any]:
        """Un consentimiento `ACTIVO` del titular: el que ya tenía o uno nuevo."""
        respuesta = self.otorgar()
        if respuesta.status_code == 409:
            assert respuesta.json()["codigo"] == "CONSENTIMIENTO_YA_VIGENTE", respuesta.text
            existente = respuesta.json()["detalles"]["consentimientoId"]
            detalle = self.pedir("GET", f"/v1/consentimientos/{existente}")
            assert detalle.status_code == 200, detalle.text
            return detalle.json()
        assert respuesta.status_code == 201, respuesta.text
        return respuesta.json()

    def revocar(self, consentimiento_id: str) -> httpx.Response:
        return self.pedir("POST", f"/v1/consentimientos/{consentimiento_id}/revocacion")

    def historial(self, consentimiento_id: str) -> httpx.Response:
        return self.pedir("GET", f"/v1/consentimientos/{consentimiento_id}/historial")

    def consentimientos(self) -> list[dict[str, Any]]:
        respuesta = self.pedir("GET", "/v1/consentimientos")
        assert respuesta.status_code == 200, respuesta.text
        return respuesta.json()


def _titular(ambiente, prefijo: str) -> Titular:
    config = ambiente.requerir(
        "HU-M09 (R8)",
        {
            "canales": ambiente.urls["canales"],
            f"{prefijo}_token": _token_cliente(ambiente, prefijo),
        },
    )
    titular = Titular(config["canales"], config[f"{prefijo}_token"])
    sesion = titular.pedir("GET", "/v1/sesion")
    assert sesion.status_code == 200, sesion.text
    assert "PRIVACIDAD" in sesion.json()["serviciosAutorizados"], (
        f"{prefijo}: el titular debe tener la identidad VERIFICADO (HU-M06, infra/DESPLIEGUE.md)"
    )
    return titular


@pytest.fixture(scope="module")
def titular_a(ambiente) -> Titular:
    return _titular(ambiente, "E2E_CLIENTE_A")


@pytest.fixture(scope="module")
def titular_b(ambiente) -> Titular:
    return _titular(ambiente, "E2E_CLIENTE_B")


def test_hu_m09_r8_sin_token_el_borde_responde_401(ambiente) -> None:
    config = ambiente.requerir("HU-M09 (R8)", {"canales": ambiente.urls["canales"]})

    respuesta = httpx.post(
        f"{config['canales']}/movil/v1/consentimientos/{uuid.uuid4()}/revocacion",
        timeout=TIMEOUT_S,
    )

    assert respuesta.status_code == 401


def test_hu_m09_r8_feliz_revocar_y_consultar_lista_e_historial(titular_a: Titular) -> None:
    activo = titular_a.vigente()
    consentimiento_id = activo["consentimientoId"]

    respuesta = titular_a.revocar(consentimiento_id)

    assert respuesta.status_code == 200, respuesta.text
    revocado = respuesta.json()
    assert revocado["estado"] == "REVOCADO"
    assert revocado["revocadoEn"] and revocado["revocadoEn"] >= revocado["otorgadoEn"]
    assert {k: v for k, v in revocado.items() if k not in ("estado", "revocadoEn")} == {
        k: v for k, v in activo.items() if k not in ("estado", "revocadoEn")
    }
    assert respuesta.headers.get("X-Correlation-Id")
    assert titular_a.pedir("GET", f"/v1/consentimientos/{consentimiento_id}").json() == revocado
    assert revocado in titular_a.consentimientos()
    historial = titular_a.historial(consentimiento_id)
    assert historial.status_code == 200, historial.text
    assert historial.json() == {
        "consentimientoId": consentimiento_id,
        "estado": "REVOCADO",
        "hitos": [
            {"tipo": "OTORGADO", "ocurridoEn": revocado["otorgadoEn"]},
            {"tipo": "REVOCADO", "ocurridoEn": revocado["revocadoEn"]},
        ],
    }


def test_hu_m09_r8_feliz_tras_revocar_se_otorga_uno_nuevo(titular_a: Titular) -> None:
    anterior = titular_a.vigente()
    assert titular_a.revocar(anterior["consentimientoId"]).status_code == 200

    nuevo = titular_a.otorgar()

    assert nuevo.status_code == 201, nuevo.text
    assert nuevo.json()["consentimientoId"] != anterior["consentimientoId"]
    estados = {c["consentimientoId"]: c["estado"] for c in titular_a.consentimientos()}
    assert estados[anterior["consentimientoId"]] == "REVOCADO"
    assert estados[nuevo.json()["consentimientoId"]] == "ACTIVO"
    # Se deja revocado para que la prueba se pueda repetir.
    assert titular_a.revocar(nuevo.json()["consentimientoId"]).status_code == 200


def test_hu_m09_r8_fallo_repetir_la_revocacion_no_cambia_nada(titular_a: Titular) -> None:
    consentimiento_id = titular_a.vigente()["consentimientoId"]

    primera = titular_a.revocar(consentimiento_id)
    historial = titular_a.historial(consentimiento_id).json()
    segunda = titular_a.revocar(consentimiento_id)

    assert (primera.status_code, segunda.status_code) == (200, 200)
    assert segunda.json() == primera.json()
    assert titular_a.historial(consentimiento_id).json() == historial


def test_hu_m09_r8_fallo_un_titular_no_revoca_el_consentimiento_de_otro(
    titular_a: Titular, titular_b: Titular
) -> None:
    ajeno = titular_b.vigente()["consentimientoId"]

    respuestas = [
        titular_a.revocar(ajeno),
        titular_a.historial(ajeno),
        titular_a.revocar(str(uuid.uuid4())),
    ]

    for respuesta in respuestas:
        assert respuesta.status_code == 404, respuesta.text
        assert respuesta.json()["codigo"] == "CONSENTIMIENTO_NO_ENCONTRADO"
    # El de B sigue vigente: A no pudo tocarlo.
    detalle = titular_b.pedir("GET", f"/v1/consentimientos/{ajeno}")
    assert (detalle.status_code, detalle.json()["estado"]) == (200, "ACTIVO")
