"""Revalidación del JWT de Cognito en el BFF (F1), como defensa en profundidad tras API Gateway."""

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Protocol

import jwt
from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from solventa_common.errors import NO_AUTENTICADO, ROL_NO_AUTORIZADO, ErrorNegocio

from bff_movil.adapters.inbound.http.codigos import SERVICIO_NO_DISPONIBLE
from bff_movil.adapters.inbound.http.jwks import ClaveNoEncontrada, JwksNoDisponible
from bff_movil.config import Settings

# Clave del emisor del pool de clientes en JWT_ISSUERS (infra/platform/outputs.tf).
POOL_CLIENTES = "customers"
ALGORITMOS = ["RS256"]
# Vigencia que se simula en modo local: la del access token de la app (infra/platform/identity.tf).
SESION_LOCAL = timedelta(minutes=30)
_PATRON_SUB = re.compile(r"[A-Za-z0-9._:@-]{1,128}")
_CLAIMS_REQUERIDOS = ["exp", "iss", "sub", "token_use"]

logger = logging.getLogger("solventa.sesion")


@dataclass(frozen=True, slots=True)
class IdentidadAutenticada:
    sub: str
    expira_en: datetime


class ProveedorClaves(Protocol):
    async def obtener(self, emisor: str, kid: str) -> jwt.PyJWK: ...


def _no_autenticado(motivo: str) -> ErrorNegocio:
    # Solo la categoría del rechazo: nunca el token ni el detalle de la excepción.
    logger.warning("sesión rechazada", extra={"motivo": motivo})
    return ErrorNegocio(NO_AUTENTICADO, "No autenticado", 401)


class Autenticador:
    def __init__(self, settings: Settings, claves: ProveedorClaves) -> None:
        self._settings = settings
        self._claves = claves

    async def autenticar(
        self, token: str | None, sub_encabezado: str | None, emisor_encabezado: str | None
    ) -> IdentidadAutenticada:
        if token is None:
            return self._modo_local(sub_encabezado)
        emisor, kid = self._leer_cabecera(token)
        claims = await self._verificar(token, emisor, kid)
        if claims["token_use"] != "access":
            raise _no_autenticado("token_use")
        sub = claims["sub"]
        if not isinstance(sub, str) or sub != sub_encabezado or not _PATRON_SUB.fullmatch(sub):
            raise _no_autenticado("sub")
        if emisor_encabezado is not None and emisor_encabezado != emisor:
            raise _no_autenticado("emisor_encabezado")
        if emisor != self._settings.jwt_issuers.get(POOL_CLIENTES):
            logger.warning("sesión rechazada", extra={"motivo": "pool"})
            raise ErrorNegocio(ROL_NO_AUTORIZADO, "No autorizado", 403)
        return IdentidadAutenticada(sub=sub, expira_en=datetime.fromtimestamp(claims["exp"], UTC))

    def _modo_local(self, sub: str | None) -> IdentidadAutenticada:
        # F1: este modo solo existe con ENVIRONMENT=local.
        if self._settings.environment != "local" or sub is None or not _PATRON_SUB.fullmatch(sub):
            raise _no_autenticado("sin_credencial")
        return IdentidadAutenticada(sub=sub, expira_en=datetime.now(UTC) + SESION_LOCAL)

    def _leer_cabecera(self, token: str) -> tuple[str, str]:
        # El emisor sin verificar solo elige entre los emisores configurados; la firma se
        # valida después con las claves de ese emisor.
        try:
            kid = jwt.get_unverified_header(token).get("kid")
            emisor = jwt.decode(token, options={"verify_signature": False}).get("iss")
        except jwt.PyJWTError as error:
            raise _no_autenticado("malformado") from error
        if (
            not isinstance(kid, str)
            or not isinstance(emisor, str)
            or emisor not in self._settings.jwt_issuers.values()
        ):
            raise _no_autenticado("emisor_o_kid")
        return emisor, kid

    async def _verificar(self, token: str, emisor: str, kid: str) -> dict[str, object]:
        try:
            clave = await self._claves.obtener(emisor, kid)
            return jwt.decode(
                token,
                clave,
                algorithms=ALGORITMOS,
                issuer=emisor,
                options={"require": _CLAIMS_REQUERIDOS, "verify_aud": False},
            )
        except ClaveNoEncontrada as error:
            raise _no_autenticado("kid") from error
        except JwksNoDisponible as error:
            raise ErrorNegocio(SERVICIO_NO_DISPONIBLE, "Servicio no disponible", 503) from error
        except jwt.ExpiredSignatureError as error:
            raise _no_autenticado("expirado") from error
        except jwt.PyJWTError as error:
            raise _no_autenticado("invalido") from error


_esquema = HTTPBearer(
    auto_error=False,
    scheme_name="bearerAuth",
    description="Access token de Cognito del pool de clientes",
)


async def autenticar(
    request: Request,
    credenciales: Annotated[HTTPAuthorizationCredentials | None, Depends(_esquema)],
    x_authenticated_sub: Annotated[str | None, Header()] = None,
    x_authenticated_issuer: Annotated[str | None, Header()] = None,
) -> IdentidadAutenticada:
    """Revalida el token y que su titular sea el que API Gateway autenticó."""
    autenticador: Autenticador = request.app.state.autenticador
    token = credenciales.credentials if credenciales and credenciales.credentials else None
    return await autenticador.autenticar(token, x_authenticated_sub, x_authenticated_issuer)
