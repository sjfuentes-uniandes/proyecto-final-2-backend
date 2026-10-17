"""API de consentimientos con repositorios en memoria (HU-M08: AC1, AC3, AC4 y aislamiento)."""

from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from clientes.adapters.outbound.textos_json import TextosJson
from clientes.config import Settings
from clientes.domain.consentimientos.modelos import EstadoConsentimiento
from clientes.domain.identidad.modelos import EstadoIdentidad
from clientes.main import crear_app
from fastapi.testclient import TestClient

RelojFalso = RepositorioMemoria = RepositorioConsentimientosMemoria = Any

SUB = "sub-titular"
OTRO_SUB = "sub-otro-titular"
CORRELACION = "corr-api-m08"
RUTA = "/v1/clientes/actual/consentimientos"
TEXTO = TextosJson.cargar().vigente("PERFILAMIENTO_PRECIO", "es-CO")
CUERPO = {
    "proposito": "PERFILAMIENTO_PRECIO",
    "alcances": ["FA_PRODUCTOS_VIGENTES", "DA_REGISTROS_PUBLICOS"],
    "vigenciaMeses": 12,
    "versionTexto": TEXTO.version,
    "idioma": "es-CO",
}


def actor(sub: str = SUB) -> dict[str, str]:
    return {"X-Actor-Id": sub, "X-Actor-Rol": "CLIENTE"}


def con_clave(clave: UUID | None = None, sub: str = SUB) -> dict[str, str]:
    return {**actor(sub), "Idempotency-Key": str(clave or uuid4())}


@pytest.fixture
def api(
    repositorio: RepositorioMemoria, consentimientos: RepositorioConsentimientosMemoria
) -> Iterator[TestClient]:
    async def sin_llamadas(request: httpx.Request) -> httpx.Response:
        raise AssertionError("otorgar no invoca a adaptador-identidad")

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
    def crear(sub: str = SUB, estado: EstadoIdentidad = EstadoIdentidad.VERIFICADO) -> Any:
        cliente = replace(
            repositorio.asegurar(sub),
            estado=estado,
            verificado_en=datetime(2026, 10, 1, tzinfo=UTC)
            if estado.name == "VERIFICADO"
            else None,
        )
        repositorio.guardar(cliente)
        return cliente

    return crear


def _error(respuesta: httpx.Response, estado: int, codigo: str) -> dict[str, Any]:
    assert respuesta.status_code == estado, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["codigo"] == codigo
    assert cuerpo["correlationId"] == CORRELACION
    assert set(cuerpo) == {"codigo", "mensaje", "correlationId", "detalles"}
    return cuerpo


def test_201_otorga_y_devuelve_el_comprobante(
    api: TestClient, titular: Any, consentimientos: Any
) -> None:
    """AC1: queda ACTIVO con versión del texto, fecha, propósito, alcances y vencimiento."""
    cliente = titular()
    respuesta = api.post(RUTA, json=CUERPO, headers=con_clave())
    assert respuesta.status_code == 201, respuesta.text
    assert "Idempotent-Replayed" not in respuesta.headers
    cuerpo = respuesta.json()
    consentimiento_id = UUID(cuerpo.pop("consentimientoId"))
    assert cuerpo == {
        "estado": "ACTIVO",
        "proposito": "PERFILAMIENTO_PRECIO",
        "alcances": ["DA_REGISTROS_PUBLICOS", "FA_PRODUCTOS_VIGENTES"],
        "versionTexto": TEXTO.version,
        "idioma": "es-CO",
        "otorgadoEn": "2026-10-17T12:00:00Z",
        "vigenteHasta": "2027-10-17T12:00:00Z",
        "revocadoEn": None,
    }
    guardado = consentimientos.consentimientos[consentimiento_id]
    assert guardado.cliente_id == cliente.id
    (evento,) = consentimientos.eventos
    assert evento["agregado"] == consentimiento_id
    assert evento["actor"] == SUB


def test_vigencia_de_seis_meses(api: TestClient, titular: Any) -> None:
    titular()
    respuesta = api.post(RUTA, json={**CUERPO, "vigenciaMeses": 6}, headers=con_clave())
    assert respuesta.status_code == 201
    assert respuesta.json()["vigenteHasta"] == "2027-04-17T12:00:00Z"


def test_200_repeticion_devuelve_el_mismo_consentimiento(
    api: TestClient, titular: Any, consentimientos: Any
) -> None:
    """AC4: reenviar la misma confirmación no duplica ni choca con el 409."""
    titular()
    clave = uuid4()
    primero = api.post(RUTA, json=CUERPO, headers=con_clave(clave))
    segundo = api.post(RUTA, json=CUERPO, headers=con_clave(clave))

    assert segundo.headers["Idempotent-Replayed"] == "true"
    assert segundo.json() == primero.json()
    assert len(consentimientos.consentimientos) == 1
    assert len(consentimientos.eventos) == 1


def test_422_misma_clave_con_otro_cuerpo(api: TestClient, titular: Any) -> None:
    titular()
    clave = uuid4()
    assert api.post(RUTA, json=CUERPO, headers=con_clave(clave)).status_code == 201
    otro = {**CUERPO, "alcances": ["FA_INGRESOS_AGREGADOS"]}
    _error(api.post(RUTA, json=otro, headers=con_clave(clave)), 422, "IDEMPOTENCIA_CONFLICTO")


def test_409_ya_vigente_con_otra_clave(api: TestClient, titular: Any) -> None:
    titular()
    creado = api.post(RUTA, json=CUERPO, headers=con_clave()).json()
    cuerpo = _error(
        api.post(RUTA, json=CUERPO, headers=con_clave()), 409, "CONSENTIMIENTO_YA_VIGENTE"
    )
    assert cuerpo["detalles"] == {"consentimientoId": creado["consentimientoId"]}


def test_422_sin_vigencia_no_crea_consentimiento_ni_evento(
    api: TestClient, titular: Any, consentimientos: Any
) -> None:
    """AC3: se informa qué falta y no queda ningún rastro."""
    titular()
    sin_vigencia = {k: v for k, v in CUERPO.items() if k != "vigenciaMeses"}
    cuerpo = _error(
        api.post(RUTA, json=sin_vigencia, headers=con_clave()), 422, "SOLICITUD_INVALIDA"
    )
    assert cuerpo["detalles"] == {"campos": ["vigenciaMeses"]}
    assert consentimientos.consentimientos == {}
    assert consentimientos.eventos == []


def test_422_informa_todos_los_campos_que_faltan(api: TestClient, titular: Any) -> None:
    titular()
    cuerpo = _error(api.post(RUTA, json={}, headers=con_clave()), 422, "SOLICITUD_INVALIDA")
    assert cuerpo["detalles"] == {
        "campos": ["proposito", "alcances", "vigenciaMeses", "versionTexto", "idioma"]
    }


@pytest.mark.parametrize(
    ("cambio", "campo"),
    [
        ({"alcances": []}, "alcances"),
        ({"alcances": ["DATOS_BIOMETRICOS"]}, "alcances"),
        ({"proposito": "OTRO"}, "proposito"),
        ({"idioma": "fr-FR"}, "idioma"),
        ({"versionTexto": "  "}, "versionTexto"),
        ({"vigenciaMeses": None}, "vigenciaMeses"),
    ],
)
def test_422_valores_invalidos(
    api: TestClient, titular: Any, cambio: dict[str, Any], campo: str
) -> None:
    titular()
    cuerpo = _error(
        api.post(RUTA, json={**CUERPO, **cambio}, headers=con_clave()), 422, "SOLICITUD_INVALIDA"
    )
    assert cuerpo["detalles"] == {"campos": [campo]}


def test_422_tipos_incorrectos_los_valida_el_esquema(api: TestClient, titular: Any) -> None:
    titular()
    cuerpo = _error(
        api.post(RUTA, json={**CUERPO, "vigenciaMeses": "doce"}, headers=con_clave()),
        422,
        "SOLICITUD_INVALIDA",
    )
    assert cuerpo["detalles"] == {"campos": ["vigenciaMeses"]}


def test_422_cuerpo_que_no_es_json(api: TestClient, titular: Any) -> None:
    titular()
    respuesta = api.post(
        RUTA, content="{no es json", headers={**con_clave(), "Content-Type": "application/json"}
    )
    _error(respuesta, 422, "SOLICITUD_INVALIDA")


def test_400_cuerpo_con_bytes_que_no_son_utf8(api: TestClient, titular: Any) -> None:
    titular()
    respuesta = api.post(
        RUTA, content=b"\xc3\x28", headers={**con_clave(), "Content-Type": "application/json"}
    )
    _error(respuesta, 400, "ERROR_HTTP")


@pytest.mark.parametrize("clave", [None, "no-es-uuid"])
def test_422_idempotency_key_obligatoria_y_uuid(
    api: TestClient, titular: Any, consentimientos: Any, clave: str | None
) -> None:
    titular()
    cabeceras = actor() if clave is None else {**actor(), "Idempotency-Key": clave}
    cuerpo = _error(api.post(RUTA, json=CUERPO, headers=cabeceras), 422, "SOLICITUD_INVALIDA")
    assert cuerpo["detalles"] == {"campos": ["Idempotency-Key"]}
    assert consentimientos.consentimientos == {}


def test_422_vigencia_fuera_de_la_lista(api: TestClient, titular: Any) -> None:
    titular()
    respuesta = api.post(RUTA, json={**CUERPO, "vigenciaMeses": 3}, headers=con_clave())
    _error(respuesta, 422, "VIGENCIA_INVALIDA")


def test_422_texto_que_no_es_el_vigente(
    api: TestClient, titular: Any, consentimientos: Any
) -> None:
    titular()
    respuesta = api.post(RUTA, json={**CUERPO, "versionTexto": "1999-01-01.1"}, headers=con_clave())
    _error(respuesta, 422, "TEXTO_NO_VIGENTE")
    assert consentimientos.consentimientos == {}


@pytest.mark.parametrize("estado", [EstadoIdentidad.PENDIENTE, EstadoIdentidad.RECHAZADO])
def test_403_identidad_no_verificada(
    api: TestClient, titular: Any, consentimientos: Any, estado: EstadoIdentidad
) -> None:
    titular(estado=estado)
    _error(api.post(RUTA, json=CUERPO, headers=con_clave()), 403, "IDENTIDAD_NO_VERIFICADA")
    assert consentimientos.consentimientos == {}


def test_404_el_titular_aun_no_tiene_cliente(api: TestClient) -> None:
    _error(api.post(RUTA, json=CUERPO, headers=con_clave()), 404, "CLIENTE_NO_ENCONTRADO")


@pytest.mark.parametrize(
    ("headers", "estado", "codigo"),
    [
        ({}, 401, "ACTOR_REQUERIDO"),
        ({"X-Actor-Id": SUB}, 401, "ACTOR_REQUERIDO"),
        ({"X-Actor-Id": SUB, "X-Actor-Rol": "ASESOR"}, 403, "ROL_NO_AUTORIZADO"),
    ],
)
def test_el_actor_se_valida_antes_que_el_cuerpo(
    api: TestClient, headers: dict[str, str], estado: int, codigo: str
) -> None:
    """El 401/403 gana al 422 aunque falten la clave y el cuerpo."""
    ruta_detalle = f"{RUTA}/{uuid4()}"
    _error(api.post(RUTA, json={}, headers=headers), estado, codigo)
    _error(api.get(RUTA, headers=headers), estado, codigo)
    _error(api.get(ruta_detalle, headers=headers), estado, codigo)


def test_lista_vacia_si_el_titular_no_tiene_cliente(api: TestClient) -> None:
    respuesta = api.get(RUTA, headers=actor())
    assert respuesta.status_code == 200
    assert respuesta.json() == []


def test_lista_del_mas_reciente_al_mas_antiguo_con_estado_derivado(
    api: TestClient, titular: Any, reloj: Any
) -> None:
    titular()
    primero = api.post(RUTA, json={**CUERPO, "vigenciaMeses": 6}, headers=con_clave()).json()
    reloj.avanzar(timedelta(days=200))  # ya pasaron los 6 meses: el almacenado sigue ACTIVO
    segundo = api.post(RUTA, json=CUERPO, headers=con_clave()).json()
    lista = api.get(RUTA, headers=actor()).json()
    assert [c["consentimientoId"] for c in lista] == [
        segundo["consentimientoId"],
        primero["consentimientoId"],
    ]
    assert [c["estado"] for c in lista] == ["ACTIVO", "VENCIDO"]


def test_detalle_del_propio_consentimiento(api: TestClient, titular: Any) -> None:
    titular()
    creado = api.post(RUTA, json=CUERPO, headers=con_clave()).json()
    respuesta = api.get(f"{RUTA}/{creado['consentimientoId']}", headers=actor())
    assert respuesta.status_code == 200
    assert respuesta.json() == creado


def test_detalle_vencido_se_deriva_al_leer(api: TestClient, titular: Any, reloj: Any) -> None:
    titular()
    creado = api.post(RUTA, json={**CUERPO, "vigenciaMeses": 6}, headers=con_clave()).json()
    reloj.avanzar(timedelta(days=200))
    cuerpo = api.get(f"{RUTA}/{creado['consentimientoId']}", headers=actor()).json()
    assert cuerpo["estado"] == EstadoConsentimiento.VENCIDO
    assert cuerpo["revocadoEn"] is None


def test_404_detalle_inexistente(api: TestClient, titular: Any) -> None:
    titular()
    _error(api.get(f"{RUTA}/{uuid4()}", headers=actor()), 404, "CONSENTIMIENTO_NO_ENCONTRADO")


def test_404_detalle_sin_cliente(api: TestClient) -> None:
    _error(api.get(f"{RUTA}/{uuid4()}", headers=actor()), 404, "CONSENTIMIENTO_NO_ENCONTRADO")


def test_404_un_titular_no_ve_los_consentimientos_de_otro(api: TestClient, titular: Any) -> None:
    """Un consentimiento ajeno es indistinguible de uno inexistente (ADR 0006)."""
    titular(SUB)
    titular(OTRO_SUB)
    ajeno = api.post(RUTA, json=CUERPO, headers=con_clave(sub=OTRO_SUB)).json()
    inexistente = api.get(f"{RUTA}/{uuid4()}", headers=actor())
    ajeno_respuesta = api.get(f"{RUTA}/{ajeno['consentimientoId']}", headers=actor())
    _error(ajeno_respuesta, 404, "CONSENTIMIENTO_NO_ENCONTRADO")
    assert ajeno_respuesta.json()["detalles"] == inexistente.json()["detalles"]
    assert ajeno_respuesta.json()["mensaje"] == inexistente.json()["mensaje"]
    assert api.get(RUTA, headers=actor()).json() == []
    assert len(api.get(RUTA, headers=actor(OTRO_SUB)).json()) == 1


def test_el_cliente_sale_del_token_nunca_del_cuerpo(
    api: TestClient, titular: Any, consentimientos: Any
) -> None:
    propio = titular(SUB)
    ajeno = titular(OTRO_SUB)
    respuesta = api.post(
        RUTA, json={**CUERPO, "clienteId": str(ajeno.id)}, headers=con_clave(sub=SUB)
    )
    assert respuesta.status_code == 201
    (creado,) = consentimientos.consentimientos.values()
    assert creado.cliente_id == propio.id


def test_texto_vigente(api: TestClient) -> None:
    respuesta = api.get(
        "/v1/consentimientos/textos/vigente",
        params={"proposito": "PERFILAMIENTO_PRECIO", "idioma": "en-US"},
    )
    assert respuesta.status_code == 200, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["versionTexto"] == TEXTO.version
    assert cuerpo["idioma"] == "en-US"
    assert cuerpo["vigenciasMeses"] == [6, 12]
    assert cuerpo["vigenciaDefectoMeses"] == 12
    assert "no legal validity" in cuerpo["aviso"]
    assert set(cuerpo) == {
        "proposito",
        "idioma",
        "versionTexto",
        "titulo",
        "cuerpo",
        "aviso",
        "vigenciasMeses",
        "vigenciaDefectoMeses",
    }


@pytest.mark.parametrize(
    ("params", "campos"),
    [
        ({}, ["proposito", "idioma"]),
        ({"proposito": "PERFILAMIENTO_PRECIO"}, ["idioma"]),
        ({"proposito": "OTRO", "idioma": "es-CO"}, ["proposito"]),
        ({"proposito": "PERFILAMIENTO_PRECIO", "idioma": "fr-FR"}, ["idioma"]),
    ],
)
def test_422_texto_vigente_con_parametros_invalidos(
    api: TestClient, params: dict[str, str], campos: list[str]
) -> None:
    respuesta = api.get("/v1/consentimientos/textos/vigente", params=params)
    assert _error(respuesta, 422, "SOLICITUD_INVALIDA")["detalles"] == {"campos": campos}
