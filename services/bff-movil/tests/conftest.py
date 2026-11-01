"""Fixtures de la sesión: llaves RSA efímeras, JWKS y clientes simulados con respx.

Los tokens y las llaves se generan en cada ejecución; nunca se usan tokens reales.
"""

import io
import json
import logging
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import jwt
import pytest
import respx
from bff_movil.config import Settings
from bff_movil.domain.sesion.errores import ClienteNoEncontrado
from bff_movil.main import crear_app
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from solventa_common.logging import FormateadorJson

EMISOR_CLIENTES = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_clientes"
EMISOR_BACKOFFICE = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_backoffice"
CLIENTES_URL = "http://clientes.test"
KID = "kid-prueba"
SUB = "11111111-1111-4111-8111-111111111111"
# C01 de contracts/datos-sinteticos/clientes-perfilamiento.json.
CLIENTE_ID = UUID("00000000-0000-4000-8000-000000000001")
CUERPO_VERIFICACION = {"selfie": "selfie-sintetica-aprobada", "pruebaDeVida": {"completado": True}}
REGISTRO_ID = UUID("00000000-0000-4000-8000-0000000000aa")
CORREO = "persona@ejemplo.co"
CUERPO_REGISTRO = {"registroId": str(REGISTRO_ID), "correo": CORREO, **CUERPO_VERIFICACION}
TOKEN_REGISTRO = "v1.token-sintetico.firma-sintetica"
CONSENTIMIENTO_ID = UUID("00000000-0000-4000-9000-000000000001")
IDEMPOTENCY_KEY = "6f1c2b9e-3d4a-4e8f-9b7c-1a2b3c4d5e6f"
CUERPO_OTORGAR = {
    "proposito": "PERFILAMIENTO_PRECIO",
    "alcances": ["FA_PRODUCTOS_VIGENTES"],
    "vigenciaMeses": 12,
    "versionTexto": "2026-10-17.1",
    "idioma": "es-CO",
}


def consentimiento_json(**cambios: Any) -> dict[str, Any]:
    """Consentimiento sintético tal como lo devuelve `clientes`."""
    return {
        "consentimientoId": str(CONSENTIMIENTO_ID),
        "estado": "ACTIVO",
        "proposito": "PERFILAMIENTO_PRECIO",
        "alcances": ["FA_PRODUCTOS_VIGENTES"],
        "versionTexto": "2026-10-17.1",
        "idioma": "es-CO",
        "otorgadoEn": "2026-10-17T15:00:00Z",
        "vigenteHasta": "2027-10-17T23:59:59Z",
        "revocadoEn": None,
        **cambios,
    }


def historial_json(**cambios: Any) -> dict[str, Any]:
    """Historial sintético de un consentimiento revocado, como lo devuelve `clientes`."""
    return {
        "consentimientoId": str(CONSENTIMIENTO_ID),
        "estado": "REVOCADO",
        "hitos": [
            {"tipo": "OTORGADO", "ocurridoEn": "2026-10-17T15:00:00Z"},
            {"tipo": "REVOCADO", "ocurridoEn": "2026-11-02T08:15:00Z"},
        ],
        **cambios,
    }


TEXTO_VIGENTE = {
    "proposito": "PERFILAMIENTO_PRECIO",
    "idioma": "es-CO",
    "versionTexto": "2026-10-17.1",
    "titulo": "Autorización de ejemplo",
    "cuerpo": "Texto ilustrativo.",
    "aviso": "Sin validez legal",
    "vigenciasMeses": [6, 12],
    "vigenciaDefectoMeses": 12,
}


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "hu(historia, criterio): criterio de aceptación que cubre la prueba (Q2)"
    )


class ResolutorFalso:
    """ResolutorCliente con una relación fija sub -> clienteId."""

    def __init__(self, relaciones: dict[str, UUID]) -> None:
        self._relaciones = relaciones

    async def resolver(self, sub: str) -> UUID:
        try:
            return self._relaciones[sub]
        except KeyError as error:
            raise ClienteNoEncontrado() from error


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session")
def llave_privada() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="session")
def otra_llave_privada() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwk(llave: rsa.RSAPrivateKey, kid: str) -> dict[str, Any]:
    publica = jwt.algorithms.RSAAlgorithm.to_jwk(llave.public_key(), as_dict=True)
    return {**publica, "kid": kid, "use": "sig", "alg": "RS256"}


@dataclass
class Telemetria:
    spans: InMemorySpanExporter
    lector: InMemoryMetricReader
    salida: io.StringIO

    def logs(self) -> list[dict[str, Any]]:
        return [json.loads(linea) for linea in self.salida.getvalue().splitlines() if linea.strip()]

    def texto_logs(self) -> str:
        return self.salida.getvalue()

    def texto_spans(self) -> str:
        return " ".join(
            f"{s.name} {dict(s.attributes or {})} {[dict(e.attributes or {}) for e in s.events]}"
            for s in self.spans.get_finished_spans()
        )

    def puntos(self, nombre: str, operacion: str) -> list[Any]:
        datos = self.lector.get_metrics_data()
        if datos is None:
            return []
        return [
            punto
            for recurso in datos.resource_metrics
            for alcance in recurso.scope_metrics
            for metrica in alcance.metrics
            if metrica.name == nombre
            for punto in metrica.data.data_points
            if punto.attributes.get("Operation") == operacion
        ]


@dataclass
class Entorno:
    cliente: TestClient
    jwks: respx.Route
    jwks_backoffice: respx.Route
    identidad: respx.Route
    verificacion: respx.Route
    registro_verificacion: respx.Route
    registro_vinculacion: respx.Route
    otorgamiento: respx.Route
    consentimientos: respx.Route
    consentimiento: respx.Route
    revocacion: respx.Route
    historial: respx.Route
    texto_vigente: respx.Route
    telemetria: Telemetria
    llave: rsa.RSAPrivateKey
    sub: str = SUB
    cliente_id: UUID = CLIENTE_ID
    emisor: str = EMISOR_CLIENTES
    emisor_backoffice: str = EMISOR_BACKOFFICE

    def emitir(
        self,
        *,
        llave: rsa.RSAPrivateKey | None = None,
        sub: str = SUB,
        emisor: str = EMISOR_CLIENTES,
        token_use: str = "access",
        vigencia: timedelta = timedelta(minutes=30),
        kid: str = KID,
        **extra: Any,
    ) -> str:
        ahora = datetime.now(UTC)
        claims = {
            "sub": sub,
            "iss": emisor,
            "token_use": token_use,
            "iat": ahora,
            "exp": ahora + vigencia,
            **extra,
        }
        return jwt.encode(claims, llave or self.llave, algorithm="RS256", headers={"kid": kid})

    def consultar(
        self,
        token: str | None = None,
        *,
        sub: str | None = SUB,
        encabezados: dict[str, str] | None = None,
    ) -> httpx.Response:
        cabeceras = dict(encabezados or {})
        if token is not None:
            cabeceras["Authorization"] = f"Bearer {token}"
        if sub is not None:
            cabeceras["X-Authenticated-Sub"] = sub
        return self.cliente.get("/v1/sesion", headers=cabeceras)

    def identidad_responde(self, estado: str, cliente_id: UUID = CLIENTE_ID) -> None:
        self.identidad.mock(
            return_value=httpx.Response(
                200,
                json={
                    "clienteId": str(cliente_id),
                    "estado": estado,
                    "verificadoEn": "2026-10-01T00:00:00Z" if estado == "VERIFICADO" else None,
                },
            )
        )

    def verificar(
        self,
        token: str | None = None,
        *,
        sub: str | None = SUB,
        cuerpo: dict[str, Any] | None = None,
        encabezados: dict[str, str] | None = None,
    ) -> httpx.Response:
        cabeceras = dict(encabezados or {})
        if token is not None:
            cabeceras["Authorization"] = f"Bearer {token}"
        if sub is not None:
            cabeceras["X-Authenticated-Sub"] = sub
        return self.cliente.post(
            "/v1/identidad/verificaciones",
            json=cuerpo if cuerpo is not None else CUERPO_VERIFICACION,
            headers=cabeceras,
        )

    def cuerpo_registro(self, **cambios: Any) -> dict[str, Any]:
        return {**CUERPO_REGISTRO, **cambios}

    def registrar(
        self,
        *,
        cuerpo: dict[str, Any] | None = None,
        encabezados: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Verificación previa a la cuenta: pública, sin token."""
        return self.cliente.post(
            "/v1/registro/verificaciones",
            json=cuerpo if cuerpo is not None else CUERPO_REGISTRO,
            headers=encabezados or {},
        )

    def registro_responde(
        self,
        resultado: str,
        estado: str,
        motivo: str | None = None,
        token: str | None = TOKEN_REGISTRO,
    ) -> None:
        self.registro_verificacion.mock(
            return_value=httpx.Response(
                200,
                json={
                    "clienteId": str(CLIENTE_ID),
                    "estado": estado,
                    "resultado": resultado,
                    "motivo": motivo,
                    "verificadoEn": "2026-10-01T00:00:00Z" if estado == "VERIFICADO" else None,
                    "tokenRegistro": token if estado == "VERIFICADO" else None,
                    "expiraEn": "2026-10-01T00:30:00Z" if estado == "VERIFICADO" else None,
                },
            )
        )

    def vincular(
        self,
        token: str | None = None,
        *,
        sub: str | None = SUB,
        cuerpo: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Primer ingreso: vincula la cuenta recién creada con su identidad verificada."""
        cabeceras: dict[str, str] = {}
        if token is not None:
            cabeceras["Authorization"] = f"Bearer {token}"
        if sub is not None:
            cabeceras["X-Authenticated-Sub"] = sub
        return self.cliente.post(
            "/v1/registro/vinculacion",
            json=cuerpo
            if cuerpo is not None
            else {"registroId": str(REGISTRO_ID), "tokenRegistro": TOKEN_REGISTRO},
            headers=cabeceras,
        )

    def pedir(
        self,
        metodo: str,
        ruta: str,
        token: str | None = None,
        *,
        sub: str | None = SUB,
        cuerpo: Any = None,
        encabezados: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Llamada autenticada genérica: el token se emite si no se pasa uno."""
        cabeceras = dict(encabezados or {})
        cabeceras.setdefault("Authorization", f"Bearer {token or self.emitir()}")
        if sub is not None:
            cabeceras["X-Authenticated-Sub"] = sub
        return self.cliente.request(metodo, ruta, json=cuerpo, headers=cabeceras, params=params)

    def otorgar(
        self,
        token: str | None = None,
        *,
        cuerpo: Any = None,
        clave: str | None = IDEMPOTENCY_KEY,
        encabezados: dict[str, str] | None = None,
    ) -> httpx.Response:
        cabeceras = dict(encabezados or {})
        if clave is not None:
            cabeceras["Idempotency-Key"] = clave
        return self.pedir(
            "POST",
            "/v1/consentimientos",
            token,
            cuerpo=CUERPO_OTORGAR if cuerpo is None else cuerpo,
            encabezados=cabeceras,
        )

    def verificacion_responde(self, resultado: str, estado: str, motivo: str | None = None) -> None:
        self.verificacion.mock(
            return_value=httpx.Response(
                200,
                json={
                    "clienteId": str(CLIENTE_ID),
                    "estado": estado,
                    "resultado": resultado,
                    "motivo": motivo,
                    "verificadoEn": "2026-10-01T00:00:00Z" if estado == "VERIFICADO" else None,
                },
            )
        )


@pytest.fixture
def capturar_logs() -> Iterator[Callable[[], io.StringIO]]:
    """Redirige el logger raíz a un buffer con el formateador JSON."""
    raiz = logging.getLogger()
    anteriores, nivel = list(raiz.handlers), raiz.level

    def capturar() -> io.StringIO:
        salida = io.StringIO()
        handler = logging.StreamHandler(salida)
        handler.setFormatter(FormateadorJson("bff-movil", "pruebas"))
        raiz.handlers = [handler]
        raiz.setLevel(logging.INFO)
        return salida

    yield capturar
    raiz.handlers, raiz.level = anteriores, nivel


@pytest.fixture
def crear_entorno(
    monkeypatch: pytest.MonkeyPatch,
    capturar_logs: Callable[[], io.StringIO],
    llave_privada: rsa.RSAPrivateKey,
) -> Iterator[Callable[..., Entorno]]:
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    pila = ExitStack()

    def crear(
        *,
        environment: str = "pruebas",
        con_cliente: bool = True,
        consulta_polizas: Any = None,
        **ajustes: Any,
    ) -> Entorno:
        router = pila.enter_context(respx.mock(assert_all_called=False))
        jwks = router.get(f"{EMISOR_CLIENTES}/.well-known/jwks.json").respond(
            200, json={"keys": [_jwk(llave_privada, KID)]}
        )
        jwks_backoffice = router.get(f"{EMISOR_BACKOFFICE}/.well-known/jwks.json").respond(
            200, json={"keys": [_jwk(llave_privada, KID)]}
        )
        identidad = router.get(f"{CLIENTES_URL}/v1/clientes/{CLIENTE_ID}/identidad")
        verificacion = router.post(f"{CLIENTES_URL}/v1/clientes/actual/identidad/verificaciones")
        registro_verificacion = router.post(
            f"{CLIENTES_URL}/v1/registros/{REGISTRO_ID}/verificaciones"
        )
        registro_vinculacion = router.post(f"{CLIENTES_URL}/v1/registros/{REGISTRO_ID}/vinculacion")
        consentimientos_url = f"{CLIENTES_URL}/v1/clientes/actual/consentimientos"
        otorgamiento = router.post(consentimientos_url).respond(201, json=consentimiento_json())
        consentimientos = router.get(consentimientos_url).respond(200, json=[consentimiento_json()])
        consentimiento = router.get(f"{consentimientos_url}/{CONSENTIMIENTO_ID}").respond(
            200, json=consentimiento_json()
        )
        revocacion = router.post(f"{consentimientos_url}/{CONSENTIMIENTO_ID}/revocacion").respond(
            200,
            json=consentimiento_json(estado="REVOCADO", revocadoEn="2026-11-02T08:15:00Z"),
        )
        historial = router.get(f"{consentimientos_url}/{CONSENTIMIENTO_ID}/historial").respond(
            200, json=historial_json()
        )
        texto_vigente = router.get(f"{CLIENTES_URL}/v1/consentimientos/textos/vigente").respond(
            200, json=TEXTO_VIGENTE
        )
        spans, lector = InMemorySpanExporter(), InMemoryMetricReader()
        settings = Settings(
            ENVIRONMENT=environment,
            JWT_ISSUERS={"customers": EMISOR_CLIENTES, "backoffice": EMISOR_BACKOFFICE},
            CLIENTES_URL=CLIENTES_URL,
            **ajustes,
        )
        app = crear_app(
            settings,
            resolutor=ResolutorFalso({SUB: CLIENTE_ID} if con_cliente else {}),
            consulta_polizas=consulta_polizas,
            span_exporter=spans,
            metric_readers=[lector],
        )
        # Después de crear la app: instrumentar() reconfigura los logs.
        salida = capturar_logs()
        cliente = pila.enter_context(TestClient(app, raise_server_exceptions=False))
        entorno = Entorno(
            cliente=cliente,
            jwks=jwks,
            jwks_backoffice=jwks_backoffice,
            identidad=identidad,
            verificacion=verificacion,
            registro_verificacion=registro_verificacion,
            registro_vinculacion=registro_vinculacion,
            otorgamiento=otorgamiento,
            consentimientos=consentimientos,
            consentimiento=consentimiento,
            revocacion=revocacion,
            historial=historial,
            texto_vigente=texto_vigente,
            telemetria=Telemetria(spans=spans, lector=lector, salida=salida),
            llave=llave_privada,
        )
        entorno.identidad_responde("VERIFICADO")
        entorno.verificacion_responde("APROBADO", "VERIFICADO")
        entorno.registro_responde("APROBADO", "VERIFICADO")
        registro_vinculacion.mock(
            return_value=httpx.Response(
                200,
                json={
                    "clienteId": str(CLIENTE_ID),
                    "estado": "VERIFICADO",
                    "verificadoEn": "2026-10-01T00:00:00Z",
                },
            )
        )
        return entorno

    yield crear
    pila.close()


@pytest.fixture
def entorno(crear_entorno: Callable[..., Entorno]) -> Entorno:
    return crear_entorno()
