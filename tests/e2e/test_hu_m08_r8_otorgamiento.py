"""N3 R8 (HU-M08) contra un ambiente desplegado: otorgamiento del consentimiento desde la app.

Misma intención que la versión de módulo (`services/bff-movil/tests/module/
test_r8_otorgamiento_postgres.py`), pero por el borde real: API Gateway (`/movil`, autorizador
del pool de clientes) → `bff-movil` → `clientes` → RDS. Los terceros son los simuladores del
ambiente: la identidad de los dos titulares de prueba se verificó antes con HU-M06 contra el
simulador KYC. No hay acceso a la base: "no se creó nada" se comprueba por la API.

Sin revocación hasta HU-M09, un titular solo puede tener un consentimiento vigente por
propósito: la primera corrida lo crea (201) y las siguientes reciben 409 con el existente. La
prueba feliz verifica el existente en ese caso y termina en `skip` para que se vea que el alta no
se volvió a ejercer. "`clientes` apagado" no se reproduce aquí (sería tocar el ambiente): lo
cubre la versión de módulo.

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

    def otorgar(self, cuerpo: dict[str, Any], clave: str | None = None) -> httpx.Response:
        return self.pedir(
            "POST",
            "/v1/consentimientos",
            json=cuerpo,
            headers={"Idempotency-Key": clave or str(uuid.uuid4())},
        )

    def cuerpo(self, **cambios: Any) -> dict[str, Any]:
        texto = self.pedir(
            "GET",
            "/v1/consentimientos/texto-vigente",
            params={"proposito": PROPOSITO, "idioma": "es-CO"},
        )
        assert texto.status_code == 200, texto.text
        return {
            "proposito": PROPOSITO,
            "alcances": ["FA_PRODUCTOS_VIGENTES"],
            "vigenciaMeses": 12,
            "versionTexto": texto.json()["versionTexto"],
            "idioma": "es-CO",
            **cambios,
        }

    def consentimientos(self) -> list[dict[str, Any]]:
        respuesta = self.pedir("GET", "/v1/consentimientos")
        assert respuesta.status_code == 200, respuesta.text
        return respuesta.json()

    def vigente_o_nuevo(self) -> tuple[httpx.Response, str]:
        """Otorga; si ya hay uno vigente (corrida anterior), devuelve el existente."""
        respuesta = self.otorgar(self.cuerpo())
        if respuesta.status_code == 409:
            assert respuesta.json()["codigo"] == "CONSENTIMIENTO_YA_VIGENTE", respuesta.text
            return respuesta, respuesta.json()["detalles"]["consentimientoId"]
        assert respuesta.status_code == 201, respuesta.text
        return respuesta, respuesta.json()["consentimientoId"]


def _titular(ambiente, prefijo: str) -> Titular:
    config = ambiente.requerir(
        "HU-M08 (R8)",
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


def test_hu_m08_r8_sin_token_el_borde_responde_401(ambiente) -> None:
    config = ambiente.requerir("HU-M08 (R8)", {"canales": ambiente.urls["canales"]})

    respuesta = httpx.get(f"{config['canales']}/movil/v1/consentimientos", timeout=TIMEOUT_S)

    assert respuesta.status_code == 401


def test_hu_m08_r8_feliz_otorgar_y_consultar_el_comprobante(titular_a: Titular) -> None:
    respuesta, consentimiento_id = titular_a.vigente_o_nuevo()

    detalle = titular_a.pedir("GET", f"/v1/consentimientos/{consentimiento_id}")
    assert detalle.status_code == 200, detalle.text
    comprobante = detalle.json()
    assert comprobante["estado"] == "ACTIVO"
    assert comprobante["proposito"] == PROPOSITO
    assert comprobante["versionTexto"] and comprobante["vigenteHasta"] > comprobante["otorgadoEn"]
    assert comprobante in titular_a.consentimientos()
    assert respuesta.headers.get("X-Correlation-Id")
    if respuesta.status_code == 409:
        pytest.skip(
            "El titular ya tenía un consentimiento vigente: se verificó el existente, pero el "
            "alta (201) no se volvió a ejercer. Se podrá repetir cuando HU-M09 permita revocar."
        )
    assert respuesta.status_code == 201
    assert respuesta.json() == comprobante


@pytest.mark.parametrize(
    ("cambios", "codigo"),
    [
        ({"vigenciaMeses": None}, "SOLICITUD_INVALIDA"),
        ({"proposito": None, "alcances": None, "vigenciaMeses": None}, "SOLICITUD_INVALIDA"),
        ({"vigenciaMeses": 7}, "VIGENCIA_INVALIDA"),
    ],
)
def test_hu_m08_r8_fallo_solicitud_invalida_no_crea_nada(
    titular_a: Titular, cambios: dict[str, Any], codigo: str
) -> None:
    antes = titular_a.consentimientos()

    respuesta = titular_a.otorgar(titular_a.cuerpo(**cambios))

    assert respuesta.status_code == 422, respuesta.text
    assert respuesta.json()["codigo"] == codigo
    if codigo == "SOLICITUD_INVALIDA":
        faltantes = {c for c, v in cambios.items() if v is None}
        assert faltantes <= set(respuesta.json()["detalles"]["campos"])
    assert titular_a.consentimientos() == antes


def test_hu_m08_r8_fallo_repetir_la_misma_clave_no_crea_otro(titular_a: Titular) -> None:
    antes = titular_a.consentimientos()
    cuerpo, clave = titular_a.cuerpo(), str(uuid.uuid4())

    primera = titular_a.otorgar(cuerpo, clave=clave)
    segunda = titular_a.otorgar(cuerpo, clave=clave)

    def identificador(respuesta: httpx.Response) -> str:
        cuerpo_respuesta = respuesta.json()
        return (
            cuerpo_respuesta.get("consentimientoId")
            or cuerpo_respuesta["detalles"]["consentimientoId"]
        )

    assert primera.status_code in (201, 409), primera.text
    if primera.status_code == 201:
        assert segunda.status_code == 200, segunda.text
        assert segunda.headers.get("Idempotent-Replayed") == "true"
    else:
        # Con uno vigente, la clave no se guardó (409): la repetición vuelve a ser 409.
        assert segunda.status_code == 409, segunda.text
    assert identificador(segunda) == identificador(primera)
    despues = titular_a.consentimientos()
    assert len(despues) <= len(antes) + 1
    assert sum(c["estado"] == "ACTIVO" and c["proposito"] == PROPOSITO for c in despues) == 1


def test_hu_m08_r8_fallo_un_titular_no_ve_el_consentimiento_de_otro(
    titular_a: Titular, titular_b: Titular
) -> None:
    _, ajeno = titular_b.vigente_o_nuevo()

    detalle = titular_a.pedir("GET", f"/v1/consentimientos/{ajeno}")

    assert detalle.status_code == 404, detalle.text
    assert detalle.json()["codigo"] == "CONSENTIMIENTO_NO_ENCONTRADO"
    assert all(c["consentimientoId"] != ajeno for c in titular_a.consentimientos())
    assert titular_b.pedir("GET", f"/v1/consentimientos/{ajeno}").status_code == 200
