"""Verificación del consentimiento por la API, el caso de uso y el cableado, con repositorios en
memoria (HU-W31 AC1–AC4; ADR 0011)."""

import json
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from clientes.adapters.inbound.http.verificaciones_consentimiento import Alcance, Proposito
from clientes.adapters.outbound import postgres
from clientes.adapters.outbound.consentimientos_pg import ConsentimientosPg
from clientes.application.consentimientos.casos import VerificarConsentimiento
from clientes.config import Settings
from clientes.domain.consentimientos.modelos import (
    ALCANCES,
    PROPOSITOS,
    Consentimiento,
    EstadoConsentimiento,
)
from clientes.domain.consentimientos.verificacion import Decision
from clientes.domain.identidad.modelos import Cliente, ClienteNoEncontrado
from fastapi.testclient import TestClient

RelojFalso = RepositorioMemoria = RepositorioConsentimientosMemoria = Any
RepositorioVerificacionesMemoria = Any

pytestmark = pytest.mark.anyio

CORRELACION = "corr-api-w31"
CUATRO = [
    "FA_PRODUCTOS_VIGENTES",
    "FA_HISTORIAL_PAGOS_12M",
    "FA_INGRESOS_AGREGADOS",
    "DA_REGISTROS_PUBLICOS",
]
CUERPO = {"proposito": "PERFILAMIENTO_PRECIO", "alcances": CUATRO}


def _ruta(cliente_id: UUID) -> str:
    return f"/v1/clientes/{cliente_id}/consentimientos/verificaciones"


@pytest.fixture
def api(
    repositorio: RepositorioMemoria,
    consentimientos: RepositorioConsentimientosMemoria,
    verificaciones: RepositorioVerificacionesMemoria,
) -> Iterator[TestClient]:
    async def sin_llamadas(request: httpx.Request) -> httpx.Response:
        raise AssertionError("verificar no invoca a adaptador-identidad")

    app = crear_app_con(repositorio, consentimientos, verificaciones, sin_llamadas)
    with TestClient(app, headers={"X-Correlation-Id": CORRELACION}) as cliente:
        yield cliente


def crear_app_con(
    repositorio: Any, consentimientos: Any, verificaciones: Any, transporte: Any
) -> Any:
    from clientes.main import crear_app

    return crear_app(
        Settings(),
        transport=httpx.MockTransport(transporte),
        repositorio=repositorio,
        repositorio_consentimientos=consentimientos,
        repositorio_verificaciones=verificaciones,
    )


@pytest.fixture
def cliente(repositorio: RepositorioMemoria) -> UUID:
    nuevo = Cliente(id=uuid4(), sub="sub-titular")
    repositorio.guardar(nuevo)
    return nuevo.id


@pytest.fixture
def sembrar(
    consentimientos: RepositorioConsentimientosMemoria, reloj: RelojFalso
) -> Callable[..., Consentimiento]:
    def crear(cliente_id: UUID, **cambios: Any) -> Consentimiento:
        datos: dict[str, Any] = {
            "id": uuid4(),
            "cliente_id": cliente_id,
            "proposito": "PERFILAMIENTO_PRECIO",
            "alcances": tuple(CUATRO),
            "version_texto": "v1",
            "idioma": "es-CO",
            "estado": EstadoConsentimiento.ACTIVO,
            "otorgado_en": reloj.ahora - timedelta(days=30),
            "vigente_hasta": reloj.ahora + timedelta(days=150),
            "revocado_en": None,
            "leido_en": reloj.ahora,
        } | cambios
        consentimiento = Consentimiento(**datos)
        consentimientos.consentimientos[consentimiento.id] = consentimiento
        return consentimiento

    return crear


def _verificar(api: TestClient, cliente_id: UUID, **cuerpo: Any) -> httpx.Response:
    return api.post(_ruta(cliente_id), json=CUERPO | cuerpo)


def test_ac1_vigente_permite_y_registra_la_decision(
    api: TestClient,
    cliente: UUID,
    sembrar: Callable[..., Consentimiento],
    verificaciones: RepositorioVerificacionesMemoria,
) -> None:
    vigente = sembrar(cliente)

    respuesta = _verificar(api, cliente)

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json() == {
        "decision": "PERMITIDO",
        "consentimientoId": str(vigente.id),
        "vigenteHasta": "2027-03-16T12:00:00Z",
        "alcancesPermitidos": CUATRO,
        "alcancesRechazados": [],
        "causa": None,
    }
    ((registrada, correlacion),) = verificaciones.eventos
    assert (registrada.decision, correlacion) == (Decision.PERMITIDO, CORRELACION)


def test_ac3_alcance_no_incluido_rechaza_solo_ese_sin_valores_financieros(
    api: TestClient, cliente: UUID, sembrar: Callable[..., Consentimiento]
) -> None:
    sembrar(cliente, alcances=("FA_PRODUCTOS_VIGENTES", "DA_REGISTROS_PUBLICOS"))

    cuerpo = _verificar(api, cliente).json()

    assert cuerpo["decision"] == "PERMITIDO_PARCIAL"
    assert cuerpo["alcancesPermitidos"] == ["FA_PRODUCTOS_VIGENTES", "DA_REGISTROS_PUBLICOS"]
    assert cuerpo["alcancesRechazados"] == [
        {"alcance": "FA_HISTORIAL_PAGOS_12M", "causa": "ALCANCE_NO_AUTORIZADO"},
        {"alcance": "FA_INGRESOS_AGREGADOS", "causa": "ALCANCE_NO_AUTORIZADO"},
    ]
    assert cuerpo["causa"] is None
    assert set(cuerpo) == {
        "decision",
        "consentimientoId",
        "vigenteHasta",
        "alcancesPermitidos",
        "alcancesRechazados",
        "causa",
    }


@pytest.mark.parametrize(
    ("cambios", "causa"),
    [
        ({"estado": EstadoConsentimiento.REVOCADO}, "REVOCADO"),
        ({"estado": EstadoConsentimiento.VENCIDO}, "VENCIDO"),
    ],
)
def test_ac2_revocado_o_vencido_rechaza_con_causa_estable_y_sin_id(
    api: TestClient,
    cliente: UUID,
    sembrar: Callable[..., Consentimiento],
    reloj: RelojFalso,
    verificaciones: RepositorioVerificacionesMemoria,
    cambios: dict[str, Any],
    causa: str,
) -> None:
    if cambios["estado"] is EstadoConsentimiento.REVOCADO:
        cambios = cambios | {"revocado_en": reloj.ahora - timedelta(hours=1)}
    evaluado = sembrar(cliente, **cambios)

    cuerpo = _verificar(api, cliente).json()

    assert cuerpo == {
        "decision": "RECHAZADO",
        "consentimientoId": None,
        "vigenteHasta": None,
        "alcancesPermitidos": [],
        "alcancesRechazados": [{"alcance": a, "causa": "ALCANCE_NO_AUTORIZADO"} for a in CUATRO],
        "causa": causa,
    }
    ((registrada, _),) = verificaciones.eventos
    assert registrada.consentimiento_evaluado == evaluado.id  # el registro sí lo guarda


@pytest.mark.parametrize(
    ("desfase", "decision"),
    [(-timedelta(microseconds=1), "PERMITIDO"), (timedelta(0), "RECHAZADO")],
    ids=["un-instante-antes", "en-el-instante"],
)
def test_ac4_limite_exacto_por_la_api(
    api: TestClient,
    cliente: UUID,
    sembrar: Callable[..., Consentimiento],
    reloj: RelojFalso,
    desfase: timedelta,
    decision: str,
) -> None:
    hasta = reloj.ahora + timedelta(days=1)
    sembrar(cliente, vigente_hasta=hasta)
    reloj.ahora = hasta + desfase

    cuerpo = _verificar(api, cliente).json()

    assert cuerpo["decision"] == decision
    assert cuerpo["causa"] == (None if decision == "PERMITIDO" else "VENCIDO")


def test_sin_consentimiento_y_otro_proposito_se_distinguen(
    api: TestClient,
    repositorio: RepositorioMemoria,
    cliente: UUID,
    sembrar: Callable[..., Consentimiento],
) -> None:
    con_otro = Cliente(id=uuid4(), sub="sub-otro")
    repositorio.guardar(con_otro)
    sembrar(con_otro.id, proposito="PROPOSITO_SINTETICO")

    assert _verificar(api, cliente).json()["causa"] == "SIN_CONSENTIMIENTO"
    assert _verificar(api, con_otro.id).json()["causa"] == "PROPOSITO_NO_AUTORIZADO"


def test_el_consentimiento_de_otro_cliente_no_autoriza(
    api: TestClient,
    repositorio: RepositorioMemoria,
    cliente: UUID,
    sembrar: Callable[..., Consentimiento],
) -> None:
    otro = Cliente(id=uuid4(), sub="sub-otro")
    repositorio.guardar(otro)
    sembrar(otro.id)

    assert _verificar(api, cliente).json()["causa"] == "SIN_CONSENTIMIENTO"


def test_cliente_inexistente_responde_404_sin_registrar(
    api: TestClient, verificaciones: RepositorioVerificacionesMemoria
) -> None:
    respuesta = _verificar(api, uuid4())

    assert respuesta.status_code == 404
    assert respuesta.json() == {
        "codigo": "CLIENTE_NO_ENCONTRADO",
        "mensaje": "El cliente no existe",
        "correlationId": CORRELACION,
        "detalles": {},
    }
    assert verificaciones.eventos == []


def test_sin_cache_una_revocacion_se_ve_en_la_siguiente_verificacion(
    api: TestClient,
    cliente: UUID,
    sembrar: Callable[..., Consentimiento],
    consentimientos: RepositorioConsentimientosMemoria,
    reloj: RelojFalso,
    verificaciones: RepositorioVerificacionesMemoria,
) -> None:
    vigente = sembrar(cliente)
    assert _verificar(api, cliente).json()["decision"] == "PERMITIDO"
    assert _verificar(api, cliente).json()["decision"] == "PERMITIDO"

    consentimientos.consentimientos[vigente.id] = replace(
        vigente, estado=EstadoConsentimiento.REVOCADO, revocado_en=reloj.ahora
    )

    assert _verificar(api, cliente).json()["causa"] == "REVOCADO"
    assert [v.decision for v, _ in verificaciones.eventos] == [
        Decision.PERMITIDO,
        Decision.PERMITIDO,
        Decision.RECHAZADO,
    ]


@pytest.mark.parametrize(
    "cuerpo",
    [
        {"alcances": []},
        {"alcances": ["FA_DESCONOCIDO"]},
        {"alcances": None},
        {"proposito": "OTRO"},
        {"proposito": None},
        {"proposito": "perfilamiento_precio"},
    ],
)
def test_solicitud_invalida_responde_422_sin_registrar(
    api: TestClient,
    cliente: UUID,
    verificaciones: RepositorioVerificacionesMemoria,
    cuerpo: dict[str, Any],
) -> None:
    respuesta = _verificar(api, cliente, **cuerpo)

    assert respuesta.status_code == 422
    assert respuesta.json()["codigo"] == "SOLICITUD_INVALIDA"
    assert verificaciones.eventos == []


def test_un_cliente_id_que_no_es_uuid_responde_422(api: TestClient) -> None:
    respuesta = api.post("/v1/clientes/actual/consentimientos/verificaciones", json=CUERPO)

    assert respuesta.status_code == 422
    assert respuesta.json()["detalles"]["campos"] == ["clienteId"]


def test_los_alcances_repetidos_se_responden_una_vez(
    api: TestClient, cliente: UUID, sembrar: Callable[..., Consentimiento]
) -> None:
    sembrar(cliente)

    cuerpo = _verificar(api, cliente, alcances=["DA_REGISTROS_PUBLICOS"] * 3).json()

    assert cuerpo["alcancesPermitidos"] == ["DA_REGISTROS_PUBLICOS"]


def test_no_exige_encabezados_de_actor(
    api: TestClient, cliente: UUID, sembrar: Callable[..., Consentimiento]
) -> None:
    """Servicio a servicio, como GET .../identidad: `cotizacion` no envía X-Actor-*."""
    sembrar(cliente)

    respuesta = api.post(_ruta(cliente), json=CUERPO, headers={"X-Actor-Rol": "OTRO"})

    assert respuesta.status_code == 200


def test_un_fallo_tecnico_no_responde_una_decision(
    api: TestClient, cliente: UUID, verificaciones: RepositorioVerificacionesMemoria
) -> None:
    """Sin base o sin outbox no hay decisión: 500, y `cotizacion` falla cerrado."""
    verificaciones.falla = RuntimeError("base caída")
    sin_relanzar = TestClient(api.app, raise_server_exceptions=False)

    respuesta = sin_relanzar.post(_ruta(cliente), json=CUERPO)

    assert respuesta.status_code == 500
    assert respuesta.json()["codigo"] == "ERROR_INTERNO"
    assert "decision" not in respuesta.json()


def test_los_logs_no_llevan_el_cliente_ni_los_alcances(
    api: TestClient,
    cliente: UUID,
    sembrar: Callable[..., Consentimiento],
    capturar_logs: Callable[..., Any],
) -> None:
    sembrar(cliente)
    salida = capturar_logs()

    _verificar(api, cliente)

    texto = salida.getvalue()
    assert texto  # la operación sí se registra
    assert str(cliente) not in texto
    for linea in texto.splitlines():
        registro = json.loads(linea)
        assert not any(a in json.dumps(registro) for a in CUATRO)


def test_los_enums_del_dto_son_los_del_dominio() -> None:
    assert {a.value for a in Alcance} == ALCANCES
    assert {p.value for p in Proposito} == PROPOSITOS


async def test_el_caso_de_uso_traduce_un_cliente_inexistente() -> None:
    class SinCliente:
        async def verificar(self, *_: Any) -> None:
            return None

    with pytest.raises(ClienteNoEncontrado):
        await VerificarConsentimiento(SinCliente()).ejecutar(
            uuid4(), "PERFILAMIENTO_PRECIO", CUATRO, correlation_id="c"
        )


def test_sin_repositorios_inyectados_la_verificacion_usa_el_pool_de_consentimientos(
    monkeypatch: pytest.MonkeyPatch, pools: list[Any]
) -> None:
    from clientes.main import crear_app

    monkeypatch.setattr(postgres, "migrar", lambda _: None)
    app = crear_app(Settings(), transport=httpx.MockTransport(lambda _: httpx.Response(200)))

    with TestClient(app):
        (pool,) = pools
        repositorio = app.state.verificar_consentimiento._repositorio
        assert isinstance(repositorio, ConsentimientosPg)
        assert repositorio is app.state.otorgar_consentimiento._repositorio
        assert repositorio._pool is pool


def test_un_repositorio_de_verificaciones_inyectado_se_respeta_con_postgres(
    monkeypatch: pytest.MonkeyPatch,
    pools: list[Any],
    verificaciones: RepositorioVerificacionesMemoria,
) -> None:
    from clientes.main import crear_app

    monkeypatch.setattr(postgres, "migrar", lambda _: None)
    app = crear_app(
        Settings(),
        transport=httpx.MockTransport(lambda _: httpx.Response(200)),
        repositorio_verificaciones=verificaciones,
    )

    with TestClient(app):
        assert app.state.verificar_consentimiento._repositorio is verificaciones
        assert isinstance(app.state.otorgar_consentimiento._repositorio, ConsentimientosPg)


def test_sin_repositorio_de_verificaciones_no_cablea_la_verificacion(
    repositorio: RepositorioMemoria, consentimientos: RepositorioConsentimientosMemoria
) -> None:
    from clientes.main import crear_app

    app = crear_app(
        Settings(),
        transport=httpx.MockTransport(lambda _: httpx.Response(200)),
        repositorio=repositorio,
        repositorio_consentimientos=consentimientos,
    )

    with TestClient(app):
        assert not hasattr(app.state, "verificar_consentimiento")
