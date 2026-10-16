"""Pruebas de módulo: `clientes` contra un Postgres real (HU-M06)."""

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
import psycopg
import pytest
from clientes.adapters.outbound.postgres import RepositorioPostgres, migrar
from clientes.config import Settings
from clientes.domain.identidad.modelos import EstadoIdentidad, RegistroYaVinculado, ResultadoKyc
from clientes.main import crear_app
from fastapi.testclient import TestClient

docker = pytest.importorskip("docker")

SUB = "sub-modulo-1"
ACTOR = {"X-Actor-Id": SUB, "X-Actor-Rol": "CLIENTE"}
SELFIE = {"selfie": "selfie-sintetica-aprobada", "pruebaDeVida": {"completado": True}}


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


@pytest.fixture
def repo(base: str) -> RepositorioPostgres:
    with psycopg.connect(base, autocommit=True) as conexion:
        conexion.execute("TRUNCATE cliente")
    return RepositorioPostgres(base)


def test_la_tabla_no_guarda_biometria(base: str) -> None:
    """AC4 (back): solo el veredicto de la identidad; ninguna columna para la evidencia."""
    with psycopg.connect(base) as conexion:
        columnas = {
            fila[0]
            for fila in conexion.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'cliente'"
            )
        }
    assert columnas == {"id", "sub", "registro_id", "correo", "estado", "verificado_en", "creado"}


def test_asegurar_es_idempotente(repo: RepositorioPostgres) -> None:
    primero = repo.asegurar(SUB)
    segundo = repo.asegurar(SUB)
    assert primero == segundo
    assert primero.estado is EstadoIdentidad.PENDIENTE
    assert repo.por_sub(SUB) == primero
    assert repo.obtener(primero.id) == primero


def test_asegurar_concurrente_deja_un_solo_cliente(repo: RepositorioPostgres) -> None:
    """AC3: reintentos simultáneos del mismo titular no crean un segundo cliente."""
    with ThreadPoolExecutor(max_workers=10) as pool:
        ids = {c.id for c in pool.map(repo.asegurar, [SUB] * 20)}
    assert len(ids) == 1
    with psycopg.connect(repo.url) as conexion:
        fila = conexion.execute("SELECT count(*) FROM cliente WHERE sub = %s", (SUB,)).fetchone()
    assert fila is not None and fila[0] == 1


def test_guardar_persiste_el_estado(repo: RepositorioPostgres) -> None:
    ahora = datetime(2026, 10, 15, 15, tzinfo=UTC)
    cliente = repo.asegurar(SUB)
    repo.guardar(cliente.con_resultado(ResultadoKyc.APROBADO, ahora))
    guardado = repo.por_sub(SUB)
    assert guardado is not None
    assert guardado.estado is EstadoIdentidad.VERIFICADO
    assert guardado.verificado_en == ahora


def test_inexistentes_devuelven_none(repo: RepositorioPostgres) -> None:
    assert repo.por_sub("nadie") is None
    assert repo.obtener(uuid4()) is None
    assert repo.por_registro(uuid4()) is None


def test_asegurar_registro_es_idempotente_y_no_pisa_el_correo(repo: RepositorioPostgres) -> None:
    registro = uuid4()

    primero = repo.asegurar_registro(registro, "uno@ejemplo.co")
    segundo = repo.asegurar_registro(registro, "otro@ejemplo.co")

    assert primero == segundo
    assert primero.sub is None and primero.correo == "uno@ejemplo.co"
    assert primero.registro_id == registro
    assert repo.por_registro(registro) == primero


def test_asegurar_registro_concurrente_deja_un_solo_cliente(repo: RepositorioPostgres) -> None:
    """AC3: reintentos simultáneos del mismo registro no crean un segundo cliente."""
    registro = uuid4()
    with ThreadPoolExecutor(max_workers=10) as pool:
        ids = {
            c.id for c in pool.map(lambda _: repo.asegurar_registro(registro, "a@b.co"), range(20))
        }
    assert len(ids) == 1


def test_vincular_asocia_el_sub_y_es_idempotente(repo: RepositorioPostgres) -> None:
    cliente = repo.asegurar_registro(uuid4(), "a@b.co")

    vinculado = repo.vincular(cliente.id, "sub-vinculo")

    assert vinculado.sub == "sub-vinculo" and vinculado.id == cliente.id
    assert repo.vincular(cliente.id, "sub-vinculo") == vinculado
    assert repo.por_sub("sub-vinculo") == vinculado


def test_vincular_un_cliente_que_ya_es_de_otro_sub(repo: RepositorioPostgres) -> None:
    cliente = repo.asegurar_registro(uuid4(), "a@b.co")
    repo.vincular(cliente.id, "sub-dueno")

    with pytest.raises(RegistroYaVinculado):
        repo.vincular(cliente.id, "sub-intruso")
    assert repo.por_sub("sub-dueno") is not None and repo.por_sub("sub-intruso") is None


def test_vincular_un_sub_que_ya_tiene_otro_cliente(repo: RepositorioPostgres) -> None:
    repo.asegurar("sub-con-cliente")
    cliente = repo.asegurar_registro(uuid4(), "a@b.co")

    with pytest.raises(RegistroYaVinculado):
        repo.vincular(cliente.id, "sub-con-cliente")
    assert repo.obtener(cliente.id).sub is None


def test_vinculos_simultaneos_de_distintos_titulares_dejan_un_solo_dueno(
    repo: RepositorioPostgres,
) -> None:
    cliente = repo.asegurar_registro(uuid4(), "a@b.co")

    def intentar(sub: str) -> str:
        try:
            repo.vincular(cliente.id, sub)
            return "ok"
        except RegistroYaVinculado:
            return "conflicto"

    with ThreadPoolExecutor(max_workers=8) as pool:
        resultados = list(pool.map(intentar, [f"sub-carrera-{i}" for i in range(8)]))

    assert resultados.count("ok") == 1 and resultados.count("conflicto") == 7
    assert repo.obtener(cliente.id).sub.startswith("sub-carrera-")


def test_un_cliente_sin_sub_ni_registro_no_existe(base: str, repo: RepositorioPostgres) -> None:
    with psycopg.connect(base) as conexion, pytest.raises(psycopg.errors.CheckViolation):
        conexion.execute("INSERT INTO cliente (id, estado) VALUES (%s, 'PENDIENTE')", (uuid4(),))


def _app(repo: RepositorioPostgres, respuestas: list[httpx.Response]) -> TestClient:
    cola = iter(respuestas)
    app = crear_app(
        Settings(),
        transport=httpx.MockTransport(lambda _: next(cola)),
        repositorio=repo,
    )
    cliente = TestClient(app)
    cliente.__enter__()
    return cliente


def test_flujo_completo_con_reintento_sobre_postgres(repo: RepositorioPostgres) -> None:
    cliente = _app(
        repo,
        [
            httpx.Response(503),
            httpx.Response(200, json={"resultado": "CAPTURA_INVALIDA", "motivo": "X"}),
            httpx.Response(200, json={"resultado": "APROBADO", "motivo": None}),
        ],
    )
    with cliente:
        ruta = "/v1/clientes/actual/identidad/verificaciones"
        interrumpido = cliente.post(ruta, json=SELFIE, headers=ACTOR)
        assert interrumpido.status_code == 503
        invalida = cliente.post(ruta, json=SELFIE, headers=ACTOR).json()
        assert invalida["estado"] == "PENDIENTE"
        verificado = cliente.post(ruta, json=SELFIE, headers=ACTOR).json()
        assert verificado["estado"] == "VERIFICADO"
        assert verificado["clienteId"] == invalida["clienteId"]
        identidad: Any = cliente.get(f"/v1/clientes/{verificado['clienteId']}/identidad").json()
        assert identidad["estado"] == "VERIFICADO"
    with psycopg.connect(repo.url) as conexion:
        fila = conexion.execute("SELECT count(*) FROM cliente").fetchone()
    assert fila is not None and fila[0] == 1


def test_flujo_de_registro_con_reintentos_sobre_postgres(repo: RepositorioPostgres) -> None:
    """Interrupción, captura inválida, verificado, primer ingreso y sesión: un solo cliente."""
    registro = uuid4()
    verificar = f"/v1/registros/{registro}/verificaciones"
    vincular = f"/v1/registros/{registro}/vinculacion"
    cuerpo = {"correo": "Persona@Ejemplo.co", **SELFIE}
    sub = {"X-Actor-Id": "sub-registro-1", "X-Actor-Rol": "CLIENTE"}
    cliente = _app(
        repo,
        [
            httpx.Response(503),
            httpx.Response(200, json={"resultado": "CAPTURA_INVALIDA", "motivo": "X"}),
            httpx.Response(200, json={"resultado": "APROBADO", "motivo": None}),
        ],
    )
    with cliente:
        assert cliente.post(verificar, json=cuerpo).status_code == 503
        invalida = cliente.post(verificar, json=cuerpo).json()
        assert invalida["estado"] == "PENDIENTE" and invalida["tokenRegistro"] is None
        verificado = cliente.post(verificar, json=cuerpo).json()
        assert verificado["estado"] == "VERIFICADO" and verificado["tokenRegistro"]
        assert verificado["clienteId"] == invalida["clienteId"]

        # La app aún no tiene cuenta: sin `sub` no se resuelve la sesión.
        assert cliente.get("/v1/clientes/actual", headers=sub).status_code == 404
        enlace = cliente.post(
            vincular, json={"tokenRegistro": verificado["tokenRegistro"]}, headers=sub
        )
        assert enlace.status_code == 200, enlace.text
        actual = cliente.get("/v1/clientes/actual", headers=sub)
        assert actual.json()["clienteId"] == verificado["clienteId"]
        assert cliente.post(verificar, json=cuerpo).status_code == 409
    with psycopg.connect(repo.url) as conexion:
        filas = conexion.execute("SELECT sub, correo, estado FROM cliente").fetchall()
    assert filas == [("sub-registro-1", "persona@ejemplo.co", "VERIFICADO")]
