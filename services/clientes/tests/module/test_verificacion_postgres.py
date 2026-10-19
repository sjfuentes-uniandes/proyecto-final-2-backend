"""Pruebas de módulo de la verificación del consentimiento contra un Postgres real (HU-W31, PR 3.2).

Cubren la consulta con una sola captura del reloj, cada decisión y cada causa con filas reales
(sembradas por INSERT: el disparador solo vigila UPDATE y DELETE), y la escritura atómica de
`VerificacionConsentimientoRegistrada` en el outbox. El límite exacto de la vigencia se prueba en
el dominio (`test_verificacion.py`): el reloj de la base no se puede congelar.
"""

import asyncio
import json
import os
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from clientes.adapters.outbound.consentimientos_pg import ConsentimientosPg
from clientes.adapters.outbound.postgres import migrar
from clientes.domain.consentimientos.verificacion import (
    CausaRechazo,
    Decision,
    SolicitudVerificacion,
)
from jsonschema import Draft202012Validator
from psycopg_pool import AsyncConnectionPool

docker = pytest.importorskip("docker")

pytestmark = pytest.mark.anyio

PROPOSITO = "PERFILAMIENTO_PRECIO"
# Solo existe un propósito en el dominio: este valor sintético solo se siembra en la base (la
# columna no lleva CHECK) para que exista un ACTIVO "de otro propósito".
OTRO_PROPOSITO = "PROPOSITO_SINTETICO_DE_PRUEBA"
CUATRO = (
    "FA_PRODUCTOS_VIGENTES",
    "FA_HISTORIAL_PAGOS_12M",
    "FA_INGRESOS_AGREGADOS",
    "DA_REGISTROS_PUBLICOS",
)
SOLICITUD = SolicitudVerificacion.crear(PROPOSITO, CUATRO)
CORRELACION = "corr-modulo-w31"
ESQUEMA = json.loads(
    (
        Path(__file__).parents[4]
        / "contracts"
        / "events"
        / "VerificacionConsentimientoRegistrada.v1.schema.json"
    ).read_text("utf-8")
)
VALIDADOR = Draft202012Validator(ESQUEMA, format_checker=Draft202012Validator.FORMAT_CHECKER)


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
        yield url


@pytest.fixture(autouse=True)
def limpia(base: str) -> None:
    with psycopg.connect(base, autocommit=True) as conexion:
        conexion.execute("TRUNCATE consentimiento_idempotencia, consentimiento, outbox, cliente")


@pytest.fixture
async def repo(base: str) -> AsyncIterator[ConsentimientosPg]:
    async with AsyncConnectionPool(base, min_size=1, max_size=4, open=False) as pool:
        yield ConsentimientosPg(pool)


def _sql(base: str, sentencia: str, parametros: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with psycopg.connect(base, autocommit=True) as conexion:
        cursor = conexion.execute(sentencia, parametros)
        return cursor.fetchall() if cursor.description else []


def _cliente(base: str) -> UUID:
    cliente_id = uuid4()
    _sql(
        base,
        "INSERT INTO cliente (id, sub, estado) VALUES (%s, %s, 'VERIFICADO')",
        (cliente_id, f"sub-{cliente_id}"),
    )
    return cliente_id


def _sembrar(
    base: str,
    cliente_id: UUID,
    *,
    estado: str = "ACTIVO",
    proposito: str = PROPOSITO,
    alcances: tuple[str, ...] = CUATRO,
    otorgado_hace: timedelta = timedelta(days=30),
    vigente_por: timedelta = timedelta(days=335),
) -> UUID:
    """Inserta una fila con instantes relativos al reloj de la base; `vigente_por` negativo deja
    el consentimiento ya vencido."""
    consentimiento_id = uuid4()
    _sql(
        base,
        "WITH t AS MATERIALIZED (SELECT clock_timestamp() AS ahora) "
        "INSERT INTO consentimiento (id, cliente_id, proposito, alcances, version_texto, idioma, "
        "estado, otorgado_en, vigente_hasta, revocado_en) "
        "SELECT %s, %s, %s, %s, 'v-modulo', 'es-CO', %s, t.ahora - %s, t.ahora + %s, "
        "CASE WHEN %s = 'REVOCADO' THEN t.ahora - interval '1 day' END FROM t",
        (
            consentimiento_id,
            cliente_id,
            proposito,
            list(alcances),
            estado,
            otorgado_hace,
            vigente_por,
            estado,
        ),
    )
    return consentimiento_id


def _outbox(base: str) -> list[tuple[Any, ...]]:
    return _sql(
        base,
        "SELECT event_id, aggregate_id, event_type, event_version, occurred_at, correlation_id, "
        "payload, published_at FROM outbox ORDER BY occurred_at",
    )


def _sobre(fila: tuple[Any, ...]) -> dict[str, Any]:
    """Lo que publicará el relay a partir de la fila (PR 2.2, aún sin construir)."""
    return {
        "eventId": str(fila[0]),
        "aggregateId": str(fila[1]),
        "type": fila[2],
        "version": fila[3],
        "occurredAt": fila[4].astimezone(UTC).isoformat(),
        "correlationId": fila[5],
        "payload": fila[6],
    }


async def test_vigente_permite_y_registra_la_decision_en_el_outbox(
    base: str, repo: ConsentimientosPg
) -> None:
    """AC1: PERMITIDO y una fila de outbox sin publicar, con el sobre del contrato."""
    cliente_id = _cliente(base)
    vigente = _sembrar(base, cliente_id)
    antes = datetime.now(UTC)

    verificacion = await repo.verificar(cliente_id, SOLICITUD, CORRELACION)

    assert verificacion is not None
    assert verificacion.decision is Decision.PERMITIDO
    assert verificacion.consentimiento_id == vigente
    assert verificacion.alcances_permitidos == CUATRO
    assert verificacion.vigente_hasta is not None
    assert verificacion.vigente_hasta.tzinfo is UTC
    # El instante es el de la base; los relojes de la máquina y del contenedor coinciden con margen.
    assert abs(verificacion.verificado_en - antes) < timedelta(seconds=30)
    (fila,) = _outbox(base)
    assert fila[1:4] == (cliente_id, "VerificacionConsentimientoRegistrada", 1)
    assert fila[4] == verificacion.verificado_en  # la misma captura del reloj que la lectura
    assert fila[5] == CORRELACION
    assert fila[7] is None  # published_at: sin relay todavía
    VALIDADOR.validate(_sobre(fila))
    assert fila[6] == {
        "clienteId": str(cliente_id),
        "consentimientoId": str(vigente),
        "proposito": PROPOSITO,
        "alcancesSolicitados": list(CUATRO),
        "decision": "PERMITIDO",
        "causa": None,
        "alcancesRechazados": [],
        "verificadoEn": verificacion.verificado_en.isoformat(),
    }


async def test_alcance_no_incluido_permite_parcial_y_registra_el_faltante(
    base: str, repo: ConsentimientosPg
) -> None:
    """AC3: solo ese acceso se rechaza y el registro guarda el alcance faltante."""
    cliente_id = _cliente(base)
    _sembrar(base, cliente_id, alcances=tuple(a for a in CUATRO if a != "FA_INGRESOS_AGREGADOS"))

    verificacion = await repo.verificar(cliente_id, SOLICITUD, CORRELACION)

    assert verificacion is not None
    assert verificacion.decision is Decision.PERMITIDO_PARCIAL
    assert verificacion.alcances_rechazados == ("FA_INGRESOS_AGREGADOS",)
    (fila,) = _outbox(base)
    VALIDADOR.validate(_sobre(fila))
    assert (fila[6]["decision"], fila[6]["alcancesRechazados"]) == (
        "PERMITIDO_PARCIAL",
        ["FA_INGRESOS_AGREGADOS"],
    )


@pytest.mark.parametrize(
    ("sembrado", "causa"),
    [
        ({"estado": "REVOCADO"}, CausaRechazo.REVOCADO),
        ({"estado": "VENCIDO", "vigente_por": -timedelta(days=1)}, CausaRechazo.VENCIDO),
        # ACTIVO almacenado y ya vencido, sin materializar: se deriva al leer (ADR 0009).
        ({"vigente_por": -timedelta(seconds=1)}, CausaRechazo.VENCIDO),
    ],
    ids=["revocado", "vencido-materializado", "vencido-derivado"],
)
async def test_no_vigente_rechaza_con_su_causa_y_registra_el_evaluado(
    base: str, repo: ConsentimientosPg, sembrado: dict[str, Any], causa: CausaRechazo
) -> None:
    """AC2: RECHAZADO con causa estable; la respuesta no lleva el id, el registro sí."""
    cliente_id = _cliente(base)
    evaluado = _sembrar(base, cliente_id, **sembrado)

    verificacion = await repo.verificar(cliente_id, SOLICITUD, CORRELACION)

    assert verificacion is not None
    assert (verificacion.decision, verificacion.causa) == (Decision.RECHAZADO, causa)
    assert (verificacion.consentimiento_id, verificacion.vigente_hasta) == (None, None)
    assert verificacion.alcances_rechazados == CUATRO
    (fila,) = _outbox(base)
    VALIDADOR.validate(_sobre(fila))
    assert fila[6]["consentimientoId"] == str(evaluado)
    assert fila[6]["causa"] == causa.value


async def test_un_activo_vencido_no_se_materializa_al_verificar(
    base: str, repo: ConsentimientosPg
) -> None:
    """Verificar solo lee la fila: el estado almacenado sigue `ACTIVO` (se materializa al otorgar)."""
    cliente_id = _cliente(base)
    vencido = _sembrar(base, cliente_id, vigente_por=-timedelta(seconds=1))

    await repo.verificar(cliente_id, SOLICITUD, CORRELACION)

    assert _sql(base, "SELECT estado FROM consentimiento WHERE id = %s", (vencido,)) == [
        ("ACTIVO",)
    ]


async def test_el_mas_reciente_del_proposito_decide(base: str, repo: ConsentimientosPg) -> None:
    """Revocado y reotorgado: decide el vigente nuevo, no el revocado anterior."""
    cliente_id = _cliente(base)
    _sembrar(base, cliente_id, estado="REVOCADO", otorgado_hace=timedelta(days=60))
    nuevo = _sembrar(base, cliente_id, otorgado_hace=timedelta(days=1))

    verificacion = await repo.verificar(cliente_id, SOLICITUD, CORRELACION)

    assert verificacion is not None
    assert (verificacion.decision, verificacion.consentimiento_id) == (Decision.PERMITIDO, nuevo)


async def test_revocado_despues_de_otro_vencido_rechaza_como_revocado(
    base: str, repo: ConsentimientosPg
) -> None:
    cliente_id = _cliente(base)
    _sembrar(
        base,
        cliente_id,
        estado="VENCIDO",
        otorgado_hace=timedelta(days=400),
        vigente_por=-timedelta(days=35),
    )
    revocado = _sembrar(base, cliente_id, estado="REVOCADO", otorgado_hace=timedelta(days=10))

    verificacion = await repo.verificar(cliente_id, SOLICITUD, CORRELACION)

    assert verificacion is not None
    assert verificacion.causa is CausaRechazo.REVOCADO
    assert verificacion.consentimiento_evaluado == revocado


async def test_sin_consentimientos_rechaza_sin_consentimiento(
    base: str, repo: ConsentimientosPg
) -> None:
    cliente_id = _cliente(base)

    verificacion = await repo.verificar(cliente_id, SOLICITUD, CORRELACION)

    assert verificacion is not None
    assert verificacion.causa is CausaRechazo.SIN_CONSENTIMIENTO
    (fila,) = _outbox(base)
    VALIDADOR.validate(_sobre(fila))
    assert fila[6]["consentimientoId"] is None


@pytest.mark.parametrize(
    ("vigente_por", "causa"),
    [
        (timedelta(days=10), CausaRechazo.PROPOSITO_NO_AUTORIZADO),
        (-timedelta(seconds=1), CausaRechazo.SIN_CONSENTIMIENTO),
    ],
    ids=["otro-proposito-vigente", "otro-proposito-vencido"],
)
async def test_solo_otro_proposito_distingue_proposito_no_autorizado(
    base: str, repo: ConsentimientosPg, vigente_por: timedelta, causa: CausaRechazo
) -> None:
    cliente_id = _cliente(base)
    _sembrar(base, cliente_id, proposito=OTRO_PROPOSITO, vigente_por=vigente_por)

    verificacion = await repo.verificar(cliente_id, SOLICITUD, CORRELACION)

    assert verificacion is not None
    assert verificacion.causa is causa
    assert verificacion.consentimiento_evaluado is None


async def test_el_consentimiento_de_otro_cliente_no_cuenta(
    base: str, repo: ConsentimientosPg
) -> None:
    """Aislamiento: el vigente de otro cliente no autoriza a este."""
    cliente_id = _cliente(base)
    _sembrar(base, _cliente(base))

    verificacion = await repo.verificar(cliente_id, SOLICITUD, CORRELACION)

    assert verificacion is not None
    assert verificacion.causa is CausaRechazo.SIN_CONSENTIMIENTO


async def test_cliente_inexistente_no_decide_ni_registra(
    base: str, repo: ConsentimientosPg
) -> None:
    assert await repo.verificar(uuid4(), SOLICITUD, CORRELACION) is None
    assert _outbox(base) == []


async def test_sin_cache_cada_verificacion_lee_y_registra(
    base: str, repo: ConsentimientosPg
) -> None:
    """ADR 0011: una revocación entre dos verificaciones se ve en la siguiente; cada verificación,
    aunque repita la decisión, deja su propio evento."""
    cliente_id = _cliente(base)
    consentimiento_id = _sembrar(base, cliente_id)

    primera = await repo.verificar(cliente_id, SOLICITUD, CORRELACION)
    repetida = await repo.verificar(cliente_id, SOLICITUD, CORRELACION)
    _sql(
        base,
        "UPDATE consentimiento SET estado = 'REVOCADO', revocado_en = clock_timestamp() "
        "WHERE id = %s",
        (consentimiento_id,),
    )
    tras_revocar = await repo.verificar(cliente_id, SOLICITUD, CORRELACION)

    assert primera is not None and repetida is not None and tras_revocar is not None
    assert [primera.decision, repetida.decision, tras_revocar.decision] == [
        Decision.PERMITIDO,
        Decision.PERMITIDO,
        Decision.RECHAZADO,
    ]
    assert tras_revocar.causa is CausaRechazo.REVOCADO
    filas = _outbox(base)
    assert [f[6]["decision"] for f in filas] == ["PERMITIDO", "PERMITIDO", "RECHAZADO"]
    assert len({f[0] for f in filas}) == 3  # un eventId distinto por verificación


async def test_si_el_outbox_falla_no_hay_decision_ni_registro(
    base: str, repo: ConsentimientosPg
) -> None:
    """Lectura y evento en la misma transacción: sin registro no se responde ninguna decisión."""
    cliente_id = _cliente(base)
    _sembrar(base, cliente_id)
    _sql(
        base,
        "CREATE FUNCTION rechazar_outbox() RETURNS trigger LANGUAGE plpgsql AS "
        "$$ BEGIN RAISE EXCEPTION 'outbox no disponible'; END $$",
    )
    _sql(
        base,
        "CREATE TRIGGER rechazar_outbox BEFORE INSERT ON outbox "
        "FOR EACH ROW EXECUTE FUNCTION rechazar_outbox()",
    )
    try:
        with pytest.raises(psycopg.errors.RaiseException):
            await repo.verificar(cliente_id, SOLICITUD, CORRELACION)
    finally:
        _sql(base, "DROP TRIGGER rechazar_outbox ON outbox")
        _sql(base, "DROP FUNCTION rechazar_outbox()")

    assert _outbox(base) == []
    # La conexión vuelve sana al pool: la siguiente verificación decide y registra.
    assert await repo.verificar(cliente_id, SOLICITUD, CORRELACION) is not None
    assert len(_outbox(base)) == 1


async def test_verificaciones_concurrentes_registran_una_fila_cada_una(
    base: str, repo: ConsentimientosPg
) -> None:
    cliente_id = _cliente(base)
    _sembrar(base, cliente_id)

    resultados = await asyncio.gather(
        *(repo.verificar(cliente_id, SOLICITUD, CORRELACION) for _ in range(8))
    )

    assert all(r is not None and r.decision is Decision.PERMITIDO for r in resultados)
    assert len(_outbox(base)) == 8
