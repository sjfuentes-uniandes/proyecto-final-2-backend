"""Relay del outbox (HU-W16, 16.4): ciclo, sobre, fallos, bucle y adaptadores con puertos falsos.

Las pruebas con PostgreSQL y LocalStack reales están en `tests/module/test_outbox_relay.py`.
"""

import asyncio
import json
import threading
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import anyio
import botocore.session
import pytest
from botocore.exceptions import ClientError, ConnectTimeoutError, EndpointConnectionError
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from pydantic import TypeAdapter
from solventa_common import outbox
from solventa_common.outbox import (
    EventoPendiente,
    OutboxPg,
    PublicadorSns,
    RelayOutbox,
    SobreInvalido,
    cliente_sns,
    describir_error,
    es_fallo_de_la_fila,
    espera_tras_fallos,
    sobre_json,
)

EVENTOS = Path(__file__).resolve().parents[4] / "contracts" / "events"
CENTINELA = "Juan Pérez 1032456789"
INSTANTE = datetime(2026, 10, 28, 13, 5, 26, 120, tzinfo=UTC)
CAMPOS_SOBRE = [
    "eventId",
    "aggregateId",
    "type",
    "version",
    "occurredAt",
    "correlationId",
    "payload",
]


def _validador(tipo: str) -> Draft202012Validator:
    esquema = json.loads((EVENTOS / f"{tipo}.v1.schema.json").read_text(encoding="utf-8"))
    return Draft202012Validator(esquema, format_checker=Draft202012Validator.FORMAT_CHECKER)


def _payload_oferta() -> dict[str, Any]:
    aceptacion = str(uuid4())
    return {
        "aceptacionId": aceptacion,
        "cotizacionId": str(uuid4()),
        "ofertaId": None,
        "decisionId": str(uuid4()),
        "clienteId": str(uuid4()),
        "productoId": "VIDA-INDIVIDUAL",
        "prima": {"valor": "1850000.50", "moneda": "COP", "periodicidad": "ANUAL"},
        "claveRecaudo": aceptacion,
        "huellaCalculo": "sha256:abc",
        "vigenteHasta": "2026-11-27T13:05:26Z",
        "versionDeclaraciones": "2026-10",
        "aceptadaEn": "2026-10-28T13:05:26.000120Z",
        "actorId": "asesor-sintetico-01",
        "canal": "PORTAL_ASESOR",
    }


def _payload_decision() -> dict[str, Any]:
    return {
        "decisionId": str(uuid4()),
        "cotizacionId": str(uuid4()),
        "ofertaId": str(uuid4()),
        "clienteId": str(uuid4()),
        "productoId": "VIDA-INDIVIDUAL",
        "resultado": "APROBADA",
        "tipo": "AUTOMATICA",
        "reglaDeterminante": None,
        "reglasNoCumplidas": [],
        "versionReglas": "2026.10",
        "hashReglas": "sha256:reglas",
        "hashInsumos": "sha256:insumos",
        "actorId": "asesor-sintetico-01",
        "decididaEn": "2026-10-28T13:05:26Z",
    }


def _evento(
    tipo: str = "OfertaAceptada",
    *,
    payload: dict[str, Any] | None = None,
    occurred_at: datetime = INSTANTE,
    attempts: int = 0,
) -> EventoPendiente:
    datos = payload if payload is not None else _payload_oferta()
    return EventoPendiente(
        event_id=uuid4(),
        aggregate_id=uuid4(),
        event_type=tipo,
        event_version=1,
        occurred_at=occurred_at,
        correlation_id="corr-sintetico-01",
        payload=json.dumps(datos, ensure_ascii=False),
        attempts=attempts,
    )


# --- Puertos falsos -------------------------------------------------------------------
@dataclass
class Fila:
    evento: EventoPendiente
    published_at: datetime | None = None
    last_error: str | None = None


@dataclass
class TransaccionFalsa:
    repositorio: "RepositorioFalso"
    publicados: list[UUID] = field(default_factory=list)
    cuando: datetime | None = None
    fallos: dict[UUID, tuple[str, bool]] = field(default_factory=dict)

    async def pendientes(self, limite: int, max_intentos: int) -> list[EventoPendiente]:
        self.repositorio.lecturas.append((limite, max_intentos))
        filas = [
            f
            for f in self.repositorio.filas.values()
            if f.published_at is None and f.evento.attempts < max_intentos
        ]
        filas.sort(key=lambda f: (f.evento.occurred_at, f.evento.event_id))
        return [f.evento for f in filas[:limite]]

    async def marcar_publicados(self, event_ids: Sequence[UUID], cuando: datetime) -> None:
        self.publicados.extend(event_ids)
        self.cuando = cuando

    async def registrar_fallo(self, event_id: UUID, error: str, *, definitivo: bool) -> None:
        self.fallos[event_id] = (error, definitivo)


@dataclass
class RepositorioFalso:
    """Tabla en memoria: los cambios de una transacción se aplican solo si termina sin error."""

    filas: dict[UUID, Fila] = field(default_factory=dict)
    lecturas: list[tuple[int, int]] = field(default_factory=list)
    falla_al_abrir: int = 0

    def agregar(self, *eventos: EventoPendiente) -> None:
        for evento in eventos:
            self.filas[evento.event_id] = Fila(evento)

    @asynccontextmanager
    async def transaccion(self) -> AsyncIterator[TransaccionFalsa]:
        if self.falla_al_abrir:
            self.falla_al_abrir -= 1
            raise ConnectionError("base de datos no disponible")
        tx = TransaccionFalsa(self)
        yield tx
        for event_id in tx.publicados:
            self.filas[event_id].published_at = tx.cuando
        for event_id, (error, definitivo) in tx.fallos.items():
            fila = self.filas[event_id]
            fila.evento = replace(fila.evento, attempts=fila.evento.attempts + int(definitivo))
            fila.last_error = error

    def pendientes(self) -> list[Fila]:
        return [f for f in self.filas.values() if f.published_at is None]


@dataclass
class PublicadorFalso:
    fallas: dict[UUID, BaseException] = field(default_factory=dict)
    publicados: list[EventoPendiente] = field(default_factory=list)
    llamadas: int = 0

    async def publicar(self, evento: EventoPendiente) -> None:
        self.llamadas += 1
        if evento.event_id in self.fallas:
            raise self.fallas[evento.event_id]
        self.publicados.append(evento)


def _error_aws(codigo: str, mensaje: str = "x", estado: int | None = 400) -> ClientError:
    """Como lo arma botocore (`client.py`): el estado va en `ResponseMetadata.HTTPStatusCode`."""
    respuesta: dict[str, Any] = {"Error": {"Code": codigo, "Message": mensaje}}
    if estado is not None:
        respuesta["ResponseMetadata"] = {"HTTPStatusCode": estado}
    return ClientError(respuesta, "Publish")


def _relay(
    repositorio: RepositorioFalso, publicador: PublicadorFalso, **opciones: Any
) -> RelayOutbox:
    return RelayOutbox(repositorio, publicador, reloj=lambda: INSTANTE, **opciones)


# --- Sobre --------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("tipo", "payload"),
    [("OfertaAceptada", _payload_oferta()), ("DecisionSuscripcionRegistrada", _payload_decision())],
)
def test_el_sobre_es_identico_al_esquema(tipo: str, payload: dict[str, Any]) -> None:
    evento = _evento(tipo, payload=payload)

    sobre = json.loads(sobre_json(evento))

    _validador(tipo).validate(sobre)
    esquema = _validador(tipo).schema
    assert list(sobre) == CAMPOS_SOBRE
    assert set(sobre) == set(esquema["required"]) == set(esquema["properties"])
    assert sobre == {
        "eventId": str(evento.event_id),
        "aggregateId": str(evento.aggregate_id),
        "type": tipo,
        "version": 1,
        "occurredAt": "2026-10-28T13:05:26.000120Z",
        "correlationId": "corr-sintetico-01",
        "payload": payload,
    }


@pytest.mark.parametrize(
    "instante",
    [
        datetime(2026, 10, 28, 13, 5, 26, tzinfo=UTC),
        datetime(2026, 10, 28, 13, 5, 26, 120, tzinfo=UTC),
        datetime(2026, 10, 28, 8, 5, 26, 999999, tzinfo=timezone(timedelta(hours=-5))),
    ],
)
def test_occurred_at_se_serializa_como_pydantic_en_utc(instante: datetime) -> None:
    sobre = json.loads(sobre_json(_evento(occurred_at=instante)))

    esperado = TypeAdapter(datetime).dump_python(instante.astimezone(UTC), mode="json")
    assert sobre["occurredAt"] == esperado
    assert sobre["occurredAt"].endswith("Z")


def test_el_payload_viaja_tal_cual_sin_pasar_por_float() -> None:
    evento = replace(_evento(), payload='{"monto": 12345678901234567890.123456789, "n": "ñ"}')

    sobre = json.loads(sobre_json(evento), parse_float=Decimal)

    assert sobre["payload"] == {"monto": Decimal("12345678901234567890.123456789"), "n": "ñ"}


# --- Publicador SNS -------------------------------------------------------------------------
class ClienteSnsFalso:
    def __init__(self) -> None:
        self.llamadas: list[dict[str, Any]] = []
        self.hilos: list[int] = []

    def publish(self, **kwargs: Any) -> dict[str, str]:
        self.llamadas.append(kwargs)
        self.hilos.append(threading.get_ident())
        return {"MessageId": "m-1"}


@pytest.mark.anyio
async def test_publica_el_sobre_con_el_atributo_event_type_en_otro_hilo() -> None:
    cliente = ClienteSnsFalso()
    evento = _evento("RecaudoConfirmado")

    await PublicadorSns(cliente, "arn:aws:sns:us-east-1:000000000000:eventos").publicar(evento)

    assert cliente.llamadas == [
        {
            "TopicArn": "arn:aws:sns:us-east-1:000000000000:eventos",
            "Message": sobre_json(evento),
            "MessageAttributes": {
                "eventType": {"DataType": "String", "StringValue": "RecaudoConfirmado"}
            },
        }
    ]
    assert cliente.hilos != [threading.get_ident()]


def test_cliente_sns_recibe_los_timeouts_y_reintentos_por_parametro() -> None:
    cliente = cliente_sns(
        "us-east-1",
        endpoint_url="http://localhost:4566",
        timeout_conexion_s=0.7,
        timeout_lectura_s=2.5,
        intentos=4,
    )

    assert cliente.meta.config.connect_timeout == 0.7
    assert cliente.meta.config.read_timeout == 2.5
    assert cliente.meta.config.retries == {"total_max_attempts": 4, "mode": "standard"}
    assert cliente.meta.endpoint_url == "http://localhost:4566"
    assert cliente.meta.region_name == "us-east-1"


def test_cliente_sns_usa_timeouts_explicitos_por_defecto() -> None:
    cliente = cliente_sns("us-east-1")

    assert cliente.meta.config.connect_timeout == outbox.TIMEOUT_CONEXION_S
    assert cliente.meta.config.read_timeout == outbox.TIMEOUT_LECTURA_S
    assert cliente.meta.config.retries == {
        "total_max_attempts": outbox.INTENTOS_SNS,
        "mode": "standard",
    }


# --- Ciclo --------------------------------------------------------------------------------
@pytest.mark.anyio
async def test_sin_pendientes_no_publica_nada() -> None:
    repositorio, publicador = RepositorioFalso(), PublicadorFalso()

    assert await _relay(repositorio, publicador).ejecutar_una_vez() == 0
    assert publicador.publicados == []


@pytest.mark.anyio
async def test_publica_en_orden_y_marca_published_at_con_el_reloj() -> None:
    repositorio, publicador = RepositorioFalso(), PublicadorFalso()
    tardio = _evento(occurred_at=INSTANTE + timedelta(seconds=1))
    temprano = _evento(occurred_at=INSTANTE)
    repositorio.agregar(tardio, temprano)

    assert await _relay(repositorio, publicador).ejecutar_una_vez() == 2

    assert [e.event_id for e in publicador.publicados] == [temprano.event_id, tardio.event_id]
    assert {f.published_at for f in repositorio.filas.values()} == {INSTANTE}


@pytest.mark.anyio
async def test_el_reloj_por_defecto_marca_en_utc() -> None:
    repositorio, publicador = RepositorioFalso(), PublicadorFalso()
    evento = _evento()
    repositorio.agregar(evento)
    antes = datetime.now(UTC)

    await RelayOutbox(repositorio, publicador).ejecutar_una_vez()

    published_at = repositorio.filas[evento.event_id].published_at
    assert published_at is not None
    assert published_at.tzinfo == UTC
    assert antes <= published_at <= datetime.now(UTC)


@pytest.mark.anyio
async def test_lee_a_lo_sumo_un_lote_con_el_maximo_de_intentos() -> None:
    repositorio, publicador = RepositorioFalso(), PublicadorFalso()
    repositorio.agregar(*(_evento() for _ in range(5)))
    relay = _relay(repositorio, publicador, lote=2, max_intentos=7)

    assert [await relay.ejecutar_una_vez() for _ in range(4)] == [2, 2, 1, 0]
    assert repositorio.lecturas == [(2, 7)] * 4


@pytest.mark.anyio
async def test_un_fallo_de_la_fila_no_bloquea_al_resto_del_lote_y_suma_attempts() -> None:
    eventos = [_evento(occurred_at=INSTANTE + timedelta(seconds=i)) for i in range(3)]
    repositorio = RepositorioFalso()
    repositorio.agregar(*eventos)
    publicador = PublicadorFalso(fallas={eventos[1].event_id: _error_aws("InvalidParameter")})

    assert await _relay(repositorio, publicador).ejecutar_una_vez() == 2

    assert [e.event_id for e in publicador.publicados] == [eventos[0].event_id, eventos[2].event_id]
    fallida = repositorio.filas[eventos[1].event_id]
    assert fallida.published_at is None
    assert fallida.evento.attempts == 1
    assert fallida.last_error == "ClientError: InvalidParameter"
    for evento in (eventos[0], eventos[2]):
        assert repositorio.filas[evento.event_id].published_at == INSTANTE
        assert repositorio.filas[evento.event_id].evento.attempts == 0


@pytest.mark.anyio
async def test_un_fallo_general_registra_last_error_sin_sumar_attempts() -> None:
    eventos = [_evento(occurred_at=INSTANTE + timedelta(seconds=i)) for i in range(2)]
    repositorio = RepositorioFalso()
    repositorio.agregar(*eventos)
    publicador = PublicadorFalso(fallas={eventos[0].event_id: TimeoutError()})

    assert await _relay(repositorio, publicador).ejecutar_una_vez() == 1

    fallida = repositorio.filas[eventos[0].event_id]
    assert (fallida.published_at, fallida.evento.attempts) == (None, 0)
    assert fallida.last_error == "TimeoutError"
    assert repositorio.filas[eventos[1].event_id].published_at == INSTANTE


@pytest.mark.anyio
async def test_si_todos_fallan_no_marca_ninguno() -> None:
    evento = _evento()
    repositorio = RepositorioFalso()
    repositorio.agregar(evento)
    publicador = PublicadorFalso(fallas={evento.event_id: ConnectionError()})

    assert await _relay(repositorio, publicador).ejecutar_una_vez() == 0
    assert repositorio.filas[evento.event_id].published_at is None
    assert repositorio.filas[evento.event_id].last_error == "ConnectionError"


@pytest.mark.anyio
async def test_60_ciclos_con_sns_inalcanzable_no_agotan_la_fila_y_se_publica_al_volver() -> None:
    evento = _evento()
    repositorio = RepositorioFalso()
    repositorio.agregar(evento)
    caida = EndpointConnectionError(endpoint_url="https://sns.us-east-1.amazonaws.com")
    publicador = PublicadorFalso(fallas={evento.event_id: caida})
    relay = _relay(repositorio, publicador)

    assert [await relay.ejecutar_una_vez() for _ in range(60)] == [0] * 60
    fila = repositorio.filas[evento.event_id]
    assert (fila.evento.attempts, fila.last_error) == (0, "EndpointConnectionError")
    assert publicador.llamadas == 60

    publicador.fallas.clear()
    assert await relay.ejecutar_una_vez() == 1
    assert fila.published_at == INSTANTE


@pytest.mark.anyio
async def test_un_invalid_parameter_repetido_5_veces_deja_la_fila_fuera_con_un_error(
    telemetria: Any,
) -> None:
    evento = _evento()
    repositorio = RepositorioFalso()
    repositorio.agregar(evento)
    publicador = PublicadorFalso(fallas={evento.event_id: _error_aws("InvalidParameter")})
    relay = _relay(repositorio, publicador)

    assert [await relay.ejecutar_una_vez() for _ in range(6)] == [0] * 6

    fila = repositorio.filas[evento.event_id]
    assert (fila.evento.attempts, fila.published_at) == (5, None)
    assert publicador.llamadas == 5
    agotados = [r for r in telemetria.logs() if r.get("message") == outbox.MENSAJE_AGOTADO]
    assert [(r["level"], r["attempts"], r["max_intentos"]) for r in agotados] == [("ERROR", 5, 5)]


def test_max_intentos_por_defecto_es_5() -> None:
    assert outbox.MAX_INTENTOS == 5


@pytest.mark.parametrize(
    ("error", "de_la_fila"),
    [
        (_error_aws("InvalidParameter", estado=400), True),
        (_error_aws("ParameterValueInvalid", estado=400), True),
        (_error_aws("AlgunOtro4xx", estado=404), True),
        (_error_aws("AuthorizationError", estado=403), False),
        (_error_aws("Throttled", estado=429), False),
        (_error_aws("KMSThrottling", estado=400), False),
        (_error_aws("Throttling", estado=400), False),
        (_error_aws("InternalError", estado=500), False),
        (_error_aws("ServiceUnavailable", estado=503), False),
        (_error_aws("InvalidParameter", estado=None), False),
        (EndpointConnectionError(endpoint_url="https://sns"), False),
        (ConnectTimeoutError(endpoint_url="https://sns"), False),
        (TimeoutError(), False),
        (ValueError("x"), False),
        (SobreInvalido(), True),
    ],
)
def test_clasifica_el_fallo_de_la_fila_frente_al_general(
    error: BaseException, de_la_fila: bool
) -> None:
    assert es_fallo_de_la_fila(error) is de_la_fila


# Errores de Publish que describen el tópico, con el estado del modelo de SNS de botocore.
ERRORES_DEL_TOPICO = [
    ("NotFound", 404),
    ("AuthorizationError", 403),
    ("InvalidSecurity", 403),
    ("KMSAccessDenied", 400),
    ("KMSDisabled", 400),
    ("KMSInvalidState", 400),
    ("KMSNotFound", 400),
    ("KMSOptInRequired", 403),
    ("KMSThrottling", 400),
]


@pytest.mark.parametrize(("codigo", "estado"), ERRORES_DEL_TOPICO)
@pytest.mark.anyio
async def test_un_error_del_topico_no_suma_attempts_y_se_publica_cuando_el_topico_responde(
    codigo: str, estado: int
) -> None:
    evento = _evento()
    repositorio = RepositorioFalso()
    repositorio.agregar(evento)
    publicador = PublicadorFalso(fallas={evento.event_id: _error_aws(codigo, estado=estado)})
    relay = _relay(repositorio, publicador)

    # Más ciclos que max_intentos (5): ninguno consume la fila.
    assert [await relay.ejecutar_una_vez() for _ in range(6)] == [0] * 6
    fila = repositorio.filas[evento.event_id]
    assert (fila.evento.attempts, fila.last_error) == (0, f"ClientError: {codigo}")

    publicador.fallas.clear()
    assert await relay.ejecutar_una_vez() == 1
    assert fila.published_at == INSTANTE


def test_los_errores_del_topico_cubren_los_kms_y_de_autorizacion_del_modelo_de_sns() -> None:
    """Si botocore agrega a `Publish` un error KMS o de autorización, la constante lo incluye."""
    modelo = botocore.session.get_session().get_service_model("sns")
    errores = {
        e.metadata["error"]["code"]: e.metadata["error"]["httpStatusCode"]
        for e in modelo.operation_model("Publish").error_shapes
    }
    del_topico = {c for c in errores if c.startswith("KMS") or errores[c] == 403} | {"NotFound"}

    assert del_topico <= outbox.ERRORES_DEL_TOPICO
    assert {(c, errores[c]) for c in del_topico} == set(ERRORES_DEL_TOPICO)


def test_un_payload_que_no_es_un_objeto_json_no_arma_sobre() -> None:
    for payload in ("no es json", "[1, 2]", "null"):
        with pytest.raises(SobreInvalido):
            sobre_json(replace(_evento(), payload=payload))


@pytest.mark.anyio
async def test_un_sobre_invalido_suma_attempts_sin_llamar_a_sns() -> None:
    evento = replace(_evento(), payload="no es json")
    repositorio = RepositorioFalso()
    repositorio.agregar(evento)
    cliente = ClienteSnsFalso()

    relay = RelayOutbox(repositorio, PublicadorSns(cliente, "arn:x"), reloj=lambda: INSTANTE)
    assert await relay.ejecutar_una_vez() == 0

    fila = repositorio.filas[evento.event_id]
    assert (fila.evento.attempts, fila.last_error) == (1, "SobreInvalido")
    assert cliente.llamadas == []


@pytest.mark.anyio
async def test_un_error_de_la_base_se_propaga_y_no_marca_nada() -> None:
    repositorio, publicador = RepositorioFalso(falla_al_abrir=1), PublicadorFalso()
    repositorio.agregar(_evento())

    with pytest.raises(ConnectionError):
        await _relay(repositorio, publicador).ejecutar_una_vez()
    assert len(repositorio.pendientes()) == 1


@pytest.mark.anyio
async def test_al_alcanzar_max_intentos_deja_de_reintentar_y_lo_registra(
    telemetria: Any,
) -> None:
    evento = _evento(attempts=2)
    repositorio = RepositorioFalso()
    repositorio.agregar(evento)
    publicador = PublicadorFalso(fallas={evento.event_id: _error_aws("InvalidParameter")})
    relay = _relay(repositorio, publicador, max_intentos=3)

    assert await relay.ejecutar_una_vez() == 0
    assert await relay.ejecutar_una_vez() == 0

    fila = repositorio.filas[evento.event_id]
    assert fila.evento.attempts == 3
    assert fila.published_at is None
    agotados = [r for r in telemetria.logs() if r.get("message") == outbox.MENSAJE_AGOTADO]
    assert len(agotados) == 1
    assert agotados[0]["level"] == "ERROR"
    assert agotados[0]["event_id"] == str(evento.event_id)
    assert agotados[0]["event_type"] == "OfertaAceptada"
    assert agotados[0]["attempts"] == 3
    assert agotados[0]["max_intentos"] == 3


@pytest.mark.anyio
async def test_un_fallo_que_no_alcanza_el_maximo_no_se_registra_como_agotado(
    telemetria: Any,
) -> None:
    evento = _evento()
    repositorio = RepositorioFalso()
    repositorio.agregar(evento)
    publicador = PublicadorFalso(fallas={evento.event_id: _error_aws("InvalidParameter")})

    await _relay(repositorio, publicador, max_intentos=3).ejecutar_una_vez()

    assert not [r for r in telemetria.logs() if r.get("message") == outbox.MENSAJE_AGOTADO]


@pytest.mark.parametrize("max_intentos", [0, -1])
def test_max_intentos_y_lote_deben_ser_positivos(max_intentos: int) -> None:
    with pytest.raises(ValueError):
        RelayOutbox(RepositorioFalso(), PublicadorFalso(), max_intentos=max_intentos)
    with pytest.raises(ValueError):
        RelayOutbox(RepositorioFalso(), PublicadorFalso(), lote=max_intentos)


# --- last_error y logs sin PII -----------------------------------------------------------------
def test_describir_error_guarda_solo_el_tipo_y_el_codigo_de_aws() -> None:
    assert describir_error(ValueError(CENTINELA)) == "ValueError"
    assert describir_error(_error_aws("InvalidParameter", CENTINELA)) == (
        "ClientError: InvalidParameter"
    )


def test_describir_error_descarta_un_codigo_que_no_es_un_identificador() -> None:
    assert describir_error(_error_aws(CENTINELA, "x")) == "ClientError"
    assert describir_error(_error_aws("", "x")) == "ClientError"


def test_describir_error_se_recorta() -> None:
    largo = type("E" * 300, (Exception,), {})()

    assert len(describir_error(largo)) == outbox.LARGO_MAX_ERROR


@pytest.mark.anyio
async def test_last_error_y_logs_sin_payload_ni_pii(telemetria: Any) -> None:
    payload = _payload_oferta() | {"actorId": CENTINELA}
    fallido, publicado = _evento(payload=payload), _evento(payload=payload)
    repositorio = RepositorioFalso()
    repositorio.agregar(fallido, publicado)
    publicador = PublicadorFalso(
        fallas={fallido.event_id: _error_aws("InvalidParameter", f"{CENTINELA} {payload}")}
    )

    await _relay(repositorio, publicador).ejecutar_una_vez()

    assert repositorio.filas[fallido.event_id].last_error == "ClientError: InvalidParameter"
    texto = telemetria.texto_logs()
    assert CENTINELA not in texto
    assert "1850000.50" not in texto
    assert "huellaCalculo" not in texto


@pytest.mark.anyio
async def test_mide_cada_publicacion_con_su_evento_y_su_correlacion(telemetria: Any) -> None:
    ok, falla, rechazo = _evento(attempts=1), _evento("RecaudoRechazado"), _evento(attempts=2)
    repositorio = RepositorioFalso()
    repositorio.agregar(ok, falla, rechazo)
    publicador = PublicadorFalso(
        fallas={falla.event_id: TimeoutError(), rechazo.event_id: _error_aws("InvalidParameter")}
    )

    await _relay(repositorio, publicador).ejecutar_una_vez()

    registros = {
        r["event_id"]: r for r in telemetria.logs() if r.get("operation") == outbox.OPERACION
    }
    assert registros[str(ok.event_id)]["result"] == "ok"
    assert registros[str(ok.event_id)]["event_type"] == "OfertaAceptada"
    assert registros[str(ok.event_id)]["attempts"] == 1
    assert registros[str(ok.event_id)]["correlation_id"] == "corr-sintetico-01"
    assert isinstance(registros[str(ok.event_id)]["duration_ms"], int | float)
    assert "failure_scope" not in registros[str(ok.event_id)]
    assert registros[str(falla.event_id)]["result"] == "error"
    assert registros[str(falla.event_id)]["attempts"] == 0
    assert registros[str(falla.event_id)]["error_type"] == "TimeoutError"
    assert registros[str(falla.event_id)]["failure_scope"] == "general"
    assert registros[str(rechazo.event_id)]["attempts"] == 3
    assert registros[str(rechazo.event_id)]["failure_scope"] == "fila"
    puntos = telemetria.puntos("OperationDuration")
    assert any(p.attributes == {"Operation": outbox.OPERACION} for p in puntos)


# --- Bucle -----------------------------------------------------------------------------------
@pytest.mark.anyio
async def test_el_bucle_termina_al_recibir_la_parada_sin_esperar_el_intervalo() -> None:
    repositorio, publicador = RepositorioFalso(), PublicadorFalso()
    relay = _relay(repositorio, publicador, intervalo_s=30)
    parada = asyncio.Event()

    async def detener() -> None:
        await asyncio.sleep(0.05)
        parada.set()

    with anyio.fail_after(2):
        await asyncio.gather(relay.ejecutar(parada), detener())
    assert repositorio.lecturas


@pytest.mark.anyio
async def test_con_la_parada_ya_recibida_no_ejecuta_ningun_ciclo() -> None:
    repositorio = RepositorioFalso()
    parada = asyncio.Event()
    parada.set()

    await _relay(repositorio, PublicadorFalso()).ejecutar(parada)

    assert repositorio.lecturas == []


@pytest.mark.anyio
async def test_con_el_lote_lleno_sigue_sin_esperar_el_intervalo() -> None:
    repositorio, publicador = RepositorioFalso(), PublicadorFalso()
    repositorio.agregar(*(_evento() for _ in range(5)))
    relay = _relay(repositorio, publicador, lote=2, intervalo_s=30)
    parada = asyncio.Event()

    async def detener_al_vaciar() -> None:
        while repositorio.pendientes():
            await asyncio.sleep(0.01)
        parada.set()

    with anyio.fail_after(2):
        await asyncio.gather(relay.ejecutar(parada), detener_al_vaciar())
    assert len(publicador.publicados) == 5


@pytest.mark.anyio
async def test_el_bucle_sobrevive_a_un_ciclo_fallido(telemetria: Any) -> None:
    repositorio, publicador = RepositorioFalso(falla_al_abrir=1), PublicadorFalso()
    repositorio.agregar(_evento())
    relay = _relay(repositorio, publicador, intervalo_s=0.01, intervalo_max_s=0.02)
    parada = asyncio.Event()

    async def detener_al_publicar() -> None:
        while not publicador.publicados:
            await asyncio.sleep(0.01)
        parada.set()

    with anyio.fail_after(2):
        await asyncio.gather(relay.ejecutar(parada), detener_al_publicar())
    fallos = [r for r in telemetria.logs() if r.get("message") == outbox.MENSAJE_CICLO_FALLIDO]
    assert [(r["level"], r["error_type"]) for r in fallos] == [("ERROR", "ConnectionError")]


@pytest.mark.parametrize(
    ("fallos", "esperado"),
    [(0, 1.0), (1, 2.0), (2, 4.0), (5, 32.0), (6, 60.0), (10_000, 60.0)],
)
def test_la_espera_crece_con_los_ciclos_fallidos_hasta_el_maximo(
    fallos: int, esperado: float
) -> None:
    assert espera_tras_fallos(1.0, 60.0, fallos) == esperado


# --- Adaptador PostgreSQL -----------------------------------------------------------------------
class CursorFalso:
    def __init__(self, filas: list[tuple[Any, ...]]) -> None:
        self.filas = filas

    async def fetchall(self) -> list[tuple[Any, ...]]:
        return self.filas


class ConexionFalsa:
    def __init__(self, filas: list[tuple[Any, ...]]) -> None:
        self.filas = filas
        self.sentencias: list[tuple[str, Any]] = []
        self.transacciones: list[str] = []

    async def execute(self, consulta: Any, parametros: Any = None) -> CursorFalso:
        self.sentencias.append((consulta.as_string(), parametros))
        return CursorFalso(self.filas)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        try:
            yield
        except BaseException:
            self.transacciones.append("rollback")
            raise
        self.transacciones.append("commit")


class PoolFalso:
    def __init__(self, conexion: ConexionFalsa) -> None:
        self.conexion = conexion

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[ConexionFalsa]:
        yield self.conexion


def _fila_pg() -> tuple[Any, ...]:
    return (
        uuid4(),
        uuid4(),
        "OfertaAceptada",
        1,
        INSTANTE,
        "corr-sintetico-01",
        '{"a": 1}',
        3,
    )


@pytest.mark.anyio
async def test_outbox_pg_lee_pendientes_con_bloqueo_y_orden() -> None:
    fila = _fila_pg()
    conexion = ConexionFalsa([fila])

    async with OutboxPg(PoolFalso(conexion)).transaccion() as tx:  # type: ignore[arg-type]
        eventos = await tx.pendientes(25, 9)

    assert eventos == [EventoPendiente(*fila)]
    consulta, parametros = conexion.sentencias[0]
    assert parametros == (9, 25)
    normalizada = " ".join(consulta.split())
    assert 'FROM "outbox"' in normalizada
    assert "payload::text" in normalizada
    assert "WHERE published_at IS NULL AND attempts < %s" in normalizada
    assert "ORDER BY occurred_at, event_id LIMIT %s FOR UPDATE SKIP LOCKED" in normalizada
    assert conexion.transacciones == ["commit"]


@pytest.mark.anyio
async def test_outbox_pg_marca_publicados_y_registra_fallos_en_la_tabla_indicada() -> None:
    conexion = ConexionFalsa([])
    ids = [uuid4(), uuid4()]

    async with OutboxPg(PoolFalso(conexion), tabla="outbox_otro").transaccion() as tx:  # type: ignore[arg-type]
        await tx.marcar_publicados(ids, INSTANTE)
        await tx.registrar_fallo(ids[0], "TimeoutError", definitivo=False)
        await tx.registrar_fallo(ids[1], "ClientError: InvalidParameter", definitivo=True)

    (marcar, p_marcar), (general, p_general), (de_fila, p_de_fila) = conexion.sentencias
    assert " ".join(marcar.split()) == (
        'UPDATE "outbox_otro" SET published_at = %s WHERE event_id = ANY(%s)'
    )
    assert p_marcar == (INSTANTE, ids)
    assert (
        " ".join(general.split())
        == " ".join(de_fila.split())
        == (
            'UPDATE "outbox_otro" SET attempts = attempts + %s, last_error = %s WHERE event_id = %s'
        )
    )
    assert p_general == (0, "TimeoutError", ids[0])
    assert p_de_fila == (1, "ClientError: InvalidParameter", ids[1])


@pytest.mark.anyio
async def test_outbox_pg_hace_rollback_si_el_ciclo_falla() -> None:
    conexion = ConexionFalsa([])

    with pytest.raises(ValueError, match="caída"):
        async with OutboxPg(PoolFalso(conexion)).transaccion():  # type: ignore[arg-type]
            raise ValueError("caída")

    assert conexion.transacciones == ["rollback"]
