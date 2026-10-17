"""R8 (HU-M08), versión de módulo: otorgamiento de la app hasta la base de `clientes`.

Corre en CI sin AWS (decisión P1-5). Nada se simula entre la app y la base:
- `bff-movil` real (su pila HTTP de producción, sin transporte hacia `clientes` inyectado);
- `clientes` real, servido por uvicorn en un puerto local, con su `AsyncConnectionPool`;
- PostgreSQL real (Testcontainers), migrado por el arranque de `clientes`.

Solo se sustituye lo que en AWS es de terceros: el JWKS de Cognito (llaves efímeras) y
`adaptador-identidad`, que otorgar no usa. El titular se siembra como fila de `cliente`
`VERIFICADO`, como lo deja HU-M06. Solo datos sintéticos.

La versión para QA, contra el ambiente desplegado, está en `tests/e2e/test_hu_m08_r8_otorgamiento.py`.
"""

import asyncio
import json
import os
import socket
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import jwt
import psycopg
import pytest
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

docker = pytest.importorskip("docker")

EMISOR = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_r8"
KID = "kid-r8"
SUB_A = "a8a8a8a8-0000-4000-8000-00000000000a"
SUB_B = "b8b8b8b8-0000-4000-8000-00000000000b"
RUTA = "/v1/consentimientos"
ESQUEMA_EVENTO = json.loads(
    (
        Path(__file__).parents[4] / "contracts" / "events" / "ConsentimientoOtorgado.v1.schema.json"
    ).read_text("utf-8")
)


# --- Infraestructura del recorrido -----------------------------------------------------------


@pytest.fixture(scope="module")
def base() -> Iterator[str]:
    # Ryuk no arranca en Docker Desktop; en la CI ya viene deshabilitado.
    os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")
    from testcontainers.community.postgres import PostgresContainer

    try:
        docker.from_env().ping()
    except docker.errors.DockerException:
        pytest.skip("Docker no está disponible para testcontainers")
    with PostgresContainer("postgres:16", driver=None) as contenedor:
        yield contenedor.get_connection_url()


class ServidorClientes:
    """`clientes` tal como se despliega, en un hilo propio.

    psycopg asíncrono no corre sobre el ProactorEventLoop de Windows: el servidor usa un
    SelectorEventLoop propio (en Linux es el bucle por defecto).
    """

    def __init__(self, base: str) -> None:
        from clientes.config import Settings
        from clientes.main import crear_app

        async def sin_llamadas(request: httpx.Request) -> httpx.Response:
            raise AssertionError("otorgar no invoca a adaptador-identidad")

        app = crear_app(
            Settings(SOLVENTA_DATABASE_URL=base), transport=httpx.MockTransport(sin_llamadas)
        )
        self._zocalo = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._zocalo.bind(("127.0.0.1", 0))
        self.url = f"http://127.0.0.1:{self._zocalo.getsockname()[1]}"
        self._servidor = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
        self._hilo = threading.Thread(target=self._correr, daemon=True)

    def _correr(self) -> None:
        asyncio.run(
            self._servidor.serve(sockets=[self._zocalo]), loop_factory=asyncio.SelectorEventLoop
        )

    def arrancar(self) -> "ServidorClientes":
        self._hilo.start()
        limite = time.monotonic() + 30
        while not self._servidor.started:
            assert self._hilo.is_alive(), "clientes no arrancó (ver el log del servidor)"
            assert time.monotonic() < limite, "clientes no arrancó a tiempo"
            time.sleep(0.05)
        return self

    def apagar(self) -> None:
        # Cierra el ciclo de vida (pool incluido) y libera el puerto: queda "apagado".
        self._servidor.should_exit = True
        self._hilo.join(timeout=15)
        assert not self._hilo.is_alive(), "clientes no se apagó"


@pytest.fixture(scope="module")
def clientes(base: str) -> Iterator[ServidorClientes]:
    servidor = ServidorClientes(base).arrancar()
    yield servidor
    servidor.apagar()


class _Enrutador(httpx.AsyncBaseTransport):
    """El JWKS de Cognito es simulado; todo lo demás (`clientes`) va por la red real."""

    def __init__(self, llave: rsa.RSAPrivateKey) -> None:
        publica = jwt.algorithms.RSAAlgorithm.to_jwk(llave.public_key(), as_dict=True)
        self._jwks = {"keys": [{**publica, "kid": KID, "use": "sig", "alg": "RS256"}]}
        self._red = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if str(request.url) == f"{EMISOR}/.well-known/jwks.json":
            return httpx.Response(200, json=self._jwks)
        return await self._red.handle_async_request(request)

    async def aclose(self) -> None:
        await self._red.aclose()


@dataclass
class App:
    """La app móvil vista desde el BFF: token del pool de clientes y `X-Authenticated-Sub`."""

    bff: TestClient
    llave: rsa.RSAPrivateKey

    def pedir(self, metodo: str, ruta: str, sub: str = SUB_A, **kwargs: Any) -> httpx.Response:
        ahora = datetime.now(UTC)
        token = jwt.encode(
            {
                "sub": sub,
                "iss": EMISOR,
                "token_use": "access",
                "iat": ahora,
                "exp": ahora + timedelta(minutes=30),
            },
            self.llave,
            algorithm="RS256",
            headers={"kid": KID},
        )
        cabeceras = {
            "Authorization": f"Bearer {token}",
            "X-Authenticated-Sub": sub,
            **kwargs.pop("headers", {}),
        }
        return self.bff.request(metodo, ruta, headers=cabeceras, **kwargs)

    def otorgar(
        self, cuerpo: dict[str, Any], sub: str = SUB_A, clave: str | None = None, **cabeceras: str
    ) -> httpx.Response:
        return self.pedir(
            "POST",
            RUTA,
            sub,
            json=cuerpo,
            headers={"Idempotency-Key": clave or str(uuid.uuid4()), **cabeceras},
        )

    def cuerpo(self, **cambios: Any) -> dict[str, Any]:
        """Lo que la app envía tras mostrar el texto vigente: su versión viene del servidor."""
        texto = self.pedir(
            "GET",
            f"{RUTA}/texto-vigente",
            params={"proposito": "PERFILAMIENTO_PRECIO", "idioma": "es-CO"},
        )
        assert texto.status_code == 200, texto.text
        return {
            "proposito": "PERFILAMIENTO_PRECIO",
            "alcances": ["FA_PRODUCTOS_VIGENTES"],
            "vigenciaMeses": 12,
            "versionTexto": texto.json()["versionTexto"],
            "idioma": "es-CO",
            **cambios,
        }


@contextmanager
def _bff(url_clientes: str) -> Iterator[App]:
    from bff_movil.config import Settings
    from bff_movil.main import crear_app

    llave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    settings = Settings(
        ENVIRONMENT="pruebas", JWT_ISSUERS={"customers": EMISOR}, CLIENTES_URL=url_clientes
    )
    with TestClient(
        crear_app(settings, transport=_Enrutador(llave)), raise_server_exceptions=False
    ) as bff:
        yield App(bff, llave)


@pytest.fixture
def app(clientes: ServidorClientes, base: str) -> Iterator[App]:
    _sql(base, "TRUNCATE consentimiento_idempotencia, consentimiento, outbox, cliente")
    with _bff(clientes.url) as aplicacion:
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


def test_r8_fallo_clientes_apagado_responde_503_y_no_crea_nada(base: str) -> None:
    """`clientes` estuvo arriba en ese puerto y se apagó: el BFF falla cerrado."""
    _sql(base, "TRUNCATE consentimiento_idempotencia, consentimiento, outbox, cliente")
    _titular(base)
    servidor = ServidorClientes(base).arrancar()
    url = servidor.url
    with _bff(url) as app:
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
