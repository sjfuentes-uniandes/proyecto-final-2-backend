"""Schemathesis 4 contra la app con repositorios en memoria y adaptador-identidad simulado."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import schemathesis
from clientes.application.consentimientos.puertos import Otorgamiento, Revocacion
from clientes.config import Settings
from clientes.domain.consentimientos.modelos import (
    ClaveIdempotenciaEnUso,
    Consentimiento,
    ConsentimientoActivoExistente,
    EstadoConsentimiento,
    RegistroIdempotencia,
    esta_vigente,
)
from clientes.domain.identidad.modelos import Cliente, EstadoIdentidad
from clientes.main import crear_app
from fastapi.testclient import TestClient
from hypothesis import settings
from schemathesis.specs.openapi.checks import positive_data_acceptance

# Starlette solo lista en Allow los métodos de la primera ruta que coincide: en /consentimientos, POST y GET
# comparten ruta y el 405 nombra solo uno.
EXCLUIDOS = [positive_data_acceptance, schemathesis.checks.allow_header_conformance]
SUB_CONTRATO = "sub-contrato"
ACTOR = {"X-Actor-Id": SUB_CONTRATO, "X-Actor-Rol": "CLIENTE"}


class _ConsentimientosMemoria:
    """Lo mínimo de RepositorioConsentimientos para ejercitar las rutas: un `ACTIVO` por
    propósito, la clave de idempotencia por cliente y la revocación de uno vigente."""

    def __init__(self) -> None:
        self.consentimientos: dict[UUID, Consentimiento] = {}
        self.claves: dict[tuple[UUID, UUID], tuple[str, UUID]] = {}

    def _leer(self, c: Consentimiento) -> Consentimiento:
        return replace(c, leido_en=datetime.now(UTC))

    async def por_clave(self, cliente_id: UUID, clave: UUID) -> RegistroIdempotencia | None:
        registro = self.claves.get((cliente_id, clave))
        if registro is None:
            return None
        return RegistroIdempotencia(registro[0], self._leer(self.consentimientos[registro[1]]))

    async def activo(self, cliente_id: UUID, proposito: str) -> Consentimiento | None:
        for c in self.consentimientos.values():
            if (c.cliente_id, c.proposito, c.estado) == (
                cliente_id,
                proposito,
                EstadoConsentimiento.ACTIVO,
            ):
                return self._leer(c)
        return None

    async def crear(self, otorgamiento: Otorgamiento) -> Consentimiento:
        solicitud = otorgamiento.solicitud
        if await self.activo(otorgamiento.cliente_id, solicitud.proposito) is not None:
            raise ConsentimientoActivoExistente()
        if (otorgamiento.cliente_id, otorgamiento.clave) in self.claves:
            raise ClaveIdempotenciaEnUso()
        ahora = datetime.now(UTC)
        creado = Consentimiento(
            id=otorgamiento.id,
            cliente_id=otorgamiento.cliente_id,
            proposito=solicitud.proposito,
            alcances=solicitud.alcances,
            version_texto=solicitud.version_texto,
            idioma=solicitud.idioma,
            estado=EstadoConsentimiento.ACTIVO,
            otorgado_en=ahora,
            vigente_hasta=ahora + timedelta(days=30 * solicitud.vigencia_meses),
            revocado_en=None,
            leido_en=ahora,
        )
        self.consentimientos[creado.id] = creado
        self.claves[(otorgamiento.cliente_id, otorgamiento.clave)] = (
            otorgamiento.huella,
            creado.id,
        )
        return creado

    async def obtener(self, cliente_id: UUID, consentimiento_id: UUID) -> Consentimiento | None:
        c = self.consentimientos.get(consentimiento_id)
        return self._leer(c) if c is not None and c.cliente_id == cliente_id else None

    async def listar(self, cliente_id: UUID) -> list[Consentimiento]:
        return [self._leer(c) for c in self.consentimientos.values() if c.cliente_id == cliente_id]

    async def revocar(self, revocacion: Revocacion) -> Consentimiento | None:
        c = self.consentimientos.get(revocacion.consentimiento_id)
        if c is None or c.cliente_id != revocacion.cliente_id:
            return None
        ahora = datetime.now(UTC)
        if esta_vigente(c.estado, c.vigente_hasta, ahora):
            c = replace(c, estado=EstadoConsentimiento.REVOCADO, revocado_en=ahora)
            self.consentimientos[c.id] = c
        return self._leer(c)


class _RepositorioMemoria:
    def __init__(self) -> None:
        self.clientes: dict[UUID, Cliente] = {}

    def obtener(self, cliente_id: UUID) -> Cliente | None:
        return self.clientes.get(cliente_id)

    def por_sub(self, sub: str) -> Cliente | None:
        return next((c for c in self.clientes.values() if c.sub == sub), None)

    def por_registro(self, registro_id: UUID) -> Cliente | None:
        return next((c for c in self.clientes.values() if c.registro_id == registro_id), None)

    def asegurar(self, sub: str) -> Cliente:
        existente = self.por_sub(sub)
        if existente is None:
            existente = Cliente(id=uuid4(), sub=sub)
            self.clientes[existente.id] = existente
        return existente

    def asegurar_registro(self, registro_id: UUID, correo: str) -> Cliente:
        existente = self.por_registro(registro_id)
        if existente is None:
            existente = Cliente(id=uuid4(), registro_id=registro_id, correo=correo)
            self.clientes[existente.id] = existente
        return existente

    def vincular(self, cliente_id: UUID, sub: str) -> Cliente:
        cliente = replace(self.clientes[cliente_id], sub=sub)
        self.clientes[cliente_id] = cliente
        return cliente

    def guardar(self, cliente: Cliente) -> None:
        self.clientes[cliente.id] = cliente


def _nominal(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"resultado": "APROBADO", "motivo": None})


consentimientos = _ConsentimientosMemoria()
identidad = _RepositorioMemoria()
identidad.guardar(
    Cliente(
        id=uuid4(),
        sub=SUB_CONTRATO,
        estado=EstadoIdentidad.VERIFICADO,
        verificado_en=datetime(2026, 10, 1, tzinfo=UTC),
    )
)
app = crear_app(
    Settings(),
    transport=httpx.MockTransport(_nominal),
    repositorio=identidad,
    repositorio_consentimientos=consentimientos,
)
# Arranca el lifespan una vez: crea el cliente HTTP y los casos de uso en app.state.
_ciclo = TestClient(app)
_ciclo.__enter__()

schema = schemathesis.openapi.from_asgi("/openapi.json", app)


# positive_data_acceptance se excluye: los 401/403 por actor ausente o con otro rol son reglas
# de encabezados que el esquema no puede expresar; test_api.py los cubre.
@schema.parametrize()
@settings(max_examples=40, deadline=None)
def test_api_cumple_su_contrato(case: schemathesis.Case) -> None:
    case.call_and_validate(excluded_checks=EXCLUIDOS)


# Con un titular verificado, el barrido pasa del 401 y llega a la lógica de las rutas: las lecturas
# (200 y 404) y la validación de otorgar (422). Un 201 exige la versión de texto vigente, que el
# esquema no puede expresar; lo cubre el último caso y los de módulo.
@schema.include(path_regex="consentimientos").parametrize()
@settings(max_examples=60, deadline=None)
def test_consentimientos_cumplen_su_contrato_con_un_titular_verificado(
    case: schemathesis.Case,
) -> None:
    case.call_and_validate(headers=ACTOR, excluded_checks=EXCLUIDOS)


def test_el_barrido_con_titular_llega_a_otorgar() -> None:
    """Un otorgamiento válido responde según el contrato publicado (201, o 409 si el barrido
    ya otorgó uno)."""
    cliente = TestClient(app)
    cuerpo = {
        "proposito": "PERFILAMIENTO_PRECIO",
        "alcances": ["FA_PRODUCTOS_VIGENTES"],
        "vigenciaMeses": 12,
        "versionTexto": app.state.consultar_texto_vigente.ejecutar(
            "PERFILAMIENTO_PRECIO", "es-CO"
        ).version,
        "idioma": "es-CO",
    }
    respuesta = cliente.post(
        "/v1/clientes/actual/consentimientos",
        json=cuerpo,
        headers={**ACTOR, "Idempotency-Key": str(uuid4())},
    )
    assert respuesta.status_code in (201, 409)


def _llamar(metodo: str, ruta: str, **kwargs: object) -> Any:
    """Llama la operación publicada y valida la respuesta contra su esquema."""
    caso = schema[ruta][metodo].Case(headers=ACTOR, **kwargs)
    respuesta = caso.call()
    caso.validate_response(respuesta, excluded_checks=EXCLUIDOS)
    return respuesta


def test_revocar_e_historial_de_uno_propio_cumplen_el_contrato() -> None:
    """El barrido solo ve 404 con identificadores aleatorios: aquí se revoca uno real (HU-M09)
    y se validan las respuestas 200 de revocar (primera vez y repetición) e historial."""
    titular = identidad.por_sub(SUB_CONTRATO)
    assert titular is not None
    ahora = datetime.now(UTC)
    propio = Consentimiento(
        id=uuid4(),
        cliente_id=titular.id,
        proposito="PERFILAMIENTO_PRECIO",
        alcances=("FA_PRODUCTOS_VIGENTES",),
        version_texto="v-contrato",
        idioma="es-CO",
        estado=EstadoConsentimiento.ACTIVO,
        otorgado_en=ahora - timedelta(days=1),
        vigente_hasta=ahora + timedelta(days=180),
        revocado_en=None,
        leido_en=ahora,
    )
    consentimientos.consentimientos[propio.id] = propio
    ruta = "/v1/clientes/actual/consentimientos/{consentimientoId}"
    parametros = {"consentimientoId": str(propio.id)}

    primera = _llamar("POST", f"{ruta}/revocacion", path_parameters=parametros)
    repeticion = _llamar("POST", f"{ruta}/revocacion", path_parameters=parametros)
    historial = _llamar("GET", f"{ruta}/historial", path_parameters=parametros)

    assert (primera.status_code, repeticion.status_code, historial.status_code) == (200,) * 3
    assert primera.json()["estado"] == "REVOCADO"
    assert repeticion.json() == primera.json()
    assert [h["tipo"] for h in historial.json()["hitos"]] == ["OTORGADO", "REVOCADO"]
