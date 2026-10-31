"""Pruebas de módulo: `clientes` contra un Postgres real (HU-M06)."""

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

import httpx
import psycopg
import pytest
from clientes.adapters.outbound.postgres import RepositorioPostgres, crear_pool, migrar
from clientes.config import Settings
from clientes.domain.identidad.modelos import (
    DatosCliente,
    EstadoIdentidad,
    RegistroYaVinculado,
    ResultadoKyc,
)
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


@pytest.fixture(params=["sin_pool", "con_pool"])
def repo(base: str, request: pytest.FixtureRequest) -> Iterator[RepositorioPostgres]:
    """Cada prueba del repositorio corre sin pool (una conexión por operación) y con el pool que
    usa la app: la semántica (transacción, idempotencia, concurrencia) debe ser la misma."""
    with psycopg.connect(base, autocommit=True) as conexion:
        conexion.execute("TRUNCATE cliente CASCADE")
    if request.param == "sin_pool":
        yield RepositorioPostgres(base)
        return
    pool = crear_pool(base, 1, 4)
    pool.open(wait=True)
    try:
        yield RepositorioPostgres(base, pool)
    finally:
        pool.close()


def test_el_pool_reutiliza_las_conexiones_en_lecturas_paralelas(base: str) -> None:
    """Muchas lecturas en paralelo (el nombre de cada cliente de una vista) no abren una conexión
    por lectura: el pool no pasa de su máximo."""
    pool = crear_pool(base, 1, 3)
    pool.open(wait=True)
    try:
        repo = RepositorioPostgres(base, pool)
        cliente = repo.asegurar("sub-pool")
        with ThreadPoolExecutor(max_workers=8) as hilos:
            leidos = list(hilos.map(lambda _: repo.obtener(cliente.id), range(40)))
        assert all(c is not None and c.id == cliente.id for c in leidos)
        estadisticas = pool.get_stats()
        assert estadisticas["pool_max"] == 3
        assert estadisticas.get("connections_num", 0) <= 3
    finally:
        pool.close()


def test_con_pool_un_error_deshace_la_transaccion(base: str) -> None:
    """La conexión vuelve al pool sin la transacción a medias: la siguiente operación funciona."""
    pool = crear_pool(base, 1, 1)
    pool.open(wait=True)
    try:
        repo = RepositorioPostgres(base, pool)
        with pytest.raises(psycopg.errors.UndefinedTable), repo._conectar() as conexion:
            conexion.execute("SELECT * FROM tabla_que_no_existe")
        assert repo.asegurar("sub-tras-error").sub == "sub-tras-error"
    finally:
        pool.close()


def test_la_tabla_no_guarda_biometria(base: str) -> None:
    """AC4 (back): solo el veredicto de la identidad; ninguna columna para la evidencia."""
    with psycopg.connect(base) as conexion:
        columnas = {
            fila[0]
            for fila in conexion.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'cliente'"
            )
        }
    assert columnas == {
        "id",
        "sub",
        "registro_id",
        "correo",
        "estado",
        "verificado_en",
        "creado",
        # HU-W30: documento del alta por un asesor y traza de la verificación.
        "tipo_documento",
        "numero_documento",
        "nombre_completo",
        "fecha_nacimiento",
        "proveedor",
        "referencia",
    }
    assert not {c for c in columnas if "selfie" in c or "biometr" in c or "rostro" in c}


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


# --- HU-W30: alta por documento y verificación documental ---

ASESOR = {"X-Actor-Id": "asesor-1", "X-Actor-Rol": "ASESOR"}
ALTA = {
    "tipoDocumento": "CC",
    "numeroDocumento": "1020456789",
    "nombreCompleto": "Natalia González Ruiz",
    "fechaNacimiento": "1990-04-18",
}


def _proveedor(resultado: str = "APROBADO") -> httpx.Response:
    cuerpo = {
        "resultado": resultado,
        "motivo": None,
        "referencia": "kyc-ref-001",
        "proveedor": "KYC",
    }
    return httpx.Response(200, json=cuerpo)


def _contar(repo: RepositorioPostgres) -> int:
    with psycopg.connect(repo.url) as conexion:
        fila = conexion.execute("SELECT count(*) FROM cliente").fetchone()
    assert fila is not None
    return int(fila[0])


def test_el_alta_por_documento_es_idempotente_y_nace_pendiente(repo: RepositorioPostgres) -> None:
    datos = DatosCliente("CC", "1020456789", "Natalia González Ruiz", date(1990, 4, 18))

    primero, creado = repo.asegurar_por_documento(datos)
    segundo, creado_de_nuevo = repo.asegurar_por_documento(
        DatosCliente("CC", "1020456789", "Otro Nombre", date(1980, 1, 1))
    )

    assert (creado, creado_de_nuevo) == (True, False)
    assert primero == segundo
    assert primero.estado is EstadoIdentidad.PENDIENTE
    assert primero.datos == datos
    assert _contar(repo) == 1


def test_el_alta_concurrente_del_mismo_documento_deja_un_solo_cliente(
    repo: RepositorioPostgres,
) -> None:
    datos = DatosCliente("CC", "1020456789", "Natalia González Ruiz", date(1990, 4, 18))
    with ThreadPoolExecutor(max_workers=10) as pool:
        resultados = list(pool.map(lambda _: repo.asegurar_por_documento(datos), range(20)))
    assert len({c.id for c, _ in resultados}) == 1
    assert sum(1 for _, creado in resultados if creado) == 1


def test_el_mismo_numero_con_otro_tipo_de_documento_es_otro_cliente(
    repo: RepositorioPostgres,
) -> None:
    fecha = date(1990, 4, 18)
    cedula, _ = repo.asegurar_por_documento(DatosCliente("CC", "1020456789", "A B", fecha))
    extranjeria, _ = repo.asegurar_por_documento(DatosCliente("CE", "1020456789", "A B", fecha))
    assert cedula.id != extranjeria.id


def test_guardar_persiste_proveedor_y_referencia(repo: RepositorioPostgres) -> None:
    ahora = datetime(2026, 10, 17, 12, tzinfo=UTC)
    cliente, _ = repo.asegurar_por_documento(
        DatosCliente("CC", "1020456789", "Natalia González Ruiz", date(1990, 4, 18))
    )

    repo.guardar(cliente.con_resultado(ResultadoKyc.APROBADO, ahora, "KYC", "kyc-ref-001"))

    guardado = repo.obtener(cliente.id)
    assert guardado is not None and guardado.verificado
    assert (guardado.verificado_en, guardado.proveedor, guardado.referencia) == (
        ahora,
        "KYC",
        "kyc-ref-001",
    )


def test_un_documento_incompleto_no_existe(base: str, repo: RepositorioPostgres) -> None:
    with psycopg.connect(base) as conexion, pytest.raises(psycopg.errors.CheckViolation):
        conexion.execute(
            "INSERT INTO cliente (id, numero_documento, estado) VALUES (%s, '1020456789', 'PENDIENTE')",
            (uuid4(),),
        )


def test_el_cliente_por_documento_y_el_de_registro_conviven_tras_migrar_de_nuevo(
    base: str, repo: RepositorioPostgres
) -> None:
    """La migración es idempotente y no rompe a los clientes de la HU-M06."""
    repo.asegurar("sub-m06")
    repo.asegurar_registro(uuid4(), "a@b.co")
    repo.asegurar_por_documento(DatosCliente("CC", "1020456789", "A B", date(1990, 4, 18)))
    migrar(base)
    assert _contar(repo) == 3


def test_flujo_del_asesor_sobre_postgres_con_falla_tecnica_y_reintento(
    repo: RepositorioPostgres,
) -> None:
    """AC1, AC3 y AC4: pendiente ante la falla, verificado al reintentar, una sola identidad."""
    cliente = _app(repo, [httpx.Response(503), _proveedor("APROBADO")])
    with cliente:
        alta = cliente.post("/v1/clientes", json=ALTA, headers=ASESOR)
        assert alta.status_code == 201
        cliente_id = alta.json()["clienteId"]
        ruta = f"/v1/clientes/{cliente_id}/identidad/verificacion-documental"

        caida = cliente.post(ruta, headers=ASESOR)
        assert caida.status_code == 503
        assert caida.json()["detalles"]["estado"] == "PENDIENTE"
        assert cliente.get(f"/v1/clientes/{cliente_id}", headers=ASESOR).json()["estado"] == (
            "PENDIENTE"
        )

        verificado = cliente.post(ruta, headers=ASESOR).json()
        assert verificado["estado"] == "VERIFICADO"
        assert (verificado["proveedor"], verificado["referencia"]) == ("KYC", "kyc-ref-001")
        # Confirmación repetida: no consume otra respuesta del proveedor.
        assert cliente.post(ruta, headers=ASESOR).json() == verificado
        assert cliente.post("/v1/clientes", json=ALTA, headers=ASESOR).status_code == 200
        identidad = cliente.get(f"/v1/clientes/{cliente_id}/identidad").json()
        assert identidad["estado"] == "VERIFICADO"
    assert _contar(repo) == 1


def test_un_rechazo_deja_la_identidad_rechazada_sobre_postgres(repo: RepositorioPostgres) -> None:
    """AC2: la identidad rechazada queda registrada, con su traza y sin duplicar al cliente."""
    cliente = _app(repo, [_proveedor("RECHAZADO")])
    with cliente:
        cliente_id = cliente.post("/v1/clientes", json=ALTA, headers=ASESOR).json()["clienteId"]
        respuesta = cliente.post(
            f"/v1/clientes/{cliente_id}/identidad/verificacion-documental", headers=ASESOR
        ).json()
        assert respuesta["estado"] == "RECHAZADO" and respuesta["verificadoEn"] is None
        assert cliente.get(f"/v1/clientes/{cliente_id}/identidad").json()["estado"] == "RECHAZADO"
    assert _contar(repo) == 1
