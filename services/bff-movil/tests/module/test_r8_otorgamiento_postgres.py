"""R8 (HU-M08), versión de módulo: otorgamiento de la app hasta la base de `clientes`.

Corre en CI sin AWS (decisión P1-5). Nada se simula entre la app y la base: `bff-movil` y
`clientes` reales sobre PostgreSQL real (infraestructura compartida en `conftest.py`). El titular
se siembra como fila de `cliente` `VERIFICADO`, como lo deja HU-M06. Solo datos sintéticos.

La versión para QA, contra el ambiente desplegado, está en `tests/e2e/test_hu_m08_r8_otorgamiento.py`.
"""

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from jsonschema import Draft202012Validator

SUB_A = "a8a8a8a8-0000-4000-8000-00000000000a"
SUB_B = "b8b8b8b8-0000-4000-8000-00000000000b"
RUTA = "/v1/consentimientos"
ESQUEMA_EVENTO = json.loads(
    (
        Path(__file__).parents[4] / "contracts" / "events" / "ConsentimientoOtorgado.v1.schema.json"
    ).read_text("utf-8")
)


# --- Infraestructura del recorrido (conftest.py) ---------------------------------------------

App = Any


@pytest.fixture
def app(clientes: Any, base: str, abrir_bff: Any) -> Iterator[App]:
    _sql(base, "TRUNCATE consentimiento_idempotencia, consentimiento, outbox, cliente")
    with abrir_bff(clientes.url, SUB_A) as aplicacion:
        yield aplicacion


def _sql(base: str, sentencia: str, parametros: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with psycopg.connect(base, autocommit=True) as conexion:
        cursor = conexion.execute(sentencia, parametros)
        return cursor.fetchall() if cursor.description else []


def _titular(base: str, sub: str = SUB_A, estado: str = "VERIFICADO") -> uuid.UUID:
    """El cliente que deja HU-M06 tras verificar la identidad."""
    cliente_id = uuid.uuid4()
    _sql(
        base,
        "INSERT INTO cliente (id, sub, estado, verificado_en) VALUES (%s, %s, %s, %s)",
        (cliente_id, sub, estado, datetime.now(UTC) if estado == "VERIFICADO" else None),
    )
    return cliente_id


def _rastro(base: str) -> tuple[int, int, int]:
    """(consentimientos, claves de idempotencia, filas de outbox)."""
    return (
        _sql(base, "SELECT count(*) FROM consentimiento")[0][0],
        _sql(base, "SELECT count(*) FROM consentimiento_idempotencia")[0][0],
        _sql(base, "SELECT count(*) FROM outbox")[0][0],
    )


# --- Caso feliz ------------------------------------------------------------------------------


@pytest.mark.hu("M08", "AC1")
def test_r8_feliz_otorgar_crea_consentimiento_y_evento_y_se_consulta(app: App, base: str) -> None:
    cliente_id = _titular(base)
    correlacion = "corr-r8-otorgar"
    cuerpo = app.cuerpo()

    respuesta = app.otorgar(cuerpo, **{"X-Correlation-Id": correlacion})

    assert respuesta.status_code == 201, respuesta.text
    otorgado = respuesta.json()
    assert otorgado["estado"] == "ACTIVO"
    assert otorgado["versionTexto"] == cuerpo["versionTexto"]
    assert otorgado["proposito"] == "PERFILAMIENTO_PRECIO"
    assert otorgado["alcances"] == ["FA_PRODUCTOS_VIGENTES"]
    assert otorgado["revocadoEn"] is None
    assert respuesta.headers["X-Correlation-Id"] == correlacion

    # En la base de `clientes`: consentimiento del titular del token, su clave y el evento,
    # escritos en la misma transacción; el evento aún no se publica (relay en la Parte 2).
    assert _rastro(base) == (1, 1, 1)
    ((fila_cliente, estado, version_texto),) = _sql(
        base, "SELECT cliente_id, estado, version_texto FROM consentimiento"
    )
    assert (fila_cliente, estado, version_texto) == (cliente_id, "ACTIVO", cuerpo["versionTexto"])
    ((evento_id, tipo, version, agregado, corr, publicado, payload, ocurrido),) = _sql(
        base,
        "SELECT event_id, event_type, event_version, aggregate_id, correlation_id, published_at, "
        "payload, occurred_at FROM outbox",
    )
    assert (tipo, version, str(agregado)) == (
        "ConsentimientoOtorgado",
        1,
        otorgado["consentimientoId"],
    )
    assert corr == correlacion, "el X-Correlation-Id de la app debe llegar al evento"
    assert publicado is None
    assert payload["clienteId"] == str(cliente_id)
    assert payload["actorId"] == SUB_A
    Draft202012Validator(
        ESQUEMA_EVENTO, format_checker=Draft202012Validator.FORMAT_CHECKER
    ).validate(
        {
            "eventId": str(evento_id),
            "aggregateId": str(agregado),
            "type": tipo,
            "version": version,
            "occurredAt": ocurrido.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "correlationId": corr,
            "payload": payload,
        }
    )

    # La app lo consulta después: lista y comprobante coinciden con lo otorgado.
    lista = app.pedir("GET", RUTA)
    detalle = app.pedir("GET", f"{RUTA}/{otorgado['consentimientoId']}")
    assert (lista.status_code, lista.json()) == (200, [otorgado])
    assert (detalle.status_code, detalle.json()) == (200, otorgado)


# --- Fallos controlados ----------------------------------------------------------------------


@pytest.mark.hu("M08", "AC3")
@pytest.mark.parametrize(
    ("cambios", "codigo", "campos"),
    [
        ({"vigenciaMeses": None}, "SOLICITUD_INVALIDA", {"vigenciaMeses"}),
        (
            {"proposito": None, "alcances": None, "vigenciaMeses": None},
            "SOLICITUD_INVALIDA",
            {"proposito", "alcances", "vigenciaMeses"},
        ),
        ({"alcances": []}, "SOLICITUD_INVALIDA", {"alcances"}),
        ({"vigenciaMeses": 7}, "VIGENCIA_INVALIDA", None),
        ({"versionTexto": "1999-01-01.1"}, "TEXTO_NO_VIGENTE", None),
    ],
)
def test_r8_fallo_solicitud_incompleta_o_invalida_no_deja_rastro(
    app: App, base: str, cambios: dict[str, Any], codigo: str, campos: set[str] | None
) -> None:
    _titular(base)

    respuesta = app.otorgar(app.cuerpo(**cambios))

    assert respuesta.status_code == 422, respuesta.text
    assert respuesta.json()["codigo"] == codigo
    if campos is not None:
        assert campos <= set(respuesta.json()["detalles"]["campos"])
    assert _rastro(base) == (0, 0, 0), "ni consentimiento, ni clave, ni evento"
    assert app.pedir("GET", RUTA).json() == []


def test_r8_fallo_identidad_no_verificada_no_deja_rastro(app: App, base: str) -> None:
    _titular(base, estado="PENDIENTE")

    respuesta = app.otorgar(app.cuerpo())

    assert respuesta.status_code == 403
    assert respuesta.json()["codigo"] == "IDENTIDAD_NO_VERIFICADA"
    assert _rastro(base) == (0, 0, 0)


def test_r8_fallo_clientes_apagado_responde_503_y_no_crea_nada(
    base: str, servidor_clientes: Any, abrir_bff: Any
) -> None:
    """`clientes` estuvo arriba en ese puerto y se apagó: el BFF falla cerrado."""
    _sql(base, "TRUNCATE consentimiento_idempotencia, consentimiento, outbox, cliente")
    _titular(base)
    servidor = servidor_clientes(base).arrancar()
    url = servidor.url
    with abrir_bff(url, SUB_A) as app:
        cuerpo = app.cuerpo()
        servidor.apagar()

        otorgar = app.otorgar(cuerpo)
        listar = app.pedir("GET", RUTA)

        assert otorgar.status_code == 503, otorgar.text
        assert otorgar.json()["codigo"] == "SERVICIO_NO_DISPONIBLE"
        assert otorgar.json()["detalles"] == {}
        assert listar.status_code == 503
    assert _rastro(base) == (0, 0, 0)


@pytest.mark.hu("M08", "AC4")
def test_r8_fallo_repetir_la_misma_clave_deja_un_solo_consentimiento(app: App, base: str) -> None:
    _titular(base)
    cuerpo, clave = app.cuerpo(), str(uuid.uuid4())

    primera = app.otorgar(cuerpo, clave=clave)
    segunda = app.otorgar(cuerpo, clave=clave)

    assert (primera.status_code, segunda.status_code) == (201, 200)
    assert segunda.headers["Idempotent-Replayed"] == "true"
    assert segunda.json() == primera.json()
    assert _rastro(base) == (1, 1, 1), "un consentimiento, una clave y un solo evento"
    assert len(app.pedir("GET", RUTA).json()) == 1


def test_r8_fallo_otra_clave_con_uno_vigente_es_409_sin_segundo_evento(app: App, base: str) -> None:
    _titular(base)
    primero = app.otorgar(app.cuerpo()).json()

    respuesta = app.otorgar(app.cuerpo())

    assert respuesta.status_code == 409
    assert respuesta.json()["codigo"] == "CONSENTIMIENTO_YA_VIGENTE"
    assert respuesta.json()["detalles"] == {"consentimientoId": primero["consentimientoId"]}
    assert _rastro(base) == (1, 1, 1)


def test_r8_fallo_un_titular_no_ve_el_consentimiento_de_otro(app: App, base: str) -> None:
    _titular(base, SUB_A)
    _titular(base, SUB_B)
    ajeno = app.otorgar(app.cuerpo(), sub=SUB_B).json()["consentimientoId"]

    detalle = app.pedir("GET", f"{RUTA}/{ajeno}", sub=SUB_A)
    lista = app.pedir("GET", RUTA, sub=SUB_A)

    assert detalle.status_code == 404
    assert detalle.json()["codigo"] == "CONSENTIMIENTO_NO_ENCONTRADO"
    assert lista.json() == []
    # El dueño sí lo ve: el 404 es por titular, no porque no exista.
    assert app.pedir("GET", f"{RUTA}/{ajeno}", sub=SUB_B).status_code == 200
