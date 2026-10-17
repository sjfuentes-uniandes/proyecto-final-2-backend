"""Pruebas de módulo: consentimientos de `clientes` contra un Postgres real (HU-M08, PR 1.3).

Cubren la migración 002 (hacia adelante, reejecutada y hacia atrás), las invariantes de la base
(índice único parcial, `CHECK`, disparador), la función de fin de vigencia (ADR 0010) y el
repositorio asíncrono con su outbox. El límite exacto de la vigencia se prueba en el dominio:
el reloj de la base no se puede congelar.
"""

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from importlib import resources
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from clientes.adapters.outbound import outbox_pg
from clientes.adapters.outbound.consentimientos_pg import ConsentimientosPg
from clientes.adapters.outbound.postgres import RepositorioPostgres, migrar
from clientes.adapters.outbound.textos_json import TextosJson
from clientes.application.consentimientos.casos import OtorgarConsentimiento
from clientes.application.consentimientos.puertos import Otorgamiento
from clientes.application.identidad.casos import ConsultarCliente
from clientes.domain.consentimientos.modelos import (
    ClaveIdempotenciaEnUso,
    ConsentimientoActivoExistente,
    EstadoConsentimiento,
    SolicitudOtorgamiento,
)
from clientes.domain.identidad.modelos import EstadoIdentidad
from psycopg.conninfo import make_conninfo
from psycopg_pool import AsyncConnectionPool

docker = pytest.importorskip("docker")

pytestmark = pytest.mark.anyio

PROPOSITO = "PERFILAMIENTO_PRECIO"
TEXTOS = TextosJson.cargar()
VERSION = TEXTOS.vigente(PROPOSITO, "es-CO").version


@pytest.fixture
def anyio_backend() -> tuple[str, dict[str, Any]]:
    # psycopg async no corre sobre el ProactorEventLoop de Windows.
    return "asyncio", {"loop_factory": asyncio.SelectorEventLoop}


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


@pytest.fixture(autouse=True)
def limpia(base: str) -> None:
    with psycopg.connect(base, autocommit=True) as conexion:
        conexion.execute("TRUNCATE consentimiento_idempotencia, consentimiento, outbox, cliente")


@pytest.fixture
async def pool(base: str) -> AsyncIterator[AsyncConnectionPool]:
    async with AsyncConnectionPool(base, min_size=1, max_size=10, open=False) as abierto:
        yield abierto


@pytest.fixture
def repo(pool: AsyncConnectionPool) -> ConsentimientosPg:
    return ConsentimientosPg(pool)


def _sql(base: str, sentencia: str, parametros: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with psycopg.connect(base, autocommit=True) as conexion:
        cursor = conexion.execute(sentencia, parametros)
        return cursor.fetchall() if cursor.description else []


def _cliente(base: str, cliente_id: UUID | None = None) -> UUID:
    """Crea (o reutiliza) un cliente: `consentimiento.cliente_id` tiene FK a `cliente(id)`."""
    cliente_id = cliente_id or uuid4()
    _sql(
        base,
        "INSERT INTO cliente (id, sub, estado) VALUES (%s, %s, 'PENDIENTE') ON CONFLICT DO NOTHING",
        (cliente_id, f"sub-{cliente_id}"),
    )
    return cliente_id


def _otorgamiento(
    cliente_id: UUID,
    *,
    clave: UUID | None = None,
    meses: int = 12,
    alcances: tuple[str, ...] = ("FA_PRODUCTOS_VIGENTES", "DA_REGISTROS_PUBLICOS"),
    huella: str = "huella-1",
) -> Otorgamiento:
    solicitud = SolicitudOtorgamiento.desde(
        proposito=PROPOSITO,
        alcances=alcances,
        vigencia_meses=meses,
        version_texto=VERSION,
        idioma="es-CO",
    )
    return Otorgamiento(
        id=uuid4(),
        cliente_id=cliente_id,
        solicitud=solicitud,
        clave=clave or uuid4(),
        huella=huella,
        actor_id="sub-modulo",
        correlation_id="corr-modulo",
    )


def _insertar_consentimiento(
    base: str,
    cliente_id: UUID,
    *,
    estado: str = "ACTIVO",
    otorgado_en: datetime | None = None,
    vigente_hasta: datetime | None = None,
    revocado_en: datetime | None = None,
    alcances: list[str] | None = None,
) -> UUID:
    ahora = datetime.now(UTC)
    consentimiento_id = uuid4()
    _cliente(base, cliente_id)
    _sql(
        base,
        "INSERT INTO consentimiento (id, cliente_id, proposito, alcances, version_texto, idioma, "
        "estado, otorgado_en, vigente_hasta, revocado_en) VALUES (%s, %s, %s, %s, 'v', 'es-CO', "
        "%s, %s, %s, %s)",
        (
            consentimiento_id,
            cliente_id,
            PROPOSITO,
            ["FA_PRODUCTOS_VIGENTES"] if alcances is None else alcances,
            estado,
            otorgado_en or ahora,
            vigente_hasta or ahora + timedelta(days=365),
            revocado_en,
        ),
    )
    return consentimiento_id


# --- Migración 002 -----------------------------------------------------------------------------


def test_la_migracion_reejecutada_conserva_un_solo_disparador_y_los_datos(base: str) -> None:
    cliente_id = uuid4()
    consentimiento_id = _insertar_consentimiento(base, cliente_id)
    migrar(base)
    triggers = _sql(
        base, "SELECT count(*) FROM pg_trigger WHERE tgname = 'consentimiento_inmutable'"
    )
    assert triggers == [(1,)]
    assert _sql(base, "SELECT id FROM consentimiento") == [(consentimiento_id,)]


def test_la_reversa_elimina_todo_y_la_migracion_vuelve_a_aplicarse(base: str) -> None:
    nueva = f"down_{uuid4().hex[:8]}"
    _sql(base, f'CREATE DATABASE "{nueva}"')
    url = make_conninfo(base, dbname=nueva)
    objetos = (
        "SELECT (to_regclass('consentimiento') IS NOT NULL), "
        "(to_regclass('consentimiento_idempotencia') IS NOT NULL), "
        "(to_regclass('outbox') IS NOT NULL), "
        "(to_regprocedure('fin_de_vigencia(timestamptz,int)') IS NOT NULL), "
        "(to_regprocedure('consentimiento_inmutable()') IS NOT NULL)"
    )
    down = (
        resources.files("clientes.adapters.outbound")
        .joinpath("migraciones", "down", "002_consentimientos.sql")
        .read_text(encoding="utf-8")
    )
    migrar(url)
    assert _sql(url, objetos) == [(True, True, True, True, True)]
    _sql(url, down)
    _sql(url, down)  # la reversa también es idempotente
    assert _sql(url, objetos) == [(False, False, False, False, False)]
    assert _sql(url, "SELECT to_regclass('cliente') IS NOT NULL") == [(True,)]
    migrar(url)
    assert _sql(url, objetos) == [(True, True, True, True, True)]


# --- Función de fin de vigencia (ADR 0010) -----------------------------------------------------


def _referencia(instante: datetime, meses: int) -> datetime:
    """Implementación de referencia: si el día no existe en el mes de destino, el último día."""
    total = instante.month - 1 + meses
    anio, mes = instante.year + total // 12, total % 12 + 1
    siguiente = datetime(anio + (mes == 12), mes % 12 + 1, 1, tzinfo=UTC)
    ultimo_dia = (siguiente - timedelta(days=1)).day
    return instante.replace(year=anio, month=mes, day=min(instante.day, ultimo_dia))


@pytest.mark.parametrize(
    ("otorgado", "meses", "esperado"),
    [
        ("2026-08-31 00:00:00", 6, "2027-02-28 00:00:00"),
        ("2027-08-31 00:00:00", 6, "2028-02-29 00:00:00"),
        ("2026-10-31 00:00:00", 6, "2027-04-30 00:00:00"),
        ("2026-12-31 00:00:00", 12, "2027-12-31 00:00:00"),
        ("2028-02-29 00:00:00", 12, "2029-02-28 00:00:00"),
        ("2026-03-31 23:59:59", 6, "2026-09-30 23:59:59"),
    ],
)
def test_fin_de_vigencia_con_fin_de_mes(
    base: str, otorgado: str, meses: int, esperado: str
) -> None:
    otorgado_utc = datetime.fromisoformat(otorgado).replace(tzinfo=UTC)
    (fila,) = _sql(base, "SELECT fin_de_vigencia(%s, %s)", (otorgado_utc, meses))
    assert fila[0] == datetime.fromisoformat(esperado).replace(tzinfo=UTC)
    assert fila[0] == _referencia(otorgado_utc, meses)


def test_fin_de_vigencia_coincide_con_la_referencia_todos_los_dias_de_dos_anos(base: str) -> None:
    inicio = datetime(2027, 1, 1, 13, 45, 30, tzinfo=UTC)
    instantes = [inicio + timedelta(days=d) for d in range(731)]
    for meses in (6, 12):
        filas = _sql(
            base,
            "SELECT fin_de_vigencia(i, %s) FROM unnest(%s::timestamptz[]) AS i ORDER BY i",
            (meses, instantes),
        )
        assert [f[0] for f in filas] == [_referencia(i, meses) for i in instantes]


def test_fin_de_vigencia_no_depende_de_la_zona_de_la_sesion(base: str) -> None:
    otorgado = datetime(2026, 8, 31, 23, 30, tzinfo=UTC)  # ya es 1 de septiembre en Tokio
    with psycopg.connect(base, autocommit=True) as conexion:
        resultados = set()
        for zona in ("UTC", "Asia/Tokyo", "America/Bogota"):
            conexion.execute(f"SET TimeZone = '{zona}'")
            resultados.add(
                conexion.execute("SELECT fin_de_vigencia(%s, 6)", (otorgado,)).fetchone()
            )
    assert len(resultados) == 1
    assert resultados.pop() == (datetime(2027, 2, 28, 23, 30, tzinfo=UTC),)


# --- Invariantes de la base --------------------------------------------------------------------


def test_el_indice_unico_parcial_permite_un_solo_activo_por_cliente_y_proposito(base: str) -> None:
    cliente_id = uuid4()
    _insertar_consentimiento(base, cliente_id)
    with pytest.raises(psycopg.errors.UniqueViolation) as error:
        _insertar_consentimiento(base, cliente_id)
    assert error.value.diag.constraint_name == "consentimiento_activo_unico"
    _insertar_consentimiento(base, uuid4())  # otro cliente
    _insertar_consentimiento(base, cliente_id, estado="VENCIDO")  # los finales no cuentan
    _insertar_consentimiento(base, cliente_id, estado="REVOCADO", revocado_en=datetime.now(UTC))


def test_la_fk_a_cliente_rechaza_huerfanos_y_no_tiene_cascade(base: str) -> None:
    with pytest.raises(psycopg.errors.ForeignKeyViolation) as error:
        _sql(
            base,
            "INSERT INTO consentimiento (id, cliente_id, proposito, alcances, version_texto, "
            "idioma, estado, otorgado_en, vigente_hasta) VALUES (%s, %s, %s, %s, 'v', 'es-CO', "
            "'ACTIVO', now(), now() + interval '1 year')",
            (uuid4(), uuid4(), PROPOSITO, ["FA_PRODUCTOS_VIGENTES"]),
        )
    assert error.value.diag.constraint_name == "consentimiento_cliente_fk"
    cliente_id = _cliente(base)
    _insertar_consentimiento(base, cliente_id)
    # Sin CASCADE: borrar al cliente no arrastra, ni borra, sus consentimientos.
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _sql(base, "DELETE FROM cliente WHERE id = %s", (cliente_id,))
    assert _sql(base, "SELECT count(*) FROM consentimiento") == [(1,)]
    assert _sql(
        base,
        "SELECT confdeltype FROM pg_constraint WHERE conname = 'consentimiento_cliente_fk'",
    ) == [("a",)]  # 'a' = NO ACTION


@pytest.mark.parametrize(
    "caso",
    [
        {"estado": "REVOCADO"},
        {"estado": "ACTIVO", "revocado_en": datetime(2026, 1, 1, tzinfo=UTC)},
        {"estado": "VENCIDO", "revocado_en": datetime(2026, 1, 1, tzinfo=UTC)},
        {"vigente_hasta": datetime(2000, 1, 1, tzinfo=UTC)},
        {"alcances": []},
    ],
)
def test_los_check_rechazan_filas_incoherentes(base: str, caso: dict[str, Any]) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        _insertar_consentimiento(base, uuid4(), **caso)


def test_el_estado_debe_ser_uno_de_los_tres(base: str) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        _insertar_consentimiento(base, uuid4(), estado="OTORGADO")


def test_un_activo_puede_pasar_a_revocado_o_vencido_solo_con_estado_y_revocado_en(
    base: str,
) -> None:
    revocado = _insertar_consentimiento(base, uuid4())
    _sql(
        base,
        "UPDATE consentimiento SET estado = 'REVOCADO', revocado_en = clock_timestamp() "
        "WHERE id = %s",
        (revocado,),
    )
    vencido = _insertar_consentimiento(base, uuid4())
    _sql(base, "UPDATE consentimiento SET estado = 'VENCIDO' WHERE id = %s", (vencido,))
    assert _sql(base, "SELECT estado FROM consentimiento ORDER BY estado") == [
        ("REVOCADO",),
        ("VENCIDO",),
    ]


@pytest.mark.parametrize("estado", ["REVOCADO", "VENCIDO"])
def test_un_estado_final_no_cambia_ni_se_reactiva(base: str, estado: str) -> None:
    final = _insertar_consentimiento(
        base,
        uuid4(),
        estado=estado,
        revocado_en=datetime.now(UTC) if estado == "REVOCADO" else None,
    )
    for cambio in (
        "estado = 'ACTIVO', revocado_en = NULL",
        "estado = 'VENCIDO', revocado_en = NULL",
        "estado = 'REVOCADO', revocado_en = clock_timestamp()",
    ):
        with pytest.raises(psycopg.errors.RaiseException):
            _sql(base, f"UPDATE consentimiento SET {cambio} WHERE id = %s", (final,))


def test_un_activo_no_puede_seguir_activo_con_otro_valor(base: str) -> None:
    activo = _insertar_consentimiento(base, uuid4())
    with pytest.raises(psycopg.errors.RaiseException):
        _sql(base, "UPDATE consentimiento SET estado = 'ACTIVO' WHERE id = %s", (activo,))


@pytest.mark.parametrize(
    "columna",
    [
        "id = gen_random_uuid()",
        "cliente_id = gen_random_uuid()",
        "proposito = 'OTRO'",
        "alcances = ARRAY['DA_REGISTROS_PUBLICOS']",
        "version_texto = 'otra'",
        "idioma = 'en-US'",
        "otorgado_en = otorgado_en - interval '1 day'",
        "vigente_hasta = vigente_hasta + interval '1 day'",
    ],
)
def test_las_columnas_inmutables_no_cambian_ni_al_revocar(base: str, columna: str) -> None:
    activo = _insertar_consentimiento(base, uuid4())
    with pytest.raises(psycopg.errors.RaiseException, match="inmutables"):
        _sql(
            base,
            f"UPDATE consentimiento SET estado = 'REVOCADO', revocado_en = clock_timestamp(), "
            f"{columna} WHERE id = %s",
            (activo,),
        )
    assert _sql(base, "SELECT estado FROM consentimiento WHERE id = %s", (activo,)) == [("ACTIVO",)]


@pytest.mark.parametrize("estado", ["ACTIVO", "REVOCADO", "VENCIDO"])
def test_un_consentimiento_nunca_se_elimina(base: str, estado: str) -> None:
    consentimiento_id = _insertar_consentimiento(
        base,
        uuid4(),
        estado=estado,
        revocado_en=datetime.now(UTC) if estado == "REVOCADO" else None,
    )
    with pytest.raises(psycopg.errors.RaiseException, match="no se elimina"):
        _sql(base, "DELETE FROM consentimiento WHERE id = %s", (consentimiento_id,))


# --- Repositorio asíncrono ---------------------------------------------------------------------


async def test_crear_usa_el_reloj_de_la_base_y_calcula_la_vigencia_alli(
    base: str, repo: ConsentimientosPg
) -> None:
    cliente_id = _cliente(base)
    antes = datetime.now(UTC)
    creado = await repo.crear(_otorgamiento(cliente_id, meses=6))
    despues = datetime.now(UTC)
    assert creado.estado is EstadoConsentimiento.ACTIVO
    assert antes - timedelta(seconds=5) <= creado.otorgado_en <= despues + timedelta(seconds=5)
    assert creado.otorgado_en.tzinfo is not None
    assert creado.leido_en == creado.otorgado_en
    assert creado.vigente_hasta == _referencia(creado.otorgado_en, 6)
    assert creado.alcances == ("DA_REGISTROS_PUBLICOS", "FA_PRODUCTOS_VIGENTES")
    assert creado.revocado_en is None
    assert creado.vigente


async def test_crear_escribe_dato_clave_y_evento_en_la_misma_transaccion(
    base: str, repo: ConsentimientosPg
) -> None:
    cliente_id = _cliente(base)
    otorgamiento = _otorgamiento(cliente_id)
    creado = await repo.crear(otorgamiento)
    assert _sql(
        base,
        "SELECT cliente_id, clave, hash_cuerpo, consentimiento_id, creado_en "
        "FROM consentimiento_idempotencia",
    ) == [(cliente_id, otorgamiento.clave, "huella-1", creado.id, creado.otorgado_en)]
    (evento,) = _sql(
        base,
        "SELECT aggregate_id, event_type, event_version, occurred_at, correlation_id, payload, "
        "published_at, attempts, last_error FROM outbox",
    )
    assert evento[:5] == (
        creado.id,
        "ConsentimientoOtorgado",
        1,
        creado.otorgado_en,
        "corr-modulo",
    )
    assert evento[6:] == (None, 0, None)  # sin publicar: el relay llega en la PR 2.2
    assert evento[5] == {
        "consentimientoId": str(creado.id),
        "clienteId": str(cliente_id),
        "proposito": PROPOSITO,
        "alcances": ["DA_REGISTROS_PUBLICOS", "FA_PRODUCTOS_VIGENTES"],
        "versionTexto": VERSION,
        "otorgadoEn": creado.otorgado_en.isoformat(),
        "vigenteHasta": creado.vigente_hasta.isoformat(),
        "actorId": "sub-modulo",
    }
    assert _sql(base, "SELECT count(*) FROM outbox WHERE published_at IS NULL") == [(1,)]


async def test_si_falla_el_outbox_no_queda_ni_el_dato_ni_la_clave(
    base: str, repo: ConsentimientosPg, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Atomicidad: dato + evento en la misma transacción."""

    async def falla(*_: object) -> None:
        raise RuntimeError("outbox caído")

    monkeypatch.setattr(outbox_pg, "insertar", falla)
    with pytest.raises(RuntimeError):
        await repo.crear(_otorgamiento(_cliente(base)))
    assert _sql(base, "SELECT count(*) FROM consentimiento") == [(0,)]
    assert _sql(base, "SELECT count(*) FROM consentimiento_idempotencia") == [(0,)]
    assert _sql(base, "SELECT count(*) FROM outbox") == [(0,)]


async def test_un_segundo_activo_del_mismo_cliente_no_deja_rastro(
    base: str, repo: ConsentimientosPg
) -> None:
    cliente_id = _cliente(base)
    await repo.crear(_otorgamiento(cliente_id))
    with pytest.raises(ConsentimientoActivoExistente):
        await repo.crear(_otorgamiento(cliente_id))
    assert _sql(base, "SELECT count(*) FROM consentimiento") == [(1,)]
    assert _sql(base, "SELECT count(*) FROM outbox") == [(1,)]
    assert _sql(base, "SELECT count(*) FROM consentimiento_idempotencia") == [(1,)]


async def test_la_misma_clave_del_mismo_cliente_ya_registrada_es_clave_en_uso(
    base: str, repo: ConsentimientosPg
) -> None:
    cliente_id, clave = _cliente(base), uuid4()
    primero = await repo.crear(_otorgamiento(cliente_id, clave=clave))
    _sql(
        base,
        "UPDATE consentimiento SET estado = 'REVOCADO', revocado_en = clock_timestamp() "
        "WHERE id = %s",
        (primero.id,),
    )
    with pytest.raises(ClaveIdempotenciaEnUso):
        await repo.crear(_otorgamiento(cliente_id, clave=clave))
    assert _sql(base, "SELECT count(*) FROM outbox") == [(1,)]


async def test_la_clave_es_por_cliente(base: str, repo: ConsentimientosPg) -> None:
    clave = uuid4()
    a = await repo.crear(_otorgamiento(_cliente(base), clave=clave))
    b = await repo.crear(_otorgamiento(_cliente(base), clave=clave))
    assert a.id != b.id


async def test_crear_para_un_cliente_inexistente_viola_la_fk_y_no_deja_rastro(
    base: str, repo: ConsentimientosPg
) -> None:
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        await repo.crear(_otorgamiento(uuid4()))
    assert _sql(base, "SELECT count(*) FROM consentimiento") == [(0,)]
    assert _sql(base, "SELECT count(*) FROM consentimiento_idempotencia") == [(0,)]
    assert _sql(base, "SELECT count(*) FROM outbox") == [(0,)]


async def test_otorgar_sobre_un_activo_vencido_sin_materializar_lo_materializa(
    base: str, repo: ConsentimientosPg
) -> None:
    cliente_id = _cliente(base)
    ahora = datetime.now(UTC)
    vencido = _insertar_consentimiento(
        base,
        cliente_id,
        otorgado_en=ahora - timedelta(days=400),
        vigente_hasta=ahora - timedelta(days=35),
    )
    almacenado = await repo.activo(cliente_id, PROPOSITO)
    assert almacenado is not None
    assert almacenado.estado is EstadoConsentimiento.ACTIVO
    assert not almacenado.vigente
    assert almacenado.estado_derivado is EstadoConsentimiento.VENCIDO

    nuevo = await repo.crear(_otorgamiento(cliente_id))
    assert nuevo.id != vencido
    assert _sql(
        base, "SELECT estado, revocado_en FROM consentimiento WHERE id = %s", (vencido,)
    ) == [("VENCIDO", None)]
    assert (await repo.activo(cliente_id, PROPOSITO)) is not None


async def test_un_activo_vigente_no_se_materializa_al_fallar_el_otorgamiento(
    base: str, repo: ConsentimientosPg
) -> None:
    cliente_id = _cliente(base)
    vigente = await repo.crear(_otorgamiento(cliente_id))
    with pytest.raises(ConsentimientoActivoExistente):
        await repo.crear(_otorgamiento(cliente_id))
    assert _sql(base, "SELECT estado FROM consentimiento WHERE id = %s", (vigente.id,)) == [
        ("ACTIVO",)
    ]


async def test_por_clave_devuelve_la_huella_y_la_representacion_actual(
    base: str, repo: ConsentimientosPg
) -> None:
    cliente_id, clave = _cliente(base), uuid4()
    creado = await repo.crear(_otorgamiento(cliente_id, clave=clave, huella="h-123"))
    registro = await repo.por_clave(cliente_id, clave)
    assert registro is not None
    assert registro.huella == "h-123"
    assert registro.consentimiento.id == creado.id
    assert registro.consentimiento.leido_en >= creado.otorgado_en

    _sql(
        base,
        "UPDATE consentimiento SET estado = 'REVOCADO', revocado_en = clock_timestamp() "
        "WHERE id = %s",
        (creado.id,),
    )
    revocado = await repo.por_clave(cliente_id, clave)
    assert revocado is not None
    assert revocado.consentimiento.estado is EstadoConsentimiento.REVOCADO
    assert revocado.consentimiento.revocado_en is not None
    assert await repo.por_clave(uuid4(), clave) is None  # otro cliente
    assert await repo.por_clave(cliente_id, uuid4()) is None  # otra clave


async def test_pasadas_24_horas_la_clave_no_cuenta_y_el_otorgamiento_la_barre(
    base: str, repo: ConsentimientosPg
) -> None:
    cliente_id, clave = _cliente(base), uuid4()
    primero = await repo.crear(_otorgamiento(cliente_id, clave=clave))
    _sql(
        base,
        "UPDATE consentimiento_idempotencia SET creado_en = clock_timestamp() - interval '25 hours'",
    )
    assert await repo.por_clave(cliente_id, clave) is None
    _sql(
        base,
        "UPDATE consentimiento SET estado = 'REVOCADO', revocado_en = clock_timestamp() "
        "WHERE id = %s",
        (primero.id,),
    )
    segundo = await repo.crear(_otorgamiento(cliente_id, clave=clave))
    assert _sql(base, "SELECT consentimiento_id FROM consentimiento_idempotencia") == [
        (segundo.id,)
    ]


async def test_obtener_y_listar_respetan_al_titular(base: str, repo: ConsentimientosPg) -> None:
    propio, ajeno = _cliente(base), _cliente(base)
    primero = await repo.crear(_otorgamiento(propio))
    _sql(
        base,
        "UPDATE consentimiento SET estado = 'REVOCADO', revocado_en = clock_timestamp() "
        "WHERE id = %s",
        (primero.id,),
    )
    segundo = await repo.crear(_otorgamiento(propio))
    de_otro = await repo.crear(_otorgamiento(ajeno))

    assert (await repo.obtener(propio, segundo.id)) is not None
    assert await repo.obtener(propio, de_otro.id) is None
    assert await repo.obtener(propio, uuid4()) is None
    assert [c.id for c in await repo.listar(propio)] == [segundo.id, primero.id]
    assert [c.id for c in await repo.listar(ajeno)] == [de_otro.id]
    assert await repo.listar(uuid4()) == []
    revocado = (await repo.listar(propio))[1]
    assert revocado.estado is EstadoConsentimiento.REVOCADO
    assert revocado.revocado_en is not None


async def test_otorgamiento_concurrente_con_claves_distintas_deja_uno_solo(
    base: str, repo: ConsentimientosPg
) -> None:
    cliente_id = _cliente(base)
    resultados = await asyncio.gather(
        *(repo.crear(_otorgamiento(cliente_id)) for _ in range(8)), return_exceptions=True
    )
    creados = [r for r in resultados if not isinstance(r, BaseException)]
    assert len(creados) == 1
    assert all(isinstance(r, ConsentimientoActivoExistente) for r in resultados if r not in creados)
    assert _sql(base, "SELECT count(*) FROM consentimiento") == [(1,)]
    assert _sql(base, "SELECT count(*) FROM outbox") == [(1,)]


async def test_otorgar_concurrente_con_la_misma_clave_crea_uno_y_el_resto_son_repeticion(
    base: str, repo: ConsentimientosPg
) -> None:
    """AC4 de punta a punta (caso de uso + Postgres): un consentimiento y un evento."""
    clientes = RepositorioPostgres(base)
    cliente = clientes.asegurar("sub-concurrente")
    clientes.guardar(
        replace(cliente, estado=EstadoIdentidad.VERIFICADO, verificado_en=datetime.now(UTC))
    )
    caso = OtorgarConsentimiento(repo, ConsultarCliente(clientes), TEXTOS)
    clave = uuid4()

    async def otorgar() -> Any:
        return await caso.ejecutar(
            "sub-concurrente",
            clave,
            proposito=PROPOSITO,
            alcances=["FA_PRODUCTOS_VIGENTES"],
            vigencia_meses=12,
            version_texto=VERSION,
            idioma="es-CO",
            correlation_id="corr-concurrente",
        )

    resultados = await asyncio.gather(*(otorgar() for _ in range(8)))
    assert len({r.consentimiento.id for r in resultados}) == 1
    assert sum(not r.repeticion for r in resultados) == 1
    assert _sql(base, "SELECT count(*) FROM consentimiento") == [(1,)]
    assert _sql(base, "SELECT count(*) FROM outbox") == [(1,)]
