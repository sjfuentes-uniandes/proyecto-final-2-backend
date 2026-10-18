"""R8 (HU-M09), versión de módulo: revocación de la app hasta la base de `clientes`.

Corre en CI sin AWS (decisión P1-5). Nada se simula entre la app y la base: `bff-movil` y
`clientes` reales sobre PostgreSQL real (infraestructura compartida en `conftest.py`). El titular
se siembra como fila de `cliente` `VERIFICADO`, como lo deja HU-M06. Solo datos sintéticos.

Aquí se prueba el recorrido; las ramas de cada capa ya las cubren las pruebas de `clientes`
(PR 2.4) y de `bff-movil` (PR 2.5).

La versión para QA, contra el ambiente desplegado, está en `tests/e2e/test_hu_m09_r8_revocacion.py`.
"""

import json
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from jsonschema import Draft202012Validator

SUB_A = "a9a9a9a9-0000-4000-8000-00000000000a"
SUB_B = "b9b9b9b9-0000-4000-8000-00000000000b"
RUTA = "/v1/consentimientos"
ESQUEMA_REVOCADO = json.loads(
    (
        Path(__file__).parents[4] / "contracts" / "events" / "ConsentimientoRevocado.v1.schema.json"
    ).read_text("utf-8")
)

App = Any


# --- Infraestructura del recorrido (conftest.py) ---------------------------------------------


@pytest.fixture
def app(clientes: Any, base: str, abrir_bff: Any) -> Iterator[App]:
    _sql(base, "TRUNCATE consentimiento_idempotencia, consentimiento, outbox, cliente")
    with abrir_bff(clientes.url, SUB_A) as aplicacion:
        yield aplicacion


def _sql(base: str, sentencia: str, parametros: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with psycopg.connect(base, autocommit=True) as conexion:
        cursor = conexion.execute(sentencia, parametros)
        return cursor.fetchall() if cursor.description else []


def _titular(base: str, sub: str = SUB_A) -> uuid.UUID:
    """El cliente que deja HU-M06 tras verificar la identidad."""
    cliente_id = uuid.uuid4()
    _sql(
        base,
        "INSERT INTO cliente (id, sub, estado, verificado_en) VALUES (%s, %s, 'VERIFICADO', %s)",
        (cliente_id, sub, datetime.now(UTC)),
    )
    return cliente_id


def _vencido(base: str, cliente_id: uuid.UUID) -> str:
    """Un `ACTIVO` almacenado cuya vigencia ya pasó: así queda hasta que algo lo materialice."""
    consentimiento_id = uuid.uuid4()
    ahora = datetime.now(UTC)
    _sql(
        base,
        "INSERT INTO consentimiento (id, cliente_id, proposito, alcances, version_texto, idioma, "
        "estado, otorgado_en, vigente_hasta) VALUES (%s, %s, 'PERFILAMIENTO_PRECIO', "
        "ARRAY['FA_PRODUCTOS_VIGENTES'], 'v0', 'es-CO', 'ACTIVO', %s, %s)",
        (consentimiento_id, cliente_id, ahora - timedelta(days=400), ahora - timedelta(days=35)),
    )
    return str(consentimiento_id)


def _revocar(app: App, consentimiento_id: str, sub: str | None = None, **kwargs: Any) -> Any:
    return app.pedir("POST", f"{RUTA}/{consentimiento_id}/revocacion", sub, **kwargs)


def _historial(app: App, consentimiento_id: str, sub: str | None = None) -> Any:
    return app.pedir("GET", f"{RUTA}/{consentimiento_id}/historial", sub)


def _eventos(base: str) -> dict[str, int]:
    """Filas de outbox por tipo de evento."""
    return dict(_sql(base, "SELECT event_type, count(*) FROM outbox GROUP BY event_type"))


def _fila(base: str, consentimiento_id: str) -> tuple[Any, ...]:
    ((estado, revocado_en),) = _sql(
        base, "SELECT estado, revocado_en FROM consentimiento WHERE id = %s", (consentimiento_id,)
    )
    return estado, revocado_en


def _z(instante: datetime) -> str:
    return instante.astimezone(UTC).isoformat().replace("+00:00", "Z")


# --- Caso feliz ------------------------------------------------------------------------------


@pytest.mark.hu("M09", "AC1")
@pytest.mark.hu("M09", "AC4")
def test_r8_feliz_otorgar_revocar_y_consultar_lista_e_historial(app: App, base: str) -> None:
    cliente_id = _titular(base)
    correlacion = "corr-r8-revocar"
    otorgado = app.otorgar(app.cuerpo()).json()
    consentimiento_id = otorgado["consentimientoId"]

    respuesta = _revocar(app, consentimiento_id, headers={"X-Correlation-Id": correlacion})

    assert respuesta.status_code == 200, respuesta.text
    revocado = respuesta.json()
    assert revocado["estado"] == "REVOCADO"
    assert revocado["revocadoEn"] is not None
    # Solo cambian el estado y la fecha: el resto del comprobante es inmutable.
    assert {k: v for k, v in revocado.items() if k not in ("estado", "revocadoEn")} == {
        k: v for k, v in otorgado.items() if k not in ("estado", "revocadoEn")
    }
    assert respuesta.headers["X-Correlation-Id"] == correlacion

    # En la base de `clientes`: la fila revocada con el instante de la base y, en la misma
    # transacción, el evento en el outbox sin publicar (el relay es la PR 2.2).
    estado, revocado_en = _fila(base, consentimiento_id)
    assert (estado, _z(revocado_en)) == ("REVOCADO", revocado["revocadoEn"])
    assert _eventos(base) == {"ConsentimientoOtorgado": 1, "ConsentimientoRevocado": 1}
    ((evento_id, version, agregado, corr, publicado, payload, ocurrido),) = _sql(
        base,
        "SELECT event_id, event_version, aggregate_id, correlation_id, published_at, payload, "
        "occurred_at FROM outbox WHERE event_type = 'ConsentimientoRevocado'",
    )
    assert (version, str(agregado), publicado, ocurrido) == (
        1,
        consentimiento_id,
        None,
        revocado_en,
    )
    assert corr == correlacion, "el X-Correlation-Id de la app debe llegar al evento"
    # Sin PII: solo identificadores y la fecha.
    assert payload == {
        "consentimientoId": consentimiento_id,
        "clienteId": str(cliente_id),
        "revocadoEn": revocado_en.isoformat(),
        "actorId": SUB_A,
    }
    Draft202012Validator(
        ESQUEMA_REVOCADO, format_checker=Draft202012Validator.FORMAT_CHECKER
    ).validate(
        {
            "eventId": str(evento_id),
            "aggregateId": str(agregado),
            "type": "ConsentimientoRevocado",
            "version": version,
            "occurredAt": _z(ocurrido),
            "correlationId": corr,
            "aggregateVersion": 2,  # ADR 0009: lo deriva el relay del tipo; no es columna
            "payload": payload,
        }
    )

    # La app lo consulta después: lista, detalle e historial cuentan lo mismo.
    lista = app.pedir("GET", RUTA)
    detalle = app.pedir("GET", f"{RUTA}/{consentimiento_id}")
    historial = _historial(app, consentimiento_id)
    assert (lista.status_code, lista.json()) == (200, [revocado])
    assert (detalle.status_code, detalle.json()) == (200, revocado)
    assert historial.status_code == 200
    assert historial.json() == {
        "consentimientoId": consentimiento_id,
        "estado": "REVOCADO",
        "hitos": [
            {"tipo": "OTORGADO", "ocurridoEn": otorgado["otorgadoEn"]},
            {"tipo": "REVOCADO", "ocurridoEn": revocado["revocadoEn"]},
        ],
    }


@pytest.mark.hu("M09", "AC4")
def test_r8_feliz_revocar_y_volver_a_otorgar_conserva_el_revocado(app: App, base: str) -> None:
    """El índice único parcial solo cuenta los `ACTIVO`: tras revocar se otorga uno nuevo y el
    anterior queda como estaba (ADR 0009)."""
    _titular(base)
    primero = app.otorgar(app.cuerpo()).json()
    revocado = _revocar(app, primero["consentimientoId"]).json()

    nuevo = app.otorgar(app.cuerpo())

    assert nuevo.status_code == 201, nuevo.text
    assert nuevo.json()["consentimientoId"] != primero["consentimientoId"]
    assert app.pedir("GET", RUTA).json() == [nuevo.json(), revocado]
    assert _sql(base, "SELECT estado, count(*) FROM consentimiento GROUP BY estado ORDER BY 1") == [
        ("ACTIVO", 1),
        ("REVOCADO", 1),
    ]
    assert _eventos(base) == {"ConsentimientoOtorgado": 2, "ConsentimientoRevocado": 1}
    assert [h["tipo"] for h in _historial(app, primero["consentimientoId"]).json()["hitos"]] == [
        "OTORGADO",
        "REVOCADO",
    ]


# --- Fallos controlados y límites ------------------------------------------------------------


@pytest.mark.hu("M09", "AC3")
def test_r8_fallo_repetir_la_revocacion_responde_igual_con_un_solo_evento(
    app: App, base: str
) -> None:
    _titular(base)
    consentimiento_id = app.otorgar(app.cuerpo()).json()["consentimientoId"]

    primera = _revocar(app, consentimiento_id)
    segunda = _revocar(app, consentimiento_id)

    assert (primera.status_code, segunda.status_code) == (200, 200)
    assert segunda.json() == primera.json()
    assert _eventos(base)["ConsentimientoRevocado"] == 1
    assert len(_historial(app, consentimiento_id).json()["hitos"]) == 2


def test_r8_fallo_revocar_sin_token_responde_401_sin_cambios(app: App, base: str) -> None:
    _titular(base)
    consentimiento_id = app.otorgar(app.cuerpo()).json()["consentimientoId"]

    respuesta = app.bff.post(
        f"{RUTA}/{consentimiento_id}/revocacion", headers={"X-Authenticated-Sub": SUB_A}
    )

    assert respuesta.status_code == 401
    assert respuesta.json()["codigo"] == "NO_AUTENTICADO"
    assert _fila(base, consentimiento_id) == ("ACTIVO", None)
    assert "ConsentimientoRevocado" not in _eventos(base)


def test_r8_fallo_revocar_uno_ajeno_o_inexistente_responde_404(app: App, base: str) -> None:
    _titular(base, SUB_A)
    _titular(base, SUB_B)
    ajeno = app.otorgar(app.cuerpo(), sub=SUB_B).json()["consentimientoId"]

    respuestas = [
        _revocar(app, ajeno),
        _historial(app, ajeno),
        _revocar(app, str(uuid.uuid4())),
    ]

    for respuesta in respuestas:
        assert respuesta.status_code == 404, respuesta.text
        assert respuesta.json()["codigo"] == "CONSENTIMIENTO_NO_ENCONTRADO"
    # Indistinguibles: lo ajeno responde igual que lo inexistente.
    assert respuestas[0].json()["mensaje"] == respuestas[2].json()["mensaje"]
    assert _fila(base, ajeno) == ("ACTIVO", None)
    assert "ConsentimientoRevocado" not in _eventos(base)
    assert app.pedir("GET", f"{RUTA}/{ajeno}", sub=SUB_B).json()["estado"] == "ACTIVO"


def test_r8_limite_revocar_un_vencido_responde_vencido_sin_evento(app: App, base: str) -> None:
    """Prueba obligatoria de la ADR 0009 de punta a punta: un `ACTIVO` almacenado ya vencido no
    se revoca."""
    vencido = _vencido(base, _titular(base))

    respuesta = _revocar(app, vencido)

    assert respuesta.status_code == 200, respuesta.text
    assert (respuesta.json()["estado"], respuesta.json()["revocadoEn"]) == ("VENCIDO", None)
    assert _fila(base, vencido) == ("ACTIVO", None)
    assert _eventos(base) == {}
    assert [h["tipo"] for h in _historial(app, vencido).json()["hitos"]] == ["OTORGADO", "VENCIDO"]


def test_r8_limite_carrera_revocar_contra_vencer_al_reotorgar(app: App, base: str) -> None:
    """Revocar un vencido mientras un otorgamiento nuevo lo materializa: en cualquier orden, la
    revocación lo ve `VENCIDO`, no hay `ConsentimientoRevocado` y el nuevo queda `ACTIVO`."""
    vencido = _vencido(base, _titular(base))
    cuerpo = app.cuerpo()

    with ThreadPoolExecutor(max_workers=2) as hilos:
        revocacion = hilos.submit(_revocar, app, vencido)
        otorgamiento = hilos.submit(app.otorgar, cuerpo)
        revocado, otorgado = revocacion.result(), otorgamiento.result()

    assert revocado.status_code == 200, revocado.text
    assert (revocado.json()["estado"], revocado.json()["revocadoEn"]) == ("VENCIDO", None)
    assert otorgado.status_code == 201, otorgado.text
    assert _fila(base, vencido) == ("VENCIDO", None)
    assert _eventos(base) == {"ConsentimientoOtorgado": 1}


def test_r8_fallo_clientes_apagado_al_revocar_responde_503_sin_cambios(
    base: str, servidor_clientes: Any, abrir_bff: Any
) -> None:
    """`clientes` estuvo arriba en ese puerto y se apagó: el BFF falla cerrado."""
    _sql(base, "TRUNCATE consentimiento_idempotencia, consentimiento, outbox, cliente")
    _titular(base)
    servidor = servidor_clientes(base).arrancar()
    with abrir_bff(servidor.url, SUB_A) as app:
        consentimiento_id = app.otorgar(app.cuerpo()).json()["consentimientoId"]
        servidor.apagar()

        revocar = _revocar(app, consentimiento_id)
        historial = _historial(app, consentimiento_id)

    for respuesta in (revocar, historial):
        assert respuesta.status_code == 503, respuesta.text
        assert respuesta.json()["codigo"] == "SERVICIO_NO_DISPONIBLE"
        assert respuesta.json()["detalles"] == {}
    assert _fila(base, consentimiento_id) == ("ACTIVO", None)
    assert _eventos(base) == {"ConsentimientoOtorgado": 1}


def test_r8_fallo_clientes_lento_al_revocar_no_deja_nada_a_medias(
    app: App, clientes: Any, base: str, abrir_bff: Any
) -> None:
    """El BFF corta antes de que `clientes` responda: la app recibe 503. Lo que haya pasado en
    `clientes` es todo o nada (estado y evento en la misma transacción), y repetir devuelve
    `REVOCADO` con un solo evento."""
    _titular(base)
    consentimiento_id = app.otorgar(app.cuerpo()).json()["consentimientoId"]

    with abrir_bff(clientes.url, SUB_A, revocacion_timeout_ms=1) as impaciente:
        lenta = _revocar(impaciente, consentimiento_id)

    assert lenta.status_code == 503, lenta.text
    assert lenta.json()["codigo"] == "SERVICIO_NO_DISPONIBLE"
    # Una sola sentencia, una sola foto: nunca un REVOCADO sin su evento ni al revés.
    ((revocados, eventos),) = _sql(
        base,
        "SELECT (SELECT count(*) FROM consentimiento WHERE estado = 'REVOCADO'), "
        "(SELECT count(*) FROM outbox WHERE event_type = 'ConsentimientoRevocado')",
    )
    assert revocados == eventos <= 1

    repeticion = _revocar(app, consentimiento_id)

    assert repeticion.status_code == 200, repeticion.text
    assert repeticion.json()["estado"] == "REVOCADO"
    assert _eventos(base)["ConsentimientoRevocado"] == 1
