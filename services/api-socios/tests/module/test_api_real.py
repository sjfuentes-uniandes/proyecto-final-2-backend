import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import boto3
import psycopg
import pytest
from api_socios.adapters.inbound.dependencias import administrar_socios, autenticar_socio
from api_socios.adapters.outbound.apigateway import ApiKeysApiGateway
from api_socios.adapters.outbound.cognito import ClientesCognito
from api_socios.adapters.outbound.postgres import RepositorioPostgres, migrar
from api_socios.application.socios import AdministrarSocios, AutenticarSocio
from api_socios.domain.socios import CanalVenta, NuevoSocio, Permiso, Plan
from api_socios.main import app
from fastapi.testclient import TestClient

docker = pytest.importorskip("docker")
moto = pytest.importorskip("moto")
REGION = "us-east-1"
ALTA = {"nombre": "Fintech Caribe S.A.S.", "canal": "plataforma_digital", "plan": "basico"}
TIERS = {Plan.BASICO: (5, 10, 1000), Plan.ESTANDAR: (50, 100, 50000), Plan.PRUEBA: (1, 2, 5)}


@pytest.fixture(scope="module")
def base() -> Iterator[str]:
    # Ryuk no arranca en Docker Desktop.
    os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")
    from testcontainers.community.postgres import PostgresContainer

    try:
        docker.from_env().ping()
    except docker.errors.DockerException:
        pytest.skip("Docker no está disponible para testcontainers")
    with PostgresContainer("postgres:16", driver=None) as contenedor:
        url = contenedor.get_connection_url()
        migrar(url)
        migrar(url)  # las migraciones son idempotentes
        yield url


@dataclass
class Aws:
    cognito: Any
    apigateway: Any
    pool_id: str
    planes: dict[str, str]


@pytest.fixture
def aws(monkeypatch: pytest.MonkeyPatch) -> Iterator[Aws]:
    for variable, valor in {
        "AWS_ACCESS_KEY_ID": "prueba",
        "AWS_SECRET_ACCESS_KEY": "prueba",
        "AWS_DEFAULT_REGION": REGION,
    }.items():
        monkeypatch.setenv(variable, valor)
    with moto.mock_aws():
        cognito = boto3.client("cognito-idp", region_name=REGION)
        pool_id = cognito.create_user_pool(PoolName="solventa-pruebas-socios")["UserPool"]["Id"]
        cognito.create_resource_server(
            UserPoolId=pool_id,
            Identifier="solventa",
            Name="API de socios",
            Scopes=[{"ScopeName": p.value, "ScopeDescription": p.value} for p in Permiso],
        )
        apigateway = boto3.client("apigateway", region_name=REGION)
        planes = {
            p.value: apigateway.create_usage_plan(
                name=f"solventa-pruebas-socios-{p.value}",
                throttle={"rateLimit": rate, "burstLimit": burst},
                quota={"limit": cuota, "period": "DAY"},
            )["id"]
            for p, (rate, burst, cuota) in TIERS.items()
        }
        yield Aws(cognito, apigateway, pool_id, planes)


@pytest.fixture
def api(base: str, aws: Aws) -> Iterator[TestClient]:
    with psycopg.connect(base, autocommit=True) as conexion:
        conexion.execute("TRUNCATE socio")
    repositorio = RepositorioPostgres(base)
    caso = AdministrarSocios(
        repositorio,
        ClientesCognito(aws.cognito, aws.pool_id, "solventa"),
        ApiKeysApiGateway(aws.apigateway, "pruebas", aws.planes),
    )
    app.dependency_overrides[administrar_socios] = lambda: caso
    app.dependency_overrides[autenticar_socio] = lambda: AutenticarSocio(repositorio)
    yield TestClient(app)
    app.dependency_overrides.clear()


def borde(aws: Aws, creado: dict[str, Any]) -> dict[str, str]:
    cliente = aws.cognito.describe_user_pool_client(
        UserPoolId=aws.pool_id, ClientId=creado["clientId"]
    )
    key = next(
        k
        for k in aws.apigateway.get_api_keys(includeValues=True)["items"]
        if k["value"] == creado["apiKey"]
    )
    return {
        "X-Partner-Client-Id": cliente["UserPoolClient"]["ClientId"],
        "X-Partner-Key-Id": key["id"],
        "X-Partner-Scopes": " ".join(cliente["UserPoolClient"]["AllowedOAuthScopes"]),
    }


def test_ciclo_de_vida_con_dos_socios(api: TestClient, aws: Aws) -> None:
    a = api.post("/admin/socios", json=ALTA | {"permisos": ["cotizar", "consultar"]})
    b = api.post(
        "/admin/socios",
        json=ALTA | {"nombre": "Retail Andino", "plan": "estandar", "permisos": ["emitir"]},
    )
    assert a.status_code == b.status_code == 201
    a, b = a.json(), b.json()

    # AC1
    cliente_a = aws.cognito.describe_user_pool_client(
        UserPoolId=aws.pool_id, ClientId=a["clientId"]
    )["UserPoolClient"]
    assert sorted(cliente_a["AllowedOAuthScopes"]) == ["solventa/consultar", "solventa/cotizar"]
    assert (
        cliente_a["AllowedOAuthFlows"] == ["client_credentials"]
        and cliente_a["ClientSecret"] == a["clientSecret"]
    )
    keys = {k["value"]: k for k in aws.apigateway.get_api_keys(includeValues=True)["items"]}
    assert keys[a["apiKey"]]["tags"]["Socio"] == a["id"] and keys[a["apiKey"]]["enabled"]
    en_plan = aws.apigateway.get_usage_plan_keys(usagePlanId=aws.planes["estandar"])["items"]
    assert [k["id"] for k in en_plan] == [keys[b["apiKey"]]["id"]]

    headers_a, headers_b = borde(aws, a), borde(aws, b)
    assert api.get("/socio/me", headers=headers_a).json()["id"] == a["id"]
    # AC4
    assert api.post("/polizas", headers=headers_a).status_code == 403
    assert api.post("/polizas", headers=headers_b).status_code == 501

    # AC3
    assert api.post(f"/admin/socios/{a['id']}/desactivar").json()["estado"] == "desactivado"
    with pytest.raises(aws.cognito.exceptions.ResourceNotFoundException):
        aws.cognito.describe_user_pool_client(UserPoolId=aws.pool_id, ClientId=a["clientId"])
    assert not aws.apigateway.get_api_key(apiKey=headers_a["X-Partner-Key-Id"])["enabled"]
    assert aws.apigateway.get_api_key(apiKey=headers_b["X-Partner-Key-Id"])["enabled"]
    # AC2
    assert api.get("/socio/me", headers=headers_a).status_code == 401
    assert api.get("/socio/me", headers=headers_b).status_code == 200
    assert api.post(f"/admin/socios/{a['id']}/desactivar").status_code == 200  # idempotente


def test_un_socio_no_accede_a_los_recursos_de_otro(api: TestClient, aws: Aws) -> None:
    a = api.post("/admin/socios", json=ALTA | {"permisos": ["cotizar"]}).json()
    b = api.post(
        "/admin/socios", json=ALTA | {"nombre": "Viajes del Norte", "permisos": ["cotizar"]}
    ).json()
    headers_a, headers_b = borde(aws, a), borde(aws, b)
    perfil_a = api.get("/socio/me", headers=headers_a)
    assert (
        perfil_a.json()["id"] == a["id"]
        and b["id"] not in perfil_a.text
        and b["clientId"] not in perfil_a.text
    )
    assert (
        api.get(
            "/socio/me", headers=headers_a | {"X-Partner-Key-Id": headers_b["X-Partner-Key-Id"]}
        ).status_code
        == 401
    )
    for ruta in ("/admin/socios", f"/admin/socios/{b['id']}"):
        assert api.get(ruta, headers=headers_a).status_code == 404
    assert api.post(f"/admin/socios/{b['id']}/desactivar", headers=headers_a).status_code == 404
    assert api.get("/socio/me", headers=headers_b).status_code == 200


def test_el_secreto_no_queda_en_la_base(api: TestClient, base: str) -> None:
    creado = api.post("/admin/socios", json=ALTA | {"permisos": ["cotizar"]}).json()
    with psycopg.connect(base) as conexion:
        tablas = [
            t
            for (t,) in conexion.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
            )
        ]
        volcado = " ".join(
            str(conexion.execute(f'SELECT json_agg(t) FROM "{t}" t').fetchone()) for t in tablas
        )
    assert creado["clientId"] in volcado
    assert creado["clientSecret"] not in volcado and creado["apiKey"] not in volcado


def test_identificadores_unicos_en_altas_concurrentes(api: TestClient, aws: Aws) -> None:
    caso = app.dependency_overrides[administrar_socios]()

    def alta(n: int) -> str:
        nuevo = NuevoSocio(
            f"Socio {n}", CanalVenta.COMERCIO, Plan.BASICO, frozenset({Permiso.COTIZAR})
        )
        return caso.habilitar(nuevo)[0].id

    with ThreadPoolExecutor(max_workers=8) as hilos:
        ids = list(hilos.map(alta, range(16)))
    assert len(set(ids)) == 16
    assert (
        len(
            aws.cognito.list_user_pool_clients(UserPoolId=aws.pool_id, MaxResults=60)[
                "UserPoolClients"
            ]
        )
        == 16
    )


def test_plan_ausente_del_ambiente_no_crea_nada(api: TestClient, aws: Aws) -> None:
    app.dependency_overrides[administrar_socios]().api_keys.planes.pop("basico")
    respuesta = api.post("/admin/socios", json=ALTA | {"permisos": ["cotizar"]})
    assert respuesta.status_code == 422 and respuesta.json()["codigo"] == "PLAN_NO_DISPONIBLE"
    assert (
        aws.cognito.list_user_pool_clients(UserPoolId=aws.pool_id, MaxResults=60)["UserPoolClients"]
        == []
    )
    assert aws.apigateway.get_api_keys()["items"] == []
    assert api.get("/admin/socios").json() == []


def _keys_en(aws: Aws, plan: str) -> list[str]:
    return [
        k["id"] for k in aws.apigateway.get_usage_plan_keys(usagePlanId=aws.planes[plan])["items"]
    ]


def test_cambio_de_plan_real_mueve_solo_la_api_key_del_socio(
    api: TestClient, aws: Aws, base: str
) -> None:
    a = api.post("/admin/socios", json=ALTA | {"permisos": ["cotizar"]}).json()
    b = api.post(
        "/admin/socios", json=ALTA | {"nombre": "Viajes del Norte", "permisos": ["cotizar"]}
    ).json()
    key_a, key_b = borde(aws, a)["X-Partner-Key-Id"], borde(aws, b)["X-Partner-Key-Id"]

    cambio = api.put(f"/admin/socios/{a['id']}/plan", json={"plan": "prueba"})
    assert cambio.status_code == 200 and cambio.json()["plan"] == "prueba"
    assert _keys_en(aws, "prueba") == [key_a]
    assert _keys_en(aws, "basico") == [key_b]
    with psycopg.connect(base) as conexion:
        planes = dict(conexion.execute("SELECT id, plan FROM socio").fetchall())
    assert planes == {a["id"]: "prueba", b["id"]: "basico"}

    disponibles = {p["plan"]: p for p in api.get("/admin/socios/planes").json()}
    assert disponibles["prueba"] == {
        "plan": "prueba",
        "limitePorSegundo": 1.0,
        "rafaga": 2,
        "cuota": 5,
        "periodo": "DAY",
    }
    assert api.get("/socio/me", headers=borde(aws, a)).json()["plan"] == "prueba"
