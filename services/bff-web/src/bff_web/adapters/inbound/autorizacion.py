"""Autorización por grupo de Cognito del back-office.

API Gateway valida el token y sobrescribe X-Authenticated-Issuer y
X-Authenticated-Groups con sus claims (infra/platform/edge.tf); el cliente no
puede enviarlos por su cuenta.
"""

import re
from collections.abc import Callable
from typing import Annotated

from fastapi import Header, HTTPException

from bff_web.config import settings


def grupos(valor: str | None) -> set[str]:
    # API Gateway entrega el claim como "[operacion, otro]" o "operacion,otro".
    return set(re.findall(r"[A-Za-z0-9_-]+", valor or ""))


def requerir_grupo(grupo: str) -> Callable[..., None]:
    def dependencia(
        x_authenticated_groups: Annotated[str | None, Header()] = None,
        x_authenticated_issuer: Annotated[str | None, Header()] = None,
    ) -> None:
        emisor = settings.jwt_issuers.get("backoffice")
        if emisor and x_authenticated_issuer != emisor:
            raise HTTPException(403, "Solo usuarios del back-office")
        if grupo not in grupos(x_authenticated_groups):
            raise HTTPException(403, f"Requiere el grupo {grupo}")

    return dependencia
