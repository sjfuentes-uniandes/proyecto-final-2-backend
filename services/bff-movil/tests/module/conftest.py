"""Infraestructura de los recorridos R8 de módulo (HU-M08 y HU-M09).

Nada se simula entre la app y la base:
- `bff-movil` real (su pila HTTP de producción, sin transporte hacia `clientes` inyectado);
- `clientes` real, servido por uvicorn en un puerto local, con su `AsyncConnectionPool`;
- PostgreSQL real (Testcontainers), migrado por el arranque de `clientes`.

Solo se sustituye lo que en AWS es de terceros: el JWKS de Cognito (llaves efímeras) y
`adaptador-identidad`, que los consentimientos no usan. El contenedor y `clientes` arrancan una
vez por sesión; cada prueba limpia las tablas con su fixture `app`. Solo datos sintéticos.
"""

import asyncio
import os
import socket
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import jwt
import pytest
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

EMISOR = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_r8"
KID = "kid-r8"
RUTA = "/v1/consentimientos"


@pytest.fixture(scope="session")
def base() -> Iterator[str]:
    # Aquí y no al importar: las demás pruebas de módulo no necesitan Docker.
    docker = pytest.importorskip("docker")
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
            raise AssertionError("los consentimientos no invocan a adaptador-identidad")

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


@pytest.fixture(scope="session")
def clientes(base: str) -> Iterator[ServidorClientes]:
    servidor = ServidorClientes(base).arrancar()
    yield servidor
    servidor.apagar()


@pytest.fixture(scope="session")
def servidor_clientes() -> type[ServidorClientes]:
    """Para las pruebas que levantan y apagan su propio `clientes` sobre la misma base."""
    return ServidorClientes


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
    """La app móvil vista desde el BFF: token del pool de clientes y `X-Authenticated-Sub`.

    `sub` es el titular por defecto de cada petición.
    """

    bff: TestClient
    llave: rsa.RSAPrivateKey
    sub: str

    def token(self, sub: str) -> str:
        ahora = datetime.now(UTC)
        return jwt.encode(
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

    def pedir(
        self, metodo: str, ruta: str, sub: str | None = None, **kwargs: Any
    ) -> httpx.Response:
        titular = sub or self.sub
        cabeceras = {
            "Authorization": f"Bearer {self.token(titular)}",
            "X-Authenticated-Sub": titular,
            **kwargs.pop("headers", {}),
        }
        return self.bff.request(metodo, ruta, headers=cabeceras, **kwargs)

    def otorgar(
        self,
        cuerpo: dict[str, Any],
        sub: str | None = None,
        clave: str | None = None,
        **cabeceras: str,
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
def _bff(url_clientes: str, sub: str, **ajustes: Any) -> Iterator[App]:
    from bff_movil.config import Settings
    from bff_movil.main import crear_app

    llave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    settings = Settings(
        ENVIRONMENT="pruebas",
        JWT_ISSUERS={"customers": EMISOR},
        CLIENTES_URL=url_clientes,
        **ajustes,
    )
    with TestClient(
        crear_app(settings, transport=_Enrutador(llave)), raise_server_exceptions=False
    ) as bff:
        yield App(bff, llave, sub)


@pytest.fixture(scope="session")
def abrir_bff() -> Callable[..., AbstractContextManager[App]]:
    """`abrir_bff(url_clientes, sub, **ajustes)`: el BFF real apuntando a ese `clientes`, con
    `sub` como titular por defecto y ajustes de configuración (por ejemplo, timeouts)."""
    return _bff
