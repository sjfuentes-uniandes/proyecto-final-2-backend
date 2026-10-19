"""Pruebas de módulo de la API de consentimientos: la app real contra un Postgres real
(HU-M08, PR 1.4; revocación e historial de HU-M09, PR 2.4).

La app se ensambla como en producción (sin repositorios inyectados): migra, abre el
`AsyncConnectionPool` en el ciclo de vida y lo cierra al apagar. Solo se sustituye el transporte
hacia `adaptador-identidad`, que otorgar no usa.
"""

import asyncio
import json
import os
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from clientes.adapters.outbound.consentimientos_pg import ConsentimientosPg
from clientes.config import Settings
from clientes.main import crear_app
from fastapi import FastAPI
from jsonschema import Draft202012Validator

docker = pytest.importorskip("docker")

pytestmark = pytest.mark.anyio

RUTA = "/v1/clientes/actual/consentimientos"
CORRELACION = "corr-modulo-m08"
SUB = "sub-titular-a"
OTRO_SUB = "sub-titular-b"
EVENTOS = Path(__file__).parents[4] / "contracts" / "events"
ESQUEMA_EVENTO = json.loads((EVENTOS / "ConsentimientoOtorgado.v1.schema.json").read_text("utf-8"))
ESQUEMA_REVOCADO = json.loads(
    (EVENTOS / "ConsentimientoRevocado.v1.schema.json").read_text("utf-8")
)


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
        yield contenedor.get_connection_url()


@pytest.fixture(autouse=True)
def limpia(base: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from clientes.adapters.outbound.postgres import migrar

    monkeypatch.setenv("SOLVENTA_DATABASE_URL", base)
    migrar(base)
    with psycopg.connect(base, autocommit=True) as conexion:
        conexion.execute("TRUNCATE consentimiento_idempotencia, consentimiento, outbox, cliente")


def _ensamblar(**ajustes: Any) -> FastAPI:
    async def sin_llamadas(request: httpx.Request) -> httpx.Response:
        raise AssertionError("otorgar no invoca a adaptador-identidad")

    return crear_app(Settings(**ajustes), transport=httpx.MockTransport(sin_llamadas))


@pytest.fixture
async def app() -> AsyncIterator[FastAPI]:
    aplicacion = _ensamblar()
    async with aplicacion.router.lifespan_context(aplicacion):
        yield aplicacion


@pytest.fixture
async def api(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://clientes",
        headers={"X-Correlation-Id": CORRELACION},
    ) as cliente:
        yield cliente


def _sql(base: str, sentencia: str, parametros: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with psycopg.connect(base, autocommit=True) as conexion:
        cursor = conexion.execute(sentencia, parametros)
        return cursor.fetchall() if cursor.description else []


def _titular(base: str, sub: str = SUB, estado: str = "VERIFICADO") -> UUID:
    cliente_id = uuid4()
    _sql(
        base,
        "INSERT INTO cliente (id, sub, estado, verificado_en) VALUES (%s, %s, %s, %s)",
        (cliente_id, sub, estado, datetime.now(UTC) if estado == "VERIFICADO" else None),
    )
    return cliente_id


def _actor(sub: str = SUB) -> dict[str, str]:
    return {"X-Actor-Id": sub, "X-Actor-Rol": "CLIENTE"}


def _con_clave(clave: UUID | None = None, sub: str = SUB) -> dict[str, str]:
    return {**_actor(sub), "Idempotency-Key": str(clave or uuid4())}


async def _cuerpo(api: httpx.AsyncClient, **cambios: Any) -> dict[str, Any]:
    texto = (
        await api.get(
            "/v1/consentimientos/textos/vigente",
            params={"proposito": "PERFILAMIENTO_PRECIO", "idioma": "es-CO"},
        )
    ).json()
    return {
        "proposito": "PERFILAMIENTO_PRECIO",
        "alcances": ["FA_PRODUCTOS_VIGENTES", "DA_REGISTROS_PUBLICOS"],
        "vigenciaMeses": 12,
        "versionTexto": texto["versionTexto"],
        "idioma": "es-CO",
        **cambios,
    }


def _conteos(base: str) -> tuple[int, int, int]:
    return (
        _sql(base, "SELECT count(*) FROM consentimiento")[0][0],
        _sql(base, "SELECT count(*) FROM consentimiento_idempotencia")[0][0],
        _sql(base, "SELECT count(*) FROM outbox")[0][0],
    )


async def test_otorgar_persiste_consentimiento_clave_y_evento(
    api: httpx.AsyncClient, base: str
) -> None:
    """R8 feliz: titular VERIFICADO otorga y lo consulta como ACTIVO con su texto y vencimiento."""
    cliente_id = _titular(base)
    cuerpo = await _cuerpo(api)

    respuesta = await api.post(RUTA, json=cuerpo, headers=_con_clave())

    assert respuesta.status_code == 201, respuesta.text
    otorgado = respuesta.json()
    assert otorgado["estado"] == "ACTIVO"
    assert otorgado["versionTexto"] == cuerpo["versionTexto"]
    assert otorgado["alcances"] == ["DA_REGISTROS_PUBLICOS", "FA_PRODUCTOS_VIGENTES"]
    assert otorgado["revocadoEn"] is None
    consultado = await api.get(f"{RUTA}/{otorgado['consentimientoId']}", headers=_actor())
    assert consultado.status_code == 200
    assert consultado.json() == otorgado
    assert (await api.get(RUTA, headers=_actor())).json() == [otorgado]

    ((fila_cliente, estado),) = _sql(base, "SELECT cliente_id, estado FROM consentimiento")
    assert (fila_cliente, estado) == (cliente_id, "ACTIVO")
    # Dato, clave y evento salieron de la misma transacción; el evento aún no se publica.
    assert _conteos(base) == (1, 1, 1)
    ((tipo, version, agregado, correlacion, publicado, payload, ocurrido),) = _sql(
        base,
        "SELECT event_type, event_version, aggregate_id, correlation_id, published_at, payload, "
        "occurred_at FROM outbox",
    )
    assert (tipo, version, str(agregado), correlacion) == (
        "ConsentimientoOtorgado",
        1,
        otorgado["consentimientoId"],
        CORRELACION,
    )
    assert publicado is None
    assert payload["clienteId"] == str(cliente_id)
    assert payload["actorId"] == SUB
    assert payload["otorgadoEn"].replace("+00:00", "Z") == otorgado["otorgadoEn"]
    assert payload["vigenteHasta"].replace("+00:00", "Z") == otorgado["vigenteHasta"]
    Draft202012Validator(
        ESQUEMA_EVENTO, format_checker=Draft202012Validator.FORMAT_CHECKER
    ).validate(
        {
            "eventId": str(_sql(base, "SELECT event_id FROM outbox")[0][0]),
            "aggregateId": str(agregado),
            "type": tipo,
            "version": version,
            "occurredAt": ocurrido.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "correlationId": correlacion,
            "aggregateVersion": 1,  # ADR 0009: lo deriva el relay del tipo; no es columna
            "payload": payload,
        }
    )


async def test_la_vigencia_la_calcula_la_base(api: httpx.AsyncClient, base: str) -> None:
    _titular(base)
    otorgado = (
        await api.post(RUTA, json=await _cuerpo(api, vigenciaMeses=6), headers=_con_clave())
    ).json()
    ((otorgado_en, vigente_hasta, calculada),) = _sql(
        base,
        "SELECT otorgado_en, vigente_hasta, fin_de_vigencia(otorgado_en, 6) FROM consentimiento",
    )
    assert vigente_hasta == calculada
    assert otorgado["vigenteHasta"] == vigente_hasta.astimezone(UTC).isoformat().replace(
        "+00:00", "Z"
    )
    assert timedelta(days=180) <= vigente_hasta - otorgado_en <= timedelta(days=185)


async def test_repeticion_con_la_misma_clave_devuelve_el_mismo_consentimiento(
    api: httpx.AsyncClient, base: str
) -> None:
    """R8 fallo controlado (AC4): el reintento no duplica ni choca con el 409."""
    _titular(base)
    cuerpo, clave = await _cuerpo(api), uuid4()

    primero = await api.post(RUTA, json=cuerpo, headers=_con_clave(clave))
    segundo = await api.post(RUTA, json=cuerpo, headers=_con_clave(clave))

    assert (primero.status_code, segundo.status_code) == (201, 200)
    assert segundo.headers["Idempotent-Replayed"] == "true"
    assert segundo.json() == primero.json()
    assert _conteos(base) == (1, 1, 1)


async def test_misma_clave_con_otro_cuerpo_es_un_conflicto(
    api: httpx.AsyncClient, base: str
) -> None:
    _titular(base)
    clave = uuid4()
    await api.post(RUTA, json=await _cuerpo(api), headers=_con_clave(clave))

    respuesta = await api.post(
        RUTA, json=await _cuerpo(api, alcances=["FA_INGRESOS_AGREGADOS"]), headers=_con_clave(clave)
    )

    assert respuesta.status_code == 422
    assert respuesta.json()["codigo"] == "IDEMPOTENCIA_CONFLICTO"
    assert _conteos(base) == (1, 1, 1)


async def test_otra_clave_con_uno_vigente_es_409_con_el_existente(
    api: httpx.AsyncClient, base: str
) -> None:
    _titular(base)
    primero = (await api.post(RUTA, json=await _cuerpo(api), headers=_con_clave())).json()

    respuesta = await api.post(RUTA, json=await _cuerpo(api), headers=_con_clave())

    assert respuesta.status_code == 409
    assert respuesta.json()["codigo"] == "CONSENTIMIENTO_YA_VIGENTE"
    assert respuesta.json()["detalles"] == {"consentimientoId": primero["consentimientoId"]}
    assert _conteos(base) == (1, 1, 1)


async def test_solicitudes_concurrentes_con_la_misma_clave_crean_uno_solo(
    api: httpx.AsyncClient, base: str
) -> None:
    _titular(base)
    cuerpo, clave = await _cuerpo(api), uuid4()

    respuestas = await asyncio.gather(
        *(api.post(RUTA, json=cuerpo, headers=_con_clave(clave)) for _ in range(6))
    )

    assert sorted(r.status_code for r in respuestas) == [200] * 5 + [201]
    assert len({r.json()["consentimientoId"] for r in respuestas}) == 1
    assert _conteos(base) == (1, 1, 1)


async def test_solicitudes_concurrentes_con_claves_distintas_dejan_un_solo_activo(
    api: httpx.AsyncClient, base: str
) -> None:
    _titular(base)
    cuerpo = await _cuerpo(api)

    respuestas = await asyncio.gather(
        *(api.post(RUTA, json=cuerpo, headers=_con_clave()) for _ in range(6))
    )

    assert sorted(r.status_code for r in respuestas) == [201] + [409] * 5
    assert {r.json()["codigo"] for r in respuestas if r.status_code == 409} == {
        "CONSENTIMIENTO_YA_VIGENTE"
    }
    assert _conteos(base) == (1, 1, 1)


async def test_solicitud_sin_vigencia_es_422_y_no_deja_rastro(
    api: httpx.AsyncClient, base: str
) -> None:
    """R8 fallo controlado (AC3): sin consentimiento, sin clave y sin evento."""
    _titular(base)
    cuerpo = await _cuerpo(api)
    del cuerpo["vigenciaMeses"]

    respuesta = await api.post(RUTA, json=cuerpo, headers=_con_clave())

    assert respuesta.status_code == 422
    assert respuesta.json()["codigo"] == "SOLICITUD_INVALIDA"
    assert respuesta.json()["detalles"] == {"campos": ["vigenciaMeses"]}
    assert _conteos(base) == (0, 0, 0)


@pytest.mark.parametrize(
    ("cambio", "codigo"),
    [
        ({"vigenciaMeses": 3}, "VIGENCIA_INVALIDA"),
        ({"versionTexto": "1999-01-01.1"}, "TEXTO_NO_VIGENTE"),
    ],
)
async def test_vigencia_y_texto_invalidos_no_dejan_rastro(
    api: httpx.AsyncClient, base: str, cambio: dict[str, Any], codigo: str
) -> None:
    _titular(base)

    respuesta = await api.post(RUTA, json=await _cuerpo(api, **cambio), headers=_con_clave())

    assert (respuesta.status_code, respuesta.json()["codigo"]) == (422, codigo)
    assert _conteos(base) == (0, 0, 0)


async def test_identidad_no_verificada_es_403_y_no_deja_rastro(
    api: httpx.AsyncClient, base: str
) -> None:
    _titular(base, estado="PENDIENTE")

    respuesta = await api.post(RUTA, json=await _cuerpo(api), headers=_con_clave())

    assert (respuesta.status_code, respuesta.json()["codigo"]) == (403, "IDENTIDAD_NO_VERIFICADA")
    assert _conteos(base) == (0, 0, 0)


async def test_titular_sin_cliente(api: httpx.AsyncClient, base: str) -> None:
    otorgar = await api.post(RUTA, json=await _cuerpo(api), headers=_con_clave())
    lista = await api.get(RUTA, headers=_actor())
    detalle = await api.get(f"{RUTA}/{uuid4()}", headers=_actor())

    assert (otorgar.status_code, otorgar.json()["codigo"]) == (404, "CLIENTE_NO_ENCONTRADO")
    assert (lista.status_code, lista.json()) == (200, [])
    assert (detalle.status_code, detalle.json()["codigo"]) == (404, "CONSENTIMIENTO_NO_ENCONTRADO")
    assert _conteos(base) == (0, 0, 0)


async def test_un_titular_no_ve_ni_consulta_los_consentimientos_de_otro(
    api: httpx.AsyncClient, base: str
) -> None:
    _titular(base, SUB)
    _titular(base, OTRO_SUB)
    ajeno = (await api.post(RUTA, json=await _cuerpo(api), headers=_con_clave(sub=OTRO_SUB))).json()

    consulta = await api.get(f"{RUTA}/{ajeno['consentimientoId']}", headers=_actor(SUB))
    inexistente = await api.get(f"{RUTA}/{uuid4()}", headers=_actor(SUB))

    assert consulta.status_code == 404
    assert consulta.json()["codigo"] == "CONSENTIMIENTO_NO_ENCONTRADO"
    assert consulta.json()["detalles"] == inexistente.json()["detalles"] == {}
    assert consulta.json()["mensaje"] == inexistente.json()["mensaje"]
    assert (await api.get(RUTA, headers=_actor(SUB))).json() == []
    assert (await api.get(RUTA, headers=_actor(OTRO_SUB))).json() == [ajeno]
    # El dueño sigue viéndolo y el intento ajeno no lo tocó.
    propio = await api.get(f"{RUTA}/{ajeno['consentimientoId']}", headers=_actor(OTRO_SUB))
    assert propio.json() == ajeno


async def test_la_clave_de_idempotencia_es_por_cliente(api: httpx.AsyncClient, base: str) -> None:
    _titular(base, SUB)
    _titular(base, OTRO_SUB)
    clave, cuerpo = uuid4(), await _cuerpo(api)

    mio = await api.post(RUTA, json=cuerpo, headers=_con_clave(clave, SUB))
    suyo = await api.post(RUTA, json=cuerpo, headers=_con_clave(clave, OTRO_SUB))

    assert (mio.status_code, suyo.status_code) == (201, 201)
    assert mio.json()["consentimientoId"] != suyo.json()["consentimientoId"]
    assert _conteos(base) == (2, 2, 2)


async def test_un_activo_vencido_se_lee_como_vencido_y_se_libera_al_reotorgar(
    api: httpx.AsyncClient, base: str
) -> None:
    cliente_id = _titular(base)
    vencido = uuid4()
    ahora = datetime.now(UTC)
    _sql(
        base,
        "INSERT INTO consentimiento (id, cliente_id, proposito, alcances, version_texto, idioma, "
        "estado, otorgado_en, vigente_hasta) VALUES (%s, %s, 'PERFILAMIENTO_PRECIO', "
        "ARRAY['FA_PRODUCTOS_VIGENTES'], 'v0', 'es-CO', 'ACTIVO', %s, %s)",
        (vencido, cliente_id, ahora - timedelta(days=400), ahora - timedelta(days=35)),
    )

    antes = await api.get(f"{RUTA}/{vencido}", headers=_actor())
    nuevo = await api.post(RUTA, json=await _cuerpo(api), headers=_con_clave())
    lista = await api.get(RUTA, headers=_actor())

    assert antes.json()["estado"] == "VENCIDO"
    assert nuevo.status_code == 201
    assert [c["estado"] for c in lista.json()] == ["ACTIVO", "VENCIDO"]
    assert _sql(base, "SELECT estado FROM consentimiento WHERE id = %s", (vencido,)) == [
        ("VENCIDO",)
    ]


async def test_texto_vigente_sirve_el_archivo_versionado(api: httpx.AsyncClient) -> None:
    respuesta = await api.get(
        "/v1/consentimientos/textos/vigente",
        params={"proposito": "PERFILAMIENTO_PRECIO", "idioma": "en-US"},
    )

    assert respuesta.status_code == 200
    assert respuesta.json()["vigenciasMeses"] == [6, 12]
    assert respuesta.json()["aviso"] == "Sample text, no legal validity."


async def test_el_pool_se_abre_con_el_tamano_configurado_y_se_cierra_al_apagar() -> None:
    aplicacion = _ensamblar(db_pool_min_size=2, db_pool_size=3)

    async with aplicacion.router.lifespan_context(aplicacion):
        repositorio = aplicacion.state.otorgar_consentimiento._repositorio
        assert isinstance(repositorio, ConsentimientosPg)
        pool = repositorio._pool
        assert (pool.min_size, pool.max_size, pool.closed) == (2, 3, False)

    assert pool.closed


# --- Revocación e historial (HU-M09, PR 2.4) ---------------------------------------------------


def _z(instante: datetime) -> str:
    return instante.astimezone(UTC).isoformat().replace("+00:00", "Z")


async def _otorgado(api: httpx.AsyncClient, sub: str = SUB) -> dict[str, Any]:
    respuesta = await api.post(RUTA, json=await _cuerpo(api), headers=_con_clave(sub=sub))
    assert respuesta.status_code == 201, respuesta.text
    return respuesta.json()


async def test_otorgar_revocar_y_consultar_el_historial(api: httpx.AsyncClient, base: str) -> None:
    """AC1 y AC4 de punta a punta en `clientes` (el R8 llega en la PR 2.6): el estado, el
    detalle, la lista y el historial coinciden con la fila, y el evento cumple su esquema."""
    cliente_id = _titular(base)
    otorgado = await _otorgado(api)
    consentimiento_id = otorgado["consentimientoId"]

    respuesta = await api.post(f"{RUTA}/{consentimiento_id}/revocacion", headers=_actor())

    assert respuesta.status_code == 200, respuesta.text
    revocado = respuesta.json()
    assert revocado["estado"] == "REVOCADO"
    ((estado, revocado_en),) = _sql(
        base, "SELECT estado, revocado_en FROM consentimiento WHERE id = %s", (consentimiento_id,)
    )
    assert (estado, revocado["revocadoEn"]) == ("REVOCADO", _z(revocado_en))
    assert (await api.get(f"{RUTA}/{consentimiento_id}", headers=_actor())).json() == revocado
    assert (await api.get(RUTA, headers=_actor())).json() == [revocado]
    historial = (await api.get(f"{RUTA}/{consentimiento_id}/historial", headers=_actor())).json()
    assert historial == {
        "consentimientoId": consentimiento_id,
        "estado": "REVOCADO",
        "hitos": [
            {"tipo": "OTORGADO", "ocurridoEn": otorgado["otorgadoEn"]},
            {"tipo": "REVOCADO", "ocurridoEn": revocado["revocadoEn"]},
        ],
    }

    ((event_id, tipo, version, agregado, correlacion, publicado, payload, ocurrido),) = _sql(
        base,
        "SELECT event_id, event_type, event_version, aggregate_id, correlation_id, published_at, "
        "payload, occurred_at FROM outbox WHERE event_type = 'ConsentimientoRevocado'",
    )
    assert (str(agregado), correlacion, publicado, ocurrido) == (
        consentimiento_id,
        CORRELACION,
        None,
        revocado_en,
    )
    assert payload == {
        "consentimientoId": consentimiento_id,
        "clienteId": str(cliente_id),
        "revocadoEn": revocado_en.isoformat(),
        "actorId": SUB,
    }
    Draft202012Validator(
        ESQUEMA_REVOCADO, format_checker=Draft202012Validator.FORMAT_CHECKER
    ).validate(
        {
            "eventId": str(event_id),
            "aggregateId": str(agregado),
            "type": tipo,
            "version": version,
            "occurredAt": _z(ocurrido),
            "correlationId": correlacion,
            "aggregateVersion": 2,  # ADR 0009: lo deriva el relay del tipo; no es columna
            "payload": payload,
        }
    )


async def test_revocar_otra_vez_responde_igual_sin_otro_evento(
    api: httpx.AsyncClient, base: str
) -> None:
    """AC3: mismo estado y un solo evento."""
    _titular(base)
    otorgado = await _otorgado(api)
    ruta = f"{RUTA}/{otorgado['consentimientoId']}/revocacion"
    primera = await api.post(ruta, headers=_actor())
    segunda = await api.post(ruta, headers=_actor())
    assert (primera.status_code, segunda.status_code) == (200, 200)
    assert segunda.json() == primera.json()
    assert _sql(
        base, "SELECT count(*) FROM outbox WHERE event_type = 'ConsentimientoRevocado'"
    ) == [(1,)]


async def test_revocaciones_concurrentes_por_la_api_dejan_un_solo_evento(
    api: httpx.AsyncClient, base: str
) -> None:
    _titular(base)
    otorgado = await _otorgado(api)
    ruta = f"{RUTA}/{otorgado['consentimientoId']}/revocacion"
    respuestas = await asyncio.gather(*(api.post(ruta, headers=_actor()) for _ in range(6)))
    assert {r.status_code for r in respuestas} == {200}
    assert len({r.json()["revocadoEn"] for r in respuestas}) == 1
    assert _sql(
        base, "SELECT count(*) FROM outbox WHERE event_type = 'ConsentimientoRevocado'"
    ) == [(1,)]


async def test_revocar_un_activo_vencido_responde_vencido_sin_tocar_la_fila(
    api: httpx.AsyncClient, base: str
) -> None:
    cliente_id = _titular(base)
    vencido = uuid4()
    ahora = datetime.now(UTC)
    _sql(
        base,
        "INSERT INTO consentimiento (id, cliente_id, proposito, alcances, version_texto, idioma, "
        "estado, otorgado_en, vigente_hasta) VALUES (%s, %s, 'PERFILAMIENTO_PRECIO', "
        "ARRAY['FA_PRODUCTOS_VIGENTES'], 'v0', 'es-CO', 'ACTIVO', %s, %s)",
        (vencido, cliente_id, ahora - timedelta(days=400), ahora - timedelta(days=35)),
    )
    respuesta = await api.post(f"{RUTA}/{vencido}/revocacion", headers=_actor())
    assert respuesta.status_code == 200
    assert (respuesta.json()["estado"], respuesta.json()["revocadoEn"]) == ("VENCIDO", None)
    historial = (await api.get(f"{RUTA}/{vencido}/historial", headers=_actor())).json()
    assert [h["tipo"] for h in historial["hitos"]] == ["OTORGADO", "VENCIDO"]
    assert _sql(base, "SELECT estado, revocado_en FROM consentimiento") == [("ACTIVO", None)]
    assert _conteos(base) == (1, 0, 0)


async def test_un_titular_no_revoca_ni_ve_el_historial_de_otro(
    api: httpx.AsyncClient, base: str
) -> None:
    _titular(base, SUB)
    _titular(base, OTRO_SUB)
    ajeno = await _otorgado(api, OTRO_SUB)
    for respuesta in (
        await api.post(f"{RUTA}/{ajeno['consentimientoId']}/revocacion", headers=_actor()),
        await api.get(f"{RUTA}/{ajeno['consentimientoId']}/historial", headers=_actor()),
        await api.post(f"{RUTA}/{uuid4()}/revocacion", headers=_actor()),
    ):
        assert respuesta.status_code == 404
        assert respuesta.json()["codigo"] == "CONSENTIMIENTO_NO_ENCONTRADO"
    assert _sql(base, "SELECT estado FROM consentimiento") == [("ACTIVO",)]
    assert _sql(
        base, "SELECT count(*) FROM outbox WHERE event_type = 'ConsentimientoRevocado'"
    ) == [(0,)]


async def test_tras_revocar_se_otorga_uno_nuevo_y_la_lista_conserva_ambos(
    api: httpx.AsyncClient, base: str
) -> None:
    """La lista de la Parte 1 sigue siendo un arreglo e incluye el revocado (compatibilidad N-1)."""
    _titular(base)
    primero = await _otorgado(api)
    await api.post(f"{RUTA}/{primero['consentimientoId']}/revocacion", headers=_actor())
    segundo = await _otorgado(api)
    lista = (await api.get(RUTA, headers=_actor())).json()
    assert [(c["consentimientoId"], c["estado"]) for c in lista] == [
        (segundo["consentimientoId"], "ACTIVO"),
        (primero["consentimientoId"], "REVOCADO"),
    ]


ASESOR = {"X-Actor-Id": "asesor-sintetico", "X-Actor-Rol": "ASESOR"}


def _web(cliente_id: Any) -> str:
    return f"/v1/clientes/{cliente_id}/consentimientos"


async def test_lectura_web_deriva_el_vencido_con_el_reloj_de_la_base_sin_escribir(
    api: httpx.AsyncClient, base: str
) -> None:
    """HU-W31, PR 3.3: el asesor lee por `clienteId`. Un `ACTIVO` almacenado cuyo `vigente_hasta`
    ya pasó se lee `VENCIDO` (ADR 0009), sin materializarlo ni escribir en el outbox."""
    cliente_id = _titular(base)
    revocado = await _otorgado(api)
    await api.post(f"{RUTA}/{revocado['consentimientoId']}/revocacion", headers=_actor())
    vencido = uuid4()
    ahora = datetime.now(UTC)
    _sql(
        base,
        "INSERT INTO consentimiento (id, cliente_id, proposito, alcances, version_texto, idioma, "
        "estado, otorgado_en, vigente_hasta) VALUES (%s, %s, 'PERFILAMIENTO_PRECIO', "
        "ARRAY['FA_PRODUCTOS_VIGENTES'], 'v0', 'es-CO', 'ACTIVO', %s, %s)",
        (vencido, cliente_id, ahora - timedelta(days=400), ahora - timedelta(seconds=1)),
    )
    antes = _conteos(base)

    lista = await api.get(_web(cliente_id), headers=ASESOR)
    historial = await api.get(f"{_web(cliente_id)}/{vencido}/historial", headers=ASESOR)

    assert lista.status_code == 200, lista.text
    assert [(c["consentimientoId"], c["estado"]) for c in lista.json()] == [
        (revocado["consentimientoId"], "REVOCADO"),
        (str(vencido), "VENCIDO"),
    ]
    assert lista.json() == (await api.get(RUTA, headers=_actor())).json()
    assert historial.status_code == 200, historial.text
    assert historial.json()["estado"] == "VENCIDO"
    assert [h["tipo"] for h in historial.json()["hitos"]] == ["OTORGADO", "VENCIDO"]
    assert _conteos(base) == antes
    assert _sql(base, "SELECT estado FROM consentimiento WHERE id = %s", (vencido,)) == [
        ("ACTIVO",)
    ]


async def test_lectura_web_aisla_clientes_y_exige_al_asesor(
    api: httpx.AsyncClient, base: str
) -> None:
    cliente_id = _titular(base, SUB)
    otro_id = _titular(base, OTRO_SUB)
    ajeno = await _otorgado(api, OTRO_SUB)

    assert (await api.get(_web(cliente_id), headers=ASESOR)).json() == []
    assert len((await api.get(_web(otro_id), headers=ASESOR)).json()) == 1
    for respuesta, codigo in (
        (await api.get(_web(uuid4()), headers=ASESOR), "CLIENTE_NO_ENCONTRADO"),
        (
            await api.get(
                f"{_web(cliente_id)}/{ajeno['consentimientoId']}/historial", headers=ASESOR
            ),
            "CONSENTIMIENTO_NO_ENCONTRADO",
        ),
    ):
        assert respuesta.status_code == 404
        assert respuesta.json()["codigo"] == codigo
    # Un titular no lee por `clienteId`, ni siquiera el propio (ADR 0006).
    propio = await api.get(_web(cliente_id), headers=_actor())
    assert (propio.status_code, propio.json()["codigo"]) == (403, "ROL_NO_AUTORIZADO")
