"""N3 R2 y R8 (HU-W31) contra un ambiente desplegado: verificación del consentimiento.

Misma intención que la versión de módulo (`services/cotizacion/tests/module/
test_r2_r8_verificacion.py`), pero por el borde real: la app (`/movil`, pool de clientes →
`bff-movil` → `clientes`) otorga y revoca, y el portal (`/web`, pool del back-office →
`bff-web` → `cotizacion` → `clientes`) pide el perfil. Usa el titular A de HU-M08 (identidad
`VERIFICADO`) y el usuario E2E del back-office, que debe estar en el grupo `asesores`.

Sin acceso a las bases: que la decisión quede registrada (`VerificacionConsentimientoRegistrada`)
y que el proveedor no reciba llamadas no se ve aquí; se comprueba por la respuesta (decisión,
causa y fuentes `NO_AUTORIZADA`). Tampoco se reproducen la revocación a mitad del perfilamiento,
el vencimiento ni `clientes` caído (exigiría controlar los tiempos, la base o el ambiente): los
cubre la versión de módulo.

Cada prueba obtiene un consentimiento vigente del titular A (lo otorga o usa el que ya tenía) y lo
deja revocado, como las de HU-M09: se pueden repetir y la feliz de HU-M08 vuelve a dar 201.

Configuración y precondiciones en infra/DESPLIEGUE.md (sección 7, "Pruebas E2E (N3)").
"""

import os
import uuid
from typing import Any

import httpx
import pytest

PROPOSITO = "PERFILAMIENTO_PRECIO"
TIMEOUT_S = 30
HISTORIA = "HU-W31 (R2 y R8)"
# Valores declarados sintéticos (los de C01 en contracts/datos-sinteticos).
DECLARADOS = {"fechaNacimiento": "1990-04-18", "plazoCreditoMeses": 180, "sumaAsegurada": 180000000}


def _token_cliente(ambiente: Any, prefijo: str) -> str:
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
    """La app móvil del titular, por `/movil`."""

    def __init__(self, base: str, token: str) -> None:
        self._base = f"{base}/movil"
        self._cabeceras = {"Authorization": f"Bearer {token}"}

    def pedir(self, metodo: str, ruta: str, **kwargs: Any) -> httpx.Response:
        cabeceras = {**self._cabeceras, **kwargs.pop("headers", {})}
        return httpx.request(
            metodo, f"{self._base}{ruta}", headers=cabeceras, timeout=TIMEOUT_S, **kwargs
        )

    def cliente_id(self) -> str:
        sesion = self.pedir("GET", "/v1/sesion")
        assert sesion.status_code == 200, sesion.text
        assert "PRIVACIDAD" in sesion.json()["serviciosAutorizados"], (
            "el titular A debe tener la identidad VERIFICADO (HU-M06, infra/DESPLIEGUE.md)"
        )
        return str(sesion.json()["clienteId"])

    def vigente(self) -> dict[str, Any]:
        """Un consentimiento `ACTIVO` del titular: el que ya tenía o uno nuevo, con un solo
        alcance (el resto queda sin autorizar: W31 AC3)."""
        texto = self.pedir(
            "GET",
            "/v1/consentimientos/texto-vigente",
            params={"proposito": PROPOSITO, "idioma": "es-CO"},
        )
        assert texto.status_code == 200, texto.text
        respuesta = self.pedir(
            "POST",
            "/v1/consentimientos",
            json={
                "proposito": PROPOSITO,
                "alcances": ["FA_PRODUCTOS_VIGENTES"],
                "vigenciaMeses": 12,
                "versionTexto": texto.json()["versionTexto"],
                "idioma": "es-CO",
            },
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        if respuesta.status_code == 409:
            assert respuesta.json()["codigo"] == "CONSENTIMIENTO_YA_VIGENTE", respuesta.text
            existente = respuesta.json()["detalles"]["consentimientoId"]
            detalle = self.pedir("GET", f"/v1/consentimientos/{existente}")
            assert detalle.status_code == 200, detalle.text
            return dict(detalle.json())
        assert respuesta.status_code == 201, respuesta.text
        return dict(respuesta.json())

    def revocar(self, consentimiento_id: str) -> httpx.Response:
        return self.pedir("POST", f"/v1/consentimientos/{consentimiento_id}/revocacion")


class Asesor:
    """El portal, por `/web`, con el usuario E2E del back-office (grupo `asesores`)."""

    def __init__(self, base: str, token: str) -> None:
        self._url = f"{base}/web/v1/perfiles"
        self._cabeceras = {"Authorization": f"Bearer {token}"}

    def perfilar(self, cliente_id: str) -> httpx.Response:
        respuesta = httpx.post(
            self._url,
            json={
                "clienteId": cliente_id,
                "productoId": "vida-hipotecario",
                "datosDeclarados": DECLARADOS,
            },
            headers={**self._cabeceras, "Idempotency-Key": str(uuid.uuid4())},
            timeout=TIMEOUT_S,
        )
        assert respuesta.status_code != 403, (
            "el usuario E2E del back-office debe estar en el grupo asesores (infra/DESPLIEGUE.md)"
        )
        return respuesta


def _fuentes(perfil: dict[str, Any]) -> dict[str, str]:
    return {f["fuente"]: f["estado"] for f in perfil["fuentes"]}


@pytest.fixture(scope="module")
def config(ambiente: Any) -> dict[str, str]:
    return ambiente.requerir(
        HISTORIA,
        {
            "canales": ambiente.urls["canales"],
            "admin_token": ambiente.token_admin(),
            "E2E_CLIENTE_A_token": _token_cliente(ambiente, "E2E_CLIENTE_A"),
        },
    )


@pytest.fixture(scope="module")
def titular(config: dict[str, str]) -> Titular:
    return Titular(config["canales"], config["E2E_CLIENTE_A_token"])


@pytest.fixture(scope="module")
def asesor(config: dict[str, str]) -> Asesor:
    return Asesor(config["canales"], config["admin_token"])


def test_hu_w31_r8_sin_token_el_borde_responde_401(ambiente: Any) -> None:
    config = ambiente.requerir(HISTORIA, {"canales": ambiente.urls["canales"]})

    respuesta = httpx.post(f"{config['canales']}/web/v1/perfiles", json={}, timeout=TIMEOUT_S)

    assert respuesta.status_code == 401


def test_hu_w31_r8_feliz_vigente_permite_y_tras_revocar_rechaza(
    titular: Titular, asesor: Asesor
) -> None:
    cliente_id = titular.cliente_id()
    activo = titular.vigente()
    try:
        antes = asesor.perfilar(cliente_id)

        assert antes.status_code == 201, antes.text
        perfil = antes.json()
        assert perfil["consentimiento"]["decision"] in ("PERMITIDO", "PERMITIDO_PARCIAL")
        # W31 AC3: lo que el consentimiento no autoriza no se consulta.
        for fuente, estado in _fuentes(perfil).items():
            if fuente not in activo["alcances"]:
                assert estado != "CONSULTADA", (fuente, estado)
    finally:
        revocado = titular.revocar(activo["consentimientoId"])
    assert revocado.status_code == 200, revocado.text

    despues = asesor.perfilar(cliente_id)

    # R8 / M09 AC2: la consulta siguiente a la revocación se rechaza antes del proveedor.
    assert despues.status_code == 201, despues.text
    perfil = despues.json()
    assert (perfil["consentimiento"]["decision"], perfil["consentimiento"]["causa"]) == (
        "RECHAZADO",
        "REVOCADO",
    )
    assert perfil["accionSugerida"] == "COMPLETAR_CONSENTIMIENTO"
    assert set(_fuentes(perfil).values()) == {"NO_AUTORIZADA"}
    assert perfil.get("comprobacionFinal") is None
    assert perfil.get("revocadoDurantePerfilamiento", False) is False


def test_hu_w31_r2_fallo_cliente_inexistente(asesor: Asesor) -> None:
    respuesta = asesor.perfilar(str(uuid.uuid4()))

    assert respuesta.status_code == 404, respuesta.text
    assert respuesta.json()["codigo"] == "CLIENTE_NO_ENCONTRADO"
