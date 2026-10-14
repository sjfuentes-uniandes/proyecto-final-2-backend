"""Ambiente desplegado para las pruebas N3; configuración en infra/DESPLIEGUE.md."""

import json
import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class Ambiente:
    salidas: dict[str, Any]
    _tokens: dict[str, str] = field(default_factory=dict)

    @property
    def urls(self) -> dict[str, str]:
        urls = self.salidas.get("api_urls", {})
        return {
            "canales": os.getenv("E2E_CANALES_URL", urls.get("canales", "")),
            "socios": os.getenv("E2E_SOCIOS_URL", urls.get("socios_mtls") or urls.get("socios", "")),
            "token_url": os.getenv("E2E_TOKEN_URL",
                                   self.salidas.get("cognito", {}).get("partners", {}).get("token_url", "")),
        }

    def socio_de_prueba(self, nombre: str) -> dict[str, str]:
        datos = self.salidas.get("partners", {}).get(nombre, {})
        prefijo = f"E2E_{nombre.upper().replace('-', '_')}"
        return {campo: os.getenv(f"{prefijo}_{campo.upper()}", datos.get(campo) or "")
                for campo in ("client_id", "client_secret", "api_key", "api_key_id")}

    def token_admin(self) -> str:
        # Un solo ingreso por sesión: Cognito puede rechazar el mismo código TOTP dos veces.
        if "admin" not in self._tokens:
            self._tokens["admin"] = _token_admin(self.salidas)
        return self._tokens["admin"]

    def requerir(self, historia: str, valores: dict[str, str]) -> dict[str, str]:
        faltan = [k for k, v in valores.items() if not v]
        if faltan:
            motivo = f"N3 {historia} sin configurar: faltan {', '.join(faltan)}"
            if os.getenv("E2E_REQUERIDO") == "1":
                pytest.fail(motivo)
            pytest.skip(motivo)
        return {k: v.rstrip("/") for k, v in valores.items()}

    @staticmethod
    def token(token_url: str, client_id: str, secreto: str) -> httpx.Response:
        return httpx.post(token_url, auth=(client_id, secreto), data={"grant_type": "client_credentials"},
                          timeout=20)

    @staticmethod
    def hasta(condicion: Callable[[], httpx.Response], esperado: int, espera_s: int = ESPERA_S) -> httpx.Response:
        limite = time.monotonic() + espera_s
        while True:
            respuesta = condicion()
            if respuesta.status_code == esperado or time.monotonic() > limite:
                return respuesta
            time.sleep(3)


@pytest.fixture(scope="session")
def ambiente() -> Ambiente:
    return Ambiente(_salidas())
