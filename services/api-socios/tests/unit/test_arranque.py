from collections.abc import Iterator

import pytest
from api_socios import main
from api_socios.adapters.inbound import dependencias
from api_socios.adapters.outbound import postgres
from api_socios.adapters.outbound.apigateway import ApiKeysApiGateway, crear_api_keys
from api_socios.adapters.outbound.cloudwatch import MetricasCloudWatch, crear_metricas
from api_socios.adapters.outbound.cognito import ClientesCognito, crear_clientes
from api_socios.application.socios import AdministrarSocios, AutenticarSocio
from api_socios.config import settings
from fastapi import HTTPException
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def limpiar_caches(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "prueba")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "prueba")
    for funcion in (
        dependencias.repositorio,
        dependencias._administrar,
        dependencias.autenticar_socio,
    ):
        funcion.cache_clear()
    yield
    for funcion in (
        dependencias.repositorio,
        dependencias._administrar,
        dependencias.autenticar_socio,
    ):
        funcion.cache_clear()


def test_sin_configuracion_de_socios_responde_503(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "partners_user_pool_id", None)
    with pytest.raises(HTTPException) as error:
        dependencias.administrar_socios()
    assert error.value.status_code == 503


def test_construye_los_adaptadores_reales(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "partners_user_pool_id", "us-east-1_socios")
    monkeypatch.setattr(settings, "partners_usage_plans", {"basico": "p1", "estandar": "p2"})
    monkeypatch.setattr(settings, "database_url_override", "postgresql://u@h/api_socios")
    caso = dependencias.administrar_socios()
    assert isinstance(caso, AdministrarSocios)
    assert (
        isinstance(caso.identidad, ClientesCognito) and caso.identidad.pool_id == "us-east-1_socios"
    )
    assert isinstance(caso.api_keys, ApiKeysApiGateway) and caso.api_keys.planes == {
        "basico": "p1",
        "estandar": "p2",
    }
    assert isinstance(caso.metricas, MetricasCloudWatch)
    assert isinstance(caso.repositorio, postgres.RepositorioPostgres)
    assert caso.repositorio.url == "postgresql://u@h/api_socios"
    assert isinstance(dependencias.autenticar_socio(), AutenticarSocio)
    assert dependencias.administrar_socios() is caso  # una instancia por proceso


def test_fabricas_boto3() -> None:
    clientes = crear_clientes("us-east-1", "us-east-1_p", "solventa")
    keys = crear_api_keys("us-east-1", "int", {"basico": "p1"})
    assert clientes.cliente.meta.service_model.service_name == "cognito-idp"
    assert keys.cliente.meta.service_model.service_name == "apigateway" and keys.ambiente == "int"
    assert crear_metricas("us-east-1").cliente.meta.service_model.service_name == "cloudwatch"


@pytest.mark.parametrize("con_base", [True, False])
def test_el_arranque_migra_solo_con_base_configurada(
    monkeypatch: pytest.MonkeyPatch, con_base: bool
) -> None:
    llamadas: list[str] = []
    monkeypatch.setattr(postgres, "migrar", llamadas.append)
    monkeypatch.setattr(settings, "db_host", "rds.interno" if con_base else None)
    monkeypatch.setattr(settings, "database_url_override", None)
    with TestClient(main.app) as cliente:
        assert cliente.get("/health").json() == {"status": "ok"}
    assert len(llamadas) == (1 if con_base else 0)
    if con_base:
        assert "rds.interno" in llamadas[0]
