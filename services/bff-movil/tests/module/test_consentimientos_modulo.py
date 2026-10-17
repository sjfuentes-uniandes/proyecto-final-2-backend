"""Módulo `bff-movil` completo contra un `clientes` simulado por HTTP real (HU-M08).

A diferencia de las unitarias (respx), aquí no se inyecta transporte: el BFF usa su pila HTTP
de producción (`correlation.cliente_async`, timeouts, JWKS) contra un servidor uvicorn local que
simula `clientes` y el JWKS del pool de clientes. El simulado aplica las reglas que importan al
BFF (titular por `X-Actor-Id`, idempotencia por titular, 409, 422 con todos los campos, 404 de
lo ajeno) y puede caerse o volverse lento. Solo datos sintéticos.
"""

import asyncio
import socket
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
import uvicorn
from bff_movil.config import Settings
from bff_movil.main import crear_app
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

KID = "kid-modulo"
SUB = "22222222-2222-4222-8222-222222222222"
SUB_AJENO = "33333333-3333-4333-8333-333333333333"
SUB_SIN_VERIFICAR = "44444444-4444-4444-8444-444444444444"
VERSION = "2026-10-17.1"
CUERPO = {
    "proposito": "PERFILAMIENTO_PRECIO",
    "alcances": ["FA_PRODUCTOS_VIGENTES"],
    "vigenciaMeses": 12,
    "versionTexto": VERSION,
    "idioma": "es-CO",
}


@dataclass
class ClientesSimulado:
    """Estado del `clientes` simulado; cada prueba lo reinicia."""

    llave: rsa.RSAPrivateKey
    consentimientos: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    claves: dict[tuple[str, str], tuple[dict[str, Any], str]] = field(default_factory=dict)
    recibidas: list[dict[str, Any]] = field(default_factory=list)
    caido: bool = False
    retraso_s: float = 0.0

    def reiniciar(self) -> None:
        self.consentimientos.clear()
        self.claves.clear()
        self.recibidas.clear()
        self.caido = False
        self.retraso_s = 0.0

    def total(self) -> int:
        return sum(len(lista) for lista in self.consentimientos.values())


def _error(estado: int, codigo: str, detalles: dict[str, Any] | None = None) -> JSONResponse:
    cuerpo = {
        "codigo": codigo,
        "mensaje": codigo,
        "correlationId": None,
        "detalles": detalles or {},
    }
    return JSONResponse(cuerpo, status_code=estado)


def _app_clientes(estado: ClientesSimulado) -> FastAPI:
    app = FastAPI()
    publica = jwt.algorithms.RSAAlgorithm.to_jwk(estado.llave.public_key(), as_dict=True)

    @app.get("/pool-clientes/.well-known/jwks.json")
    async def jwks() -> dict[str, Any]:
        return {"keys": [{**publica, "kid": KID, "use": "sig", "alg": "RS256"}]}

    @app.middleware("http")
    async def registrar(request: Request, siguiente: Any) -> Any:
        if request.url.path.startswith("/v1/"):
            estado.recibidas.append(
                {"ruta": request.url.path, "metodo": request.method, **dict(request.headers)}
            )
            if estado.caido:
                return _error(503, "DEPENDENCIA_NO_DISPONIBLE")
        return await siguiente(request)

    @app.post("/v1/clientes/actual/consentimientos")
    async def otorgar(
        request: Request,
        x_actor_id: str = Header(),
        x_actor_rol: str = Header(),
        idempotency_key: str = Header(),
    ) -> JSONResponse:
        if x_actor_rol != "CLIENTE":
            return _error(403, "ROL_NO_AUTORIZADO")
        cuerpo = await request.json()
        repetida = estado.claves.get((x_actor_id, idempotency_key))
        if repetida is not None:
            consentimiento, huella = repetida
            if huella != repr(sorted(cuerpo.items())):
                return _error(422, "IDEMPOTENCIA_CONFLICTO")
            return JSONResponse(consentimiento, headers={"Idempotent-Replayed": "true"})
        faltan = [c for c in ("proposito", "alcances", "vigenciaMeses") if not cuerpo.get(c)]
        if faltan:
            return _error(422, "SOLICITUD_INVALIDA", {"campos": faltan})
        if x_actor_id == SUB_SIN_VERIFICAR:
            return _error(403, "IDENTIDAD_NO_VERIFICADA")
        propios = estado.consentimientos.setdefault(x_actor_id, [])
        activo = next((c for c in propios if c["proposito"] == cuerpo["proposito"]), None)
        if activo is not None:
            return _error(
                409, "CONSENTIMIENTO_YA_VIGENTE", {"consentimientoId": activo["consentimientoId"]}
            )
        otorgado = datetime.now(UTC)
        consentimiento = {
            "consentimientoId": str(uuid.uuid4()),
            "estado": "ACTIVO",
            "proposito": cuerpo["proposito"],
            "alcances": cuerpo["alcances"],
            "versionTexto": cuerpo["versionTexto"],
            "idioma": cuerpo["idioma"],
            "otorgadoEn": otorgado.isoformat(),
            "vigenteHasta": (otorgado + timedelta(days=365)).isoformat(),
            "revocadoEn": None,
        }
        # Como en `clientes`, el dato queda escrito antes de responder: si la respuesta llega
        # tarde, la repetición con la misma clave lo devuelve.
        propios.append(consentimiento)
        estado.claves[(x_actor_id, idempotency_key)] = (
            consentimiento,
            repr(sorted(cuerpo.items())),
        )
        if estado.retraso_s:
            await asyncio.sleep(estado.retraso_s)
        return JSONResponse(consentimiento, status_code=201)

    @app.get("/v1/clientes/actual/consentimientos")
    async def listar(x_actor_id: str = Header()) -> JSONResponse:
        if estado.retraso_s:
            await asyncio.sleep(estado.retraso_s)
        return JSONResponse(list(reversed(estado.consentimientos.get(x_actor_id, []))))

    @app.get("/v1/clientes/actual/consentimientos/{consentimiento_id}")
    async def detalle(consentimiento_id: str, x_actor_id: str = Header()) -> JSONResponse:
        for consentimiento in estado.consentimientos.get(x_actor_id, []):
            if consentimiento["consentimientoId"] == consentimiento_id:
                return JSONResponse(consentimiento)
        return _error(404, "CONSENTIMIENTO_NO_ENCONTRADO")

    @app.get("/v1/consentimientos/textos/vigente")
    async def texto(proposito: str | None = None, idioma: str | None = None) -> JSONResponse:
        if proposito != "PERFILAMIENTO_PRECIO" or idioma not in ("es-CO", "en-US"):
            return _error(422, "SOLICITUD_INVALIDA", {"campos": ["proposito", "idioma"]})
        return JSONResponse(
            {
                "proposito": proposito,
                "idioma": idioma,
                "versionTexto": VERSION,
                "titulo": "Autorización de ejemplo",
                "cuerpo": "Texto ilustrativo.",
                "aviso": "Sin validez legal",
                "vigenciasMeses": [6, 12],
                "vigenciaDefectoMeses": 12,
            }
        )

    return app


@dataclass
class Modulo:
    bff: TestClient
    clientes: ClientesSimulado
    emisor: str

    def token(self, sub: str = SUB) -> str:
        ahora = datetime.now(UTC)
        claims = {
            "sub": sub,
            "iss": self.emisor,
            "token_use": "access",
            "iat": ahora,
            "exp": ahora + timedelta(minutes=30),
        }
        return jwt.encode(claims, self.clientes.llave, algorithm="RS256", headers={"kid": KID})

    def pedir(self, metodo: str, ruta: str, sub: str = SUB, **kwargs: Any) -> Any:
        cabeceras = {
            "Authorization": f"Bearer {self.token(sub)}",
            "X-Authenticated-Sub": sub,
            **kwargs.pop("headers", {}),
        }
        return self.bff.request(metodo, ruta, headers=cabeceras, **kwargs)

    def otorgar(self, sub: str = SUB, clave: str | None = None, cuerpo: Any = None) -> Any:
        return self.pedir(
            "POST",
            "/v1/consentimientos",
            sub,
            json=CUERPO if cuerpo is None else cuerpo,
            headers={"Idempotency-Key": clave or str(uuid.uuid4())},
        )


@pytest.fixture(scope="module")
def servidor() -> Iterator[tuple[str, ClientesSimulado]]:
    estado = ClientesSimulado(rsa.generate_private_key(public_exponent=65537, key_size=2048))
    zocalo = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    zocalo.bind(("127.0.0.1", 0))
    puerto = zocalo.getsockname()[1]
    servidor = uvicorn.Server(
        uvicorn.Config(_app_clientes(estado), log_level="warning", lifespan="off")
    )
    hilo = threading.Thread(target=servidor.run, kwargs={"sockets": [zocalo]}, daemon=True)
    hilo.start()
    limite = time.monotonic() + 10
    while not servidor.started:
        assert time.monotonic() < limite, "el clientes simulado no arrancó"
        time.sleep(0.02)
    yield f"http://127.0.0.1:{puerto}", estado
    servidor.should_exit = True
    hilo.join(timeout=5)


def _modulo(base: str, estado: ClientesSimulado, **ajustes: Any) -> Iterator[Modulo]:
    estado.reiniciar()
    emisor = f"{base}/pool-clientes"
    settings = Settings(
        ENVIRONMENT="pruebas",
        JWT_ISSUERS={"customers": emisor},
        CLIENTES_URL=ajustes.pop("clientes_url", base),
        **ajustes,
    )
    with TestClient(crear_app(settings), raise_server_exceptions=False) as bff:
        yield Modulo(bff, estado, emisor)


@pytest.fixture
def modulo(servidor: tuple[str, ClientesSimulado]) -> Iterator[Modulo]:
    yield from _modulo(*servidor)


@pytest.fixture
def modulo_ajustado(servidor: tuple[str, ClientesSimulado]) -> Any:
    generadores: list[Iterator[Modulo]] = []

    def crear(**ajustes: Any) -> Modulo:
        generador = _modulo(*servidor, **ajustes)
        generadores.append(generador)
        return next(generador)

    yield crear
    for generador in generadores:
        generador.close()


# --- Caso feliz ------------------------------------------------------------------------------


@pytest.mark.hu("M08", "AC1")
def test_otorgar_y_consultar_el_comprobante_de_punta_a_punta(modulo: Modulo) -> None:
    correlacion = "corr-modulo-m08"

    otorgado = modulo.otorgar()
    lista = modulo.pedir("GET", "/v1/consentimientos", headers={"X-Correlation-Id": correlacion})
    consentimiento_id = otorgado.json()["consentimientoId"]
    detalle = modulo.pedir("GET", f"/v1/consentimientos/{consentimiento_id}")

    assert otorgado.status_code == 201
    assert otorgado.json()["estado"] == "ACTIVO"
    assert lista.status_code == 200
    assert [c["consentimientoId"] for c in lista.json()] == [consentimiento_id]
    assert detalle.status_code == 200
    assert detalle.json() == otorgado.json()
    assert detalle.json()["versionTexto"] == VERSION
    escritura = next(r for r in modulo.clientes.recibidas if r["metodo"] == "POST")
    assert escritura["x-actor-id"] == SUB
    assert escritura["x-actor-rol"] == "CLIENTE"
    assert "idempotency-key" in escritura
    lectura = next(r for r in modulo.clientes.recibidas if r["metodo"] == "GET")
    assert lectura["x-correlation-id"] == correlacion
    assert lista.headers["X-Correlation-Id"] == correlacion


@pytest.mark.hu("M08", "AC1")
def test_texto_vigente_de_punta_a_punta(modulo: Modulo) -> None:
    respuesta = modulo.pedir(
        "GET",
        "/v1/consentimientos/texto-vigente",
        params={"proposito": "PERFILAMIENTO_PRECIO", "idioma": "en-US"},
    )

    assert respuesta.status_code == 200
    assert respuesta.json()["idioma"] == "en-US"
    assert respuesta.json()["vigenciasMeses"] == [6, 12]


@pytest.mark.hu("M08", "AC4")
def test_repetir_la_misma_clave_no_crea_otro_consentimiento(modulo: Modulo) -> None:
    clave = str(uuid.uuid4())

    primera = modulo.otorgar(clave=clave)
    segunda = modulo.otorgar(clave=clave)

    assert (primera.status_code, segunda.status_code) == (201, 200)
    assert segunda.headers["Idempotent-Replayed"] == "true"
    assert segunda.json()["consentimientoId"] == primera.json()["consentimientoId"]
    assert modulo.clientes.total() == 1


def test_la_misma_clave_con_otro_cuerpo_es_conflicto(modulo: Modulo) -> None:
    clave = str(uuid.uuid4())
    modulo.otorgar(clave=clave)

    respuesta = modulo.otorgar(clave=clave, cuerpo={**CUERPO, "vigenciaMeses": 6})

    assert respuesta.status_code == 422
    assert respuesta.json()["codigo"] == "IDEMPOTENCIA_CONFLICTO"


# --- Fallos controlados ----------------------------------------------------------------------


@pytest.mark.hu("M08", "AC3")
def test_solicitud_incompleta_informa_todos_los_campos_sin_crear_nada(modulo: Modulo) -> None:
    respuesta = modulo.otorgar(cuerpo={"versionTexto": VERSION, "idioma": "es-CO"})

    assert respuesta.status_code == 422
    assert respuesta.json()["codigo"] == "SOLICITUD_INVALIDA"
    assert respuesta.json()["detalles"]["campos"] == ["proposito", "alcances", "vigenciaMeses"]
    assert modulo.clientes.total() == 0


def test_un_segundo_consentimiento_vigente_es_409_con_el_existente(modulo: Modulo) -> None:
    primero = modulo.otorgar()

    respuesta = modulo.otorgar()

    assert respuesta.status_code == 409
    assert respuesta.json()["codigo"] == "CONSENTIMIENTO_YA_VIGENTE"
    assert respuesta.json()["detalles"] == {"consentimientoId": primero.json()["consentimientoId"]}


def test_identidad_sin_verificar_no_otorga(modulo: Modulo) -> None:
    respuesta = modulo.otorgar(sub=SUB_SIN_VERIFICAR)

    assert respuesta.status_code == 403
    assert respuesta.json()["codigo"] == "IDENTIDAD_NO_VERIFICADA"
    assert modulo.clientes.total() == 0


def test_un_cliente_no_ve_consentimientos_ajenos(modulo: Modulo) -> None:
    ajeno = modulo.otorgar(sub=SUB_AJENO).json()["consentimientoId"]

    detalle = modulo.pedir("GET", f"/v1/consentimientos/{ajeno}")
    lista = modulo.pedir("GET", "/v1/consentimientos")

    assert detalle.status_code == 404
    assert detalle.json()["codigo"] == "CONSENTIMIENTO_NO_ENCONTRADO"
    assert lista.json() == []


def test_un_token_de_otro_emisor_no_llega_a_clientes(modulo: Modulo) -> None:
    otra = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ahora = datetime.now(UTC)
    token = jwt.encode(
        {
            "sub": SUB,
            "iss": modulo.emisor,
            "token_use": "access",
            "iat": ahora,
            "exp": ahora + timedelta(minutes=5),
        },
        otra,
        algorithm="RS256",
        headers={"kid": KID},
    )

    respuesta = modulo.bff.get(
        "/v1/consentimientos",
        headers={"Authorization": f"Bearer {token}", "X-Authenticated-Sub": SUB},
    )

    assert respuesta.status_code == 401
    assert modulo.clientes.recibidas == []


@pytest.mark.hu("M08", "AC2")
def test_clientes_fallando_no_otorga_y_responde_503(modulo: Modulo) -> None:
    modulo.clientes.caido = True

    respuesta = modulo.otorgar()

    assert respuesta.status_code == 503
    assert respuesta.json()["codigo"] == "SERVICIO_NO_DISPONIBLE"
    assert modulo.clientes.total() == 0
    assert len(modulo.clientes.recibidas) == 1


def test_clientes_inalcanzable_responde_503(modulo_ajustado: Any) -> None:
    libre = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    libre.bind(("127.0.0.1", 0))
    puerto = libre.getsockname()[1]
    libre.close()
    modulo = modulo_ajustado(clientes_url=f"http://127.0.0.1:{puerto}")

    respuesta = modulo.pedir("GET", "/v1/consentimientos")

    assert respuesta.status_code == 503
    assert respuesta.json()["codigo"] == "SERVICIO_NO_DISPONIBLE"


def test_clientes_lento_corta_en_el_presupuesto_y_el_reintento_devuelve_el_mismo(
    modulo_ajustado: Any,
) -> None:
    """R8, fallo controlado: la respuesta se pierde por el timeout, pero repetir con la misma
    clave devuelve el consentimiento ya escrito y no se crea un segundo."""
    modulo = modulo_ajustado(otorgamiento_timeout_ms=200)
    modulo.clientes.retraso_s = 1.0
    clave = str(uuid.uuid4())

    inicio = time.monotonic()
    lenta = modulo.otorgar(clave=clave)
    transcurrido = time.monotonic() - inicio
    modulo.clientes.retraso_s = 0.0
    reintento = modulo.otorgar(clave=clave)

    assert lenta.status_code == 503
    assert lenta.json()["codigo"] == "SERVICIO_NO_DISPONIBLE"
    assert transcurrido < 0.9
    assert reintento.status_code == 200
    assert reintento.headers["Idempotent-Replayed"] == "true"
    assert modulo.clientes.total() == 1
    assert sum(r["metodo"] == "POST" for r in modulo.clientes.recibidas) == 2


def test_una_lectura_lenta_corta_en_el_presupuesto_de_clientes(modulo_ajustado: Any) -> None:
    modulo = modulo_ajustado(clientes_timeout_ms=150)
    modulo.clientes.retraso_s = 1.0

    inicio = time.monotonic()
    respuesta = modulo.pedir("GET", "/v1/consentimientos")

    assert respuesta.status_code == 503
    assert time.monotonic() - inicio < 0.9
    assert len(modulo.clientes.recibidas) == 1
