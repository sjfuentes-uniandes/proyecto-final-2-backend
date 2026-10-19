"""Lectura web de los consentimientos por `clienteId` (HU-W31, PR 3.3) con repositorios en memoria.

Solo lectura y solo para el asesor del back-office (ADR 0002): mismo patrón de actor que el alta
de HU-W30 (`X-Actor-Rol=ASESOR`). Un titular no lee por `clienteId` (ADR 0006).
"""

from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
import pytest
from clientes.adapters.outbound.textos_json import TextosJson
from clientes.config import Settings
from clientes.domain.identidad.modelos import EstadoIdentidad
from clientes.main import crear_app
from fastapi.testclient import TestClient

RepositorioMemoria = RepositorioConsentimientosMemoria = Any

SUB = "sub-titular"
OTRO_SUB = "sub-otro-titular"
CORRELACION = "corr-api-w31-lectura"
RUTA_TITULAR = "/v1/clientes/actual/consentimientos"
ASESOR = {"X-Actor-Id": "asesor-sintetico", "X-Actor-Rol": "ASESOR"}
TEXTO = TextosJson.cargar().vigente("PERFILAMIENTO_PRECIO", "es-CO")
CUERPO = {
    "proposito": "PERFILAMIENTO_PRECIO",
    "alcances": ["FA_PRODUCTOS_VIGENTES", "DA_REGISTROS_PUBLICOS"],
    "vigenciaMeses": 12,
    "versionTexto": TEXTO.version,
    "idioma": "es-CO",
}


def ruta(cliente_id: Any) -> str:
    return f"/v1/clientes/{cliente_id}/consentimientos"


def titular_actor(sub: str = SUB) -> dict[str, str]:
    return {"X-Actor-Id": sub, "X-Actor-Rol": "CLIENTE"}


@pytest.fixture
def api(
    repositorio: RepositorioMemoria, consentimientos: RepositorioConsentimientosMemoria
) -> Iterator[TestClient]:
    async def sin_llamadas(request: httpx.Request) -> httpx.Response:
        raise AssertionError("la lectura no invoca a adaptador-identidad")

    app = crear_app(
        Settings(),
        transport=httpx.MockTransport(sin_llamadas),
        repositorio=repositorio,
        repositorio_consentimientos=consentimientos,
    )
    with TestClient(app, headers={"X-Correlation-Id": CORRELACION}) as cliente:
        yield cliente


@pytest.fixture
def titular(repositorio: RepositorioMemoria) -> Callable[..., Any]:
    def crear(sub: str = SUB) -> Any:
        cliente = replace(
            repositorio.asegurar(sub),
            estado=EstadoIdentidad.VERIFICADO,
            verificado_en=datetime(2026, 10, 1, tzinfo=UTC),
        )
        repositorio.guardar(cliente)
        return cliente

    return crear


def _otorgar(api: TestClient, sub: str = SUB, **cambios: Any) -> dict[str, Any]:
    respuesta = api.post(
        RUTA_TITULAR,
        json={**CUERPO, **cambios},
        headers={**titular_actor(sub), "Idempotency-Key": str(uuid4())},
    )
    assert respuesta.status_code == 201, respuesta.text
    return dict(respuesta.json())


def _revocar(api: TestClient, consentimiento_id: str, sub: str = SUB) -> dict[str, Any]:
    respuesta = api.post(
        f"{RUTA_TITULAR}/{consentimiento_id}/revocacion", headers=titular_actor(sub)
    )
    assert respuesta.status_code == 200, respuesta.text
    return dict(respuesta.json())


def _error(respuesta: httpx.Response, estado: int, codigo: str) -> dict[str, Any]:
    assert respuesta.status_code == estado, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["codigo"] == codigo
    assert cuerpo["correlationId"] == CORRELACION
    return dict(cuerpo)


def test_el_asesor_lista_los_consentimientos_del_cliente_con_estado_derivado(
    api: TestClient, titular: Any, reloj: Any
) -> None:
    """Vencido, revocado y activo, del más reciente al más antiguo, con el mismo
    `ConsentimientoDto` que ve el titular."""
    cliente = titular()
    vencido = _otorgar(api, vigenciaMeses=6)
    reloj.avanzar(timedelta(days=200))  # el almacenado sigue ACTIVO: VENCIDO se deriva al leer
    revocado = _otorgar(api)
    _revocar(api, revocado["consentimientoId"])
    reloj.avanzar(timedelta(seconds=1))
    activo = _otorgar(api)

    respuesta = api.get(ruta(cliente.id), headers=ASESOR)

    assert respuesta.status_code == 200, respuesta.text
    lista = respuesta.json()
    assert [(c["consentimientoId"], c["estado"]) for c in lista] == [
        (activo["consentimientoId"], "ACTIVO"),
        (revocado["consentimientoId"], "REVOCADO"),
        (vencido["consentimientoId"], "VENCIDO"),
    ]
    assert lista == api.get(RUTA_TITULAR, headers=titular_actor()).json()


def test_el_asesor_no_ve_los_consentimientos_de_otro_cliente(api: TestClient, titular: Any) -> None:
    cliente = titular()
    titular(OTRO_SUB)
    _otorgar(api, sub=OTRO_SUB)
    respuesta = api.get(ruta(cliente.id), headers=ASESOR)
    assert respuesta.status_code == 200
    assert respuesta.json() == []


def test_404_listar_de_un_cliente_inexistente(api: TestClient) -> None:
    _error(api.get(ruta(uuid4()), headers=ASESOR), 404, "CLIENTE_NO_ENCONTRADO")


def test_el_asesor_ve_el_historial_de_un_revocado_y_de_un_vencido(
    api: TestClient, titular: Any, reloj: Any
) -> None:
    cliente = titular()
    vence = _otorgar(api, vigenciaMeses=6)
    reloj.avanzar(timedelta(days=200))
    otorgado = _otorgar(api)
    reloj.avanzar(timedelta(days=1))
    revocado = _revocar(api, otorgado["consentimientoId"])

    historial_revocado = api.get(
        f"{ruta(cliente.id)}/{otorgado['consentimientoId']}/historial", headers=ASESOR
    )
    historial_vencido = api.get(
        f"{ruta(cliente.id)}/{vence['consentimientoId']}/historial", headers=ASESOR
    )

    assert historial_revocado.status_code == 200, historial_revocado.text
    assert historial_revocado.json() == {
        "consentimientoId": otorgado["consentimientoId"],
        "estado": "REVOCADO",
        "hitos": [
            {"tipo": "OTORGADO", "ocurridoEn": otorgado["otorgadoEn"]},
            {"tipo": "REVOCADO", "ocurridoEn": revocado["revocadoEn"]},
        ],
    }
    assert historial_vencido.json() == {
        "consentimientoId": vence["consentimientoId"],
        "estado": "VENCIDO",
        "hitos": [
            {"tipo": "OTORGADO", "ocurridoEn": vence["otorgadoEn"]},
            {"tipo": "VENCIDO", "ocurridoEn": vence["vigenteHasta"]},
        ],
    }
    titular_ve = api.get(
        f"{RUTA_TITULAR}/{otorgado['consentimientoId']}/historial", headers=titular_actor()
    )
    assert historial_revocado.json() == titular_ve.json()


def test_404_historial_de_otro_cliente_inexistente_o_sin_cliente(
    api: TestClient, titular: Any
) -> None:
    """Indistinguibles entre sí: mismo código, mensaje y detalles."""
    cliente = titular()
    titular(OTRO_SUB)
    ajeno = _otorgar(api, sub=OTRO_SUB)
    respuestas = [
        api.get(f"{ruta(cliente.id)}/{ajeno['consentimientoId']}/historial", headers=ASESOR),
        api.get(f"{ruta(cliente.id)}/{uuid4()}/historial", headers=ASESOR),
        api.get(f"{ruta(uuid4())}/{ajeno['consentimientoId']}/historial", headers=ASESOR),
    ]
    cuerpos = [_error(r, 404, "CONSENTIMIENTO_NO_ENCONTRADO") for r in respuestas]
    assert len({(c["mensaje"], str(c["detalles"])) for c in cuerpos}) == 1


@pytest.mark.parametrize("sufijo", ["", f"/{uuid4()}/historial"], ids=["lista", "historial"])
def test_la_lectura_web_exige_al_actor_asesor(api: TestClient, titular: Any, sufijo: str) -> None:
    """Un titular no lee por `clienteId`, ni siquiera el propio (ADR 0006): sería la puerta para
    leer los de otro cliente."""
    cliente = titular()
    destino = f"{ruta(cliente.id)}{sufijo}"
    _error(api.get(destino), 401, "ACTOR_REQUERIDO")
    _error(api.get(destino, headers={"X-Actor-Rol": "ASESOR"}), 401, "ACTOR_REQUERIDO")
    _error(api.get(destino, headers=titular_actor()), 403, "ROL_NO_AUTORIZADO")
    _error(
        api.get(destino, headers={"X-Actor-Id": "x", "X-Actor-Rol": "AUDITOR"}),
        403,
        "ROL_NO_AUTORIZADO",
    )


def test_el_actor_se_valida_antes_que_los_identificadores(api: TestClient) -> None:
    _error(api.get(ruta("no-es-uuid")), 401, "ACTOR_REQUERIDO")
    _error(api.get(ruta("no-es-uuid"), headers=ASESOR), 422, "SOLICITUD_INVALIDA")
    _error(
        api.get(f"{ruta(uuid4())}/no-es-uuid/historial", headers=ASESOR),
        422,
        "SOLICITUD_INVALIDA",
    )


def test_las_rutas_del_titular_no_las_captura_la_lectura_web(api: TestClient, titular: Any) -> None:
    """`actual` no es un `clienteId`: el asesor recibe el 403 de las rutas del titular, no un 422
    de la lectura web, y el titular sigue leyendo las suyas."""
    titular()
    creado = _otorgar(api)
    _error(api.get(RUTA_TITULAR, headers=ASESOR), 403, "ROL_NO_AUTORIZADO")
    _error(
        api.get(f"{RUTA_TITULAR}/{creado['consentimientoId']}/historial", headers=ASESOR),
        403,
        "ROL_NO_AUTORIZADO",
    )
    assert api.get(RUTA_TITULAR, headers=titular_actor()).json() == [creado]


def test_la_lectura_web_no_escribe_ni_admite_escrituras(
    api: TestClient, titular: Any, consentimientos: Any
) -> None:
    """ADR 0002: en web no existen otorgar ni revocar; la lectura no deja eventos."""
    cliente = titular()
    creado = _otorgar(api)
    eventos = list(consentimientos.eventos)
    base = ruta(cliente.id)
    api.get(base, headers=ASESOR)
    api.get(f"{base}/{creado['consentimientoId']}/historial", headers=ASESOR)
    assert consentimientos.eventos == eventos
    assert api.post(base, json=CUERPO, headers=ASESOR).status_code == 405
    assert (
        api.post(f"{base}/{creado['consentimientoId']}/revocacion", headers=ASESOR).status_code
        == 404
    )
    assert api.get(RUTA_TITULAR, headers=titular_actor()).json()[0]["estado"] == "ACTIVO"
