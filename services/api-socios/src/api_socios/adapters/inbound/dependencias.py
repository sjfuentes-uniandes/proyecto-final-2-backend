from functools import lru_cache

from fastapi import HTTPException

from api_socios.adapters.outbound.cotizacion_http import CotizacionHttp
from api_socios.application.ofertas import ConsultarOferta, CotizarOferta
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
        metricas(),
    )


@lru_cache
def metricas():  # type: ignore[no-untyped-def]
    from api_socios.adapters.outbound.cloudwatch import crear_metricas

    return crear_metricas(settings.aws_region, settings.environment)


def publicar_cuotas() -> int:
    porcentajes = _administrar().porcentajes_de_cuota()
    metricas().publicar_cuotas(porcentajes)
    return len(porcentajes)


@lru_cache
def autenticar_socio() -> AutenticarSocio:
    return AutenticarSocio(repositorio())


@lru_cache
def _cotizacion() -> CotizacionHttp:
    return CotizacionHttp(settings.cotizacion_url, settings.cotizacion_timeout_ms)


def cotizar_oferta() -> CotizarOferta:
    return CotizarOferta(_cotizacion())


def consultar_oferta() -> ConsultarOferta:
    return ConsultarOferta(_cotizacion())
