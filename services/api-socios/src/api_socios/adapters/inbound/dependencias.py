"""Construcción perezosa de los casos de uso (las pruebas los reemplazan con dependency_overrides)."""

from functools import lru_cache

from fastapi import HTTPException

from api_socios.application.socios import AdministrarSocios, AutenticarSocio
from api_socios.config import settings


@lru_cache
def repositorio():  # type: ignore[no-untyped-def]
    from api_socios.adapters.outbound.postgres import RepositorioPostgres

    return RepositorioPostgres(settings.database_url)


def administrar_socios() -> AdministrarSocios:
    if not settings.partners_user_pool_id or not settings.partners_usage_plans:
        raise HTTPException(503, "Faltan PARTNERS_USER_POOL_ID o PARTNERS_USAGE_PLANS")
    return _administrar()


@lru_cache
def _administrar() -> AdministrarSocios:
    from api_socios.adapters.outbound.apigateway import crear_api_keys
    from api_socios.adapters.outbound.cognito import crear_clientes

    return AdministrarSocios(
        repositorio(),
        crear_clientes(
            settings.aws_region,
            settings.partners_user_pool_id or "",
            settings.partners_resource_server,
        ),
        crear_api_keys(settings.aws_region, settings.environment, settings.partners_usage_plans),
    )


@lru_cache
def autenticar_socio() -> AutenticarSocio:
    return AutenticarSocio(repositorio())
