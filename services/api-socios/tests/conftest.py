import io
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date
from itertools import count

import pytest
from api_socios.adapters.inbound.dependencias import administrar_socios, autenticar_socio
from api_socios.application.socios import AdministrarSocios, AutenticarSocio
from api_socios.domain.cuotas import LimitesPlan, Periodo
from api_socios.domain.socios import Permiso, Plan, Socio, identificador
from api_socios.main import app
from fastapi.testclient import TestClient
from solventa_common.logging import FormateadorJson


class RepositorioMemoria:
    def __init__(self) -> None:
        self.socios: dict[str, Socio] = {}
        self._secuencia = count(1)
        self.falla_al_guardar = False

    def siguiente_identificador(self) -> str:
        return identificador(next(self._secuencia))

    def guardar(self, socio: Socio) -> None:
        if self.falla_al_guardar:
            raise RuntimeError("base no disponible")
        self.socios[socio.id] = socio

    def obtener(self, socio_id: str) -> Socio | None:
        return self.socios.get(socio_id)

    def por_client_id(self, client_id: str) -> Socio | None:
        return next((s for s in self.socios.values() if s.client_id == client_id), None)

    def listar(self) -> list[Socio]:
        return list(self.socios.values())


@dataclass
class IdentidadFalsa:
    clientes: dict[str, frozenset[Permiso]] = field(default_factory=dict)
    eliminados: list[str] = field(default_factory=list)
    falla: bool = False
    _n: count = field(default_factory=lambda: count(1))

    def crear_cliente(self, socio_id: str, permisos: frozenset[Permiso]) -> tuple[str, str]:
        if self.falla:
            raise RuntimeError("cognito no disponible")
        client_id = f"cliente{next(self._n)}"
        self.clientes[client_id] = permisos
        return client_id, f"secreto-de-{client_id}-NO-REGISTRAR"

    def eliminar_cliente(self, client_id: str) -> None:
        self.eliminados.append(client_id)
        self.clientes.pop(client_id, None)


LIMITES = {
    Plan.BASICO: LimitesPlan(Plan.BASICO, 5, 10, 1000, Periodo.DIA),
    Plan.ESTANDAR: LimitesPlan(Plan.ESTANDAR, 50, 100, 50000, Periodo.DIA),
    Plan.PRUEBA: LimitesPlan(Plan.PRUEBA, 1, 2, 5, Periodo.DIA),
}


@dataclass
class ApiKeysFalsas:
    """Cada API key está en un solo plan, como en API Gateway."""

    keys: dict[str, bool] = field(default_factory=dict)
    planes: dict[str, Plan] = field(default_factory=dict)
    eliminadas: list[str] = field(default_factory=list)
    disponibles: list[Plan] = field(default_factory=lambda: list(Plan))
    usado: dict[str, int] = field(default_factory=dict)
    consultas_uso: list[tuple[str, Plan, date, date]] = field(default_factory=list)
    falla_crear: bool = False
    falla_plan: bool | Plan = False
    falla_desasociar: bool = False
    _n: count = field(default_factory=lambda: count(1))

    def crear(self, socio_id: str, nombre: str) -> tuple[str, str]:
        if self.falla_crear:
            raise RuntimeError("apigateway no disponible")
        key_id = f"key{next(self._n)}"
        self.keys[key_id] = True
        return key_id, f"valor-de-{key_id}-NO-REGISTRAR"

    def planes_disponibles(self) -> list[Plan]:
        return self.disponibles

    def asociar_plan(self, api_key_id: str, plan: Plan) -> None:
        if self.falla_plan is True or self.falla_plan == plan:
            raise RuntimeError("plan inexistente")
        if api_key_id in self.planes:
            raise RuntimeError("la API key ya está en otro plan del stage")
        self.planes[api_key_id] = plan

    def desasociar_plan(self, api_key_id: str, plan: Plan) -> None:
        if self.falla_desasociar:
            raise RuntimeError("apigateway no disponible")
        if self.planes.get(api_key_id) is plan:
            del self.planes[api_key_id]

    def limites(self, plan: Plan) -> LimitesPlan:
        return LIMITES[plan]

    def uso(
        self, api_key_id: str, plan: Plan, desde: date, hasta: date
    ) -> dict[str, list[list[int]]]:
        self.consultas_uso.append((api_key_id, plan, desde, hasta))
        if api_key_id not in self.usado:
            return {}
        usado = self.usado[api_key_id]
        return {api_key_id: [[usado, LIMITES[plan].cuota - usado]]}

    def deshabilitar(self, api_key_id: str) -> None:
        self.keys[api_key_id] = False

    def eliminar(self, api_key_id: str) -> None:
        self.eliminadas.append(api_key_id)
        self.keys.pop(api_key_id, None)


@dataclass
class Entorno:
    repositorio: RepositorioMemoria
    identidad: IdentidadFalsa
    api_keys: ApiKeysFalsas
    caso: AdministrarSocios
    cliente: TestClient


def _entorno() -> Iterator[Entorno]:
    repositorio, identidad, api_keys = RepositorioMemoria(), IdentidadFalsa(), ApiKeysFalsas()
    caso = AdministrarSocios(repositorio, identidad, api_keys)
    app.dependency_overrides[administrar_socios] = lambda: caso
    app.dependency_overrides[autenticar_socio] = lambda: AutenticarSocio(repositorio)
    # Sin "with": no corre el lifespan (migraciones).
    yield Entorno(repositorio, identidad, api_keys, caso, TestClient(app))
    app.dependency_overrides.clear()


@pytest.fixture
def entorno() -> Iterator[Entorno]:
    yield from _entorno()


@pytest.fixture(scope="module")
def entorno_compartido() -> Iterator[Entorno]:
    """Hypothesis no reinicia fixtures de función entre ejemplos."""
    yield from _entorno()


@pytest.fixture
def capturar_logs() -> Iterator[Callable[[], io.StringIO]]:
    raiz = logging.getLogger()
    anteriores, nivel = list(raiz.handlers), raiz.level

    def capturar() -> io.StringIO:
        salida = io.StringIO()
        handler = logging.StreamHandler(salida)
        handler.setFormatter(FormateadorJson("api-socios", "pruebas"))
        raiz.handlers = [handler]
        raiz.setLevel(logging.DEBUG)
        return salida

    yield capturar
    raiz.handlers, raiz.level = anteriores, nivel


def encabezados_socio(socio: Socio, *permisos: str, key_id: str | None = None) -> dict[str, str]:
    alcances = " ".join(f"solventa/{p}" for p in permisos)
    return {
        "X-Partner-Client-Id": socio.client_id,
        "X-Partner-Key-Id": key_id or socio.api_key_id,
        "X-Partner-Scopes": alcances,
    }


@pytest.fixture
def encabezados() -> Callable[..., dict[str, str]]:
    return encabezados_socio
