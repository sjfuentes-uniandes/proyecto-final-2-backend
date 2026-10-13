"""N3 HU-W01 contra un ambiente desplegado (int).

Requisitos:
    make infra-microservicios SERVICES=api-socios,bff-web ENV=int
    make test-e2e            # o: uv run pytest tests/e2e -k hu_w01 -v

Administrador (grupo administradores-socios del pool backoffice, que exige MFA), una de dos:
    E2E_ADMIN_TOKEN                      ID token copiado del portal (uso local), o
    E2E_ADMIN_USUARIO, E2E_ADMIN_CLAVE   usuario dedicado a la prueba, y
    E2E_ADMIN_TOTP                       semilla base32 de su aplicación de autenticación
                                         (ingreso SRP + código TOTP; así corre en .github/workflows/e2e.yml).

Las URL, el pool backoffice y las credenciales de socio-b salen de `terraform output` de
infra/platform (o de E2E_CANALES_URL, E2E_SOCIOS_URL, E2E_TOKEN_URL y E2E_SOCIO_B_*).
"""

import json
import os
import subprocess
import time
from collections.abc import Callable
from functools import cache
from pathlib import Path
from typing import Any

import httpx
import pytest

RAIZ = Path(__file__).parents[2]
ESPERA_S = int(os.getenv("E2E_ESPERA_S", "90"))  # propagación de Cognito y caché de API keys


@cache
def _salidas() -> dict[str, Any]:
    try:
        texto = subprocess.run(["terraform", f"-chdir={RAIZ / 'infra/platform'}", "output", "-json"],
                               check=True, capture_output=True, text=True, timeout=120).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    return {k: v["value"] for k, v in json.loads(texto).items()}


def _token_admin(salidas: dict[str, Any]) -> str:
    """ID token del administrador: el dado, o ingreso SRP + TOTP con el usuario de la prueba."""
    if os.getenv("E2E_ADMIN_TOKEN"):
        return os.environ["E2E_ADMIN_TOKEN"]
    usuario, clave, semilla = (os.getenv(v, "") for v in ("E2E_ADMIN_USUARIO", "E2E_ADMIN_CLAVE", "E2E_ADMIN_TOTP"))
    backoffice = salidas.get("cognito", {}).get("backoffice", {})
    if not (usuario and clave and semilla and backoffice.get("pool_id")):
        return ""
    import pyotp
    from pycognito import Cognito
    from pycognito.exceptions import SoftwareTokenMFAChallengeException

    sesion = Cognito(backoffice["pool_id"], backoffice["client_id"], username=usuario)
    try:
        sesion.authenticate(password=clave)
    except SoftwareTokenMFAChallengeException as reto:
        sesion.respond_to_software_token_mfa_challenge(pyotp.TOTP(semilla).now(), reto.get_tokens())
    return sesion.id_token or ""


def _config() -> dict[str, str]:
    salidas = _salidas()
    urls = salidas.get("api_urls", {})
    socio_b = salidas.get("partners", {}).get("socio-b", {})
    valores = {
        "admin_token": _token_admin(salidas),
        "canales": os.getenv("E2E_CANALES_URL", urls.get("canales", "")),
        "socios": os.getenv("E2E_SOCIOS_URL", urls.get("socios_mtls") or urls.get("socios", "")),
        "token_url": os.getenv("E2E_TOKEN_URL", salidas.get("cognito", {}).get("partners", {}).get("token_url", "")),
        "b_client_id": os.getenv("E2E_SOCIO_B_CLIENT_ID", socio_b.get("client_id") or ""),
        "b_client_secret": os.getenv("E2E_SOCIO_B_CLIENT_SECRET", socio_b.get("client_secret") or ""),
        "b_api_key": os.getenv("E2E_SOCIO_B_API_KEY", socio_b.get("api_key") or ""),
    }
    faltan = [k for k, v in valores.items() if not v]
    if faltan:
        motivo = f"N3 HU-W01 sin configurar: faltan {', '.join(faltan)}"
        # En el workflow E2E (E2E_REQUERIDO=1) la falta de configuración es un error, no un "skipped".
        if os.getenv("E2E_REQUERIDO") == "1":
            pytest.fail(motivo)
        pytest.skip(motivo)
    return {k: v.rstrip("/") for k, v in valores.items()}


def _token(config: dict[str, str], client_id: str, secreto: str) -> httpx.Response:
    return httpx.post(config["token_url"], auth=(client_id, secreto), data={"grant_type": "client_credentials"},
                      timeout=20)


def _hasta(condicion: Callable[[], httpx.Response], esperado: int) -> httpx.Response:
    """Reintenta mientras Cognito y API Gateway propagan el cambio."""
    limite = time.monotonic() + ESPERA_S
    while True:
        respuesta = condicion()
        if respuesta.status_code == esperado or time.monotonic() > limite:
            return respuesta
        time.sleep(3)


def test_hu_w01_alta_consumo_rechazo_y_desactivacion_aislada() -> None:
    config = _config()
    admin = {"Authorization": f"Bearer {config['admin_token']}"}
    sello = time.strftime("%Y%m%d%H%M%S")

    # 1. Alta desde la administración (bff-web -> api-socios): secreto visible una sola vez.
    alta = httpx.post(f"{config['canales']}/web/socios", headers=admin, timeout=30, json={
        "nombre": f"E2E HU-W01 {sello}", "canal": "plataforma_digital", "plan": "basico", "permisos": ["cotizar"],
    })
    assert alta.status_code == 201, alta.text
    socio = alta.json()
    assert socio["clientSecret"] and socio["apiKey"] and alta.headers.get("cache-control") == "no-store"
    detalle = httpx.get(f"{config['canales']}/web/socios/{socio['id']}", headers=admin, timeout=30)
    assert detalle.status_code == 200 and socio["clientSecret"] not in detalle.text

    try:
        # 2. Token client_credentials + API key -> GET /socio/me = 200.
        token = _hasta(lambda: _token(config, socio["clientId"], socio["clientSecret"]), 200)
        assert token.status_code == 200, token.text
        acceso = {"Authorization": f"Bearer {token.json()['access_token']}", "x-api-key": socio["apiKey"]}
        perfil = _hasta(lambda: httpx.get(f"{config['socios']}/socio/me", headers=acceso, timeout=30), 200)
        assert perfil.status_code == 200, perfil.text
        assert perfil.json()["id"] == socio["id"] and "clientSecret" not in perfil.text
        # AC4: permiso no asignado.
        assert httpx.post(f"{config['socios']}/polizas", headers=acceso, timeout=30).status_code == 403

        # 3. Secreto inválido: Cognito no emite token y el API responde 401 con correlationId.
        assert _token(config, socio["clientId"], "secreto-incorrecto").status_code in (400, 401)
        invalido = httpx.get(f"{config['socios']}/socio/me", timeout=30,
                             headers={"Authorization": "Bearer token-invalido", "x-api-key": socio["apiKey"]})
        assert invalido.status_code == 401 and invalido.headers.get("x-correlation-id")
    finally:
        # 4. Desactivar (idempotente) y repetir: 401; socio-b sigue autenticándose.
        desactivado = httpx.post(f"{config['canales']}/web/socios/{socio['id']}/desactivar", headers=admin, timeout=30)
        assert desactivado.status_code == 200 and desactivado.json()["estado"] == "desactivado"

    assert _token(config, socio["clientId"], socio["clientSecret"]).status_code in (400, 401)
    rechazado = _hasta(lambda: httpx.get(f"{config['socios']}/socio/me", headers=acceso, timeout=30), 401)
    assert rechazado.status_code == 401, rechazado.text

    token_b = _token(config, config["b_client_id"], config["b_client_secret"])
    assert token_b.status_code == 200, token_b.text
    salud_b = httpx.get(f"{config['socios']}/health", timeout=30,
                        headers={"Authorization": f"Bearer {token_b.json()['access_token']}",
                                 "x-api-key": config["b_api_key"]})
    assert salud_b.status_code == 200, salud_b.text
