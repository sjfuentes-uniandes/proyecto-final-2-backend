from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from catalogo.config import Settings
from catalogo.main import crear_app
from fastapi.testclient import TestClient

ADMIN = {"X-Actor-Id": "actuaria-sintetica", "X-Actor-Rol": "ADMIN_REGLAS"}
BASE = "/v1/modelos-riesgo"


def _solicitud(definicion: dict[str, Any], /, **cambios: Any) -> dict[str, Any]:
    solicitud = {
        "version": "2026.10.2",
        "productoId": "vida-hipotecario",
        "vigenteDesde": "2026-10-15T15:00:00Z",
        "definicion": definicion,
    }
    return solicitud | cambios


def _sin_valores(cuerpo: dict[str, Any]) -> None:
    assert set(cuerpo) == {"codigo", "mensaje", "correlationId", "detalles"}


def test_publicar_responde_201_con_hash_y_definicion(
    api: TestClient, definicion_candidata: dict[str, Any], repositorio: Any
) -> None:
    definicion_candidata["niveles"][0]["desde"] = "80"
    respuesta = api.post(BASE, json=_solicitud(definicion_candidata), headers=ADMIN)
    assert respuesta.status_code == 201
    cuerpo = respuesta.json()
    assert set(cuerpo) == {"version", "productoId", "vigenteDesde", "hash", "definicion"}
    assert cuerpo["definicion"] == definicion_candidata
    assert cuerpo["hash"] == repositorio.modelos["2026.10.2"].hash
    assert cuerpo["hash"].startswith("sha256:")
    assert repositorio.modelos["2026.10.2"].publicado_por == "actuaria-sintetica"


def test_publicar_una_version_existente_responde_409(
    api: TestClient, definicion_candidata: dict[str, Any]
) -> None:
    solicitud = _solicitud(definicion_candidata, version="2026.10.1")
    respuesta = api.post(BASE, json=solicitud, headers=ADMIN)
    assert respuesta.status_code == 409
    assert respuesta.json()["codigo"] == "VERSION_EXISTENTE"


def test_definicion_invalida_responde_422_con_los_errores_sin_valores(
    api: TestClient, definicion_candidata: dict[str, Any]
) -> None:
    definicion_candidata["factores"][0]["peso"] = "0.31"
    definicion_candidata["obligatorios"].append("saldo_secreto")
    respuesta = api.post(BASE, json=_solicitud(definicion_candidata), headers=ADMIN)
    assert respuesta.status_code == 422
    cuerpo = respuesta.json()
    _sin_valores(cuerpo)
    assert cuerpo["codigo"] == "DEFINICION_INVALIDA"
    assert cuerpo["detalles"] == {
        "errores": ["PESOS_NO_SUMAN_1", "OBLIGATORIO_DESCONOCIDO:saldo_secreto"]
    }
    assert "0.31" not in respuesta.text


def test_vigencia_retroactiva_responde_422(
    api: TestClient, definicion_candidata: dict[str, Any]
) -> None:
    solicitud = _solicitud(definicion_candidata, vigenteDesde="2026-10-15T14:59:54.999Z")
    respuesta = api.post(BASE, json=solicitud, headers=ADMIN)
    assert respuesta.status_code == 422
    assert respuesta.json()["codigo"] == "VIGENCIA_RETROACTIVA"


@pytest.mark.parametrize(
    "cambios",
    [
        {"version": "v2"},
        {"productoId": "Vida Hipotecario"},
        {"vigenteDesde": "2026-10-15T15:00:00"},
        {"definicion": {"productoId": "vida-hipotecario"}},
    ],
    ids=["version", "producto", "fecha-sin-zona", "definicion-incompleta"],
)
def test_cuerpo_invalido_responde_422_solicitud_invalida(
    api: TestClient, definicion_candidata: dict[str, Any], cambios: dict[str, Any]
) -> None:
    respuesta = api.post(BASE, json=_solicitud(definicion_candidata, **cambios), headers=ADMIN)
    assert respuesta.status_code == 422
    assert respuesta.json()["codigo"] == "SOLICITUD_INVALIDA"
    assert respuesta.json()["detalles"]["campos"]


@pytest.mark.parametrize(
    "encabezados",
    [
        {},
        {"X-Actor-Id": "x"},
        {"X-Actor-Rol": "ADMIN_REGLAS"},
        {"X-Actor-Id": "", "X-Actor-Rol": "ADMIN_REGLAS"},
    ],
    ids=["ninguno", "sin-rol", "sin-id", "id-vacio"],
)
def test_sin_actor_responde_401_antes_de_validar_el_cuerpo(
    api: TestClient, encabezados: dict[str, str]
) -> None:
    respuesta = api.post(BASE, json={}, headers=encabezados)
    assert respuesta.status_code == 401
    _sin_valores(respuesta.json())
    assert respuesta.json()["codigo"] == "ACTOR_REQUERIDO"


def test_rol_distinto_responde_403(api: TestClient, definicion_candidata: dict[str, Any]) -> None:
    encabezados = {"X-Actor-Id": "asesor-sintetico", "X-Actor-Rol": "ASESOR"}
    respuesta = api.post(BASE, json=_solicitud(definicion_candidata), headers=encabezados)
    assert respuesta.status_code == 403
    assert respuesta.json()["codigo"] == "ROL_NO_AUTORIZADO"


def test_consultar_por_version(api: TestClient, modelo_candidato: Any) -> None:
    respuesta = api.get(f"{BASE}/2026.10.1")
    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["version"] == "2026.10.1"
    assert cuerpo["vigenteDesde"] == "2026-10-01T00:00:00Z"
    assert cuerpo["hash"] == modelo_candidato.hash


def test_vigente_sin_en_usa_el_reloj(
    api: TestClient, definicion_candidata: dict[str, Any], reloj: Any
) -> None:
    solicitud = _solicitud(definicion_candidata, vigenteDesde="2026-10-15T15:00:01Z")
    assert api.post(BASE, json=solicitud, headers=ADMIN).status_code == 201
    assert (
        api.get(f"{BASE}/vigente", params={"productoId": "vida-hipotecario"}).json()["version"]
        == "2026.10.1"
    )
    params = {"productoId": "vida-hipotecario", "en": "2026-10-15T15:00:01Z"}
    assert api.get(f"{BASE}/vigente", params=params).json()["version"] == "2026.10.2"


@pytest.mark.parametrize(
    ("ruta", "params"),
    [
        (f"{BASE}/2030.01.1", None),
        (f"{BASE}/vigente", {"productoId": "otro-producto"}),
        (f"{BASE}/vigente", {"productoId": "vida-hipotecario", "en": "2026-09-30T00:00:00Z"}),
    ],
    ids=["version", "producto", "antes-de-la-vigencia"],
)
def test_no_encontrado_responde_404(
    api: TestClient, ruta: str, params: dict[str, str] | None
) -> None:
    respuesta = api.get(ruta, params=params)
    assert respuesta.status_code == 404
    assert respuesta.json()["codigo"] == "MODELO_NO_ENCONTRADO"


@pytest.mark.parametrize(
    ("ruta", "params"),
    [
        (f"{BASE}/2026-10-1", None),
        (f"{BASE}/vigente", None),
        (f"{BASE}/vigente", {"productoId": "vida-hipotecario", "en": "2026-10-15T15:00:00"}),
    ],
    ids=["version-mal-formada", "sin-producto", "en-sin-zona"],
)
def test_consulta_invalida_responde_422(
    api: TestClient, ruta: str, params: dict[str, str] | None
) -> None:
    respuesta = api.get(ruta, params=params)
    assert respuesta.status_code == 422
    assert respuesta.json()["codigo"] == "SOLICITUD_INVALIDA"


class _Conexion:
    def __init__(self, falla: bool) -> None:
        self.falla = falla

    async def execute(self, sql: str) -> None:
        if self.falla:
            raise OSError("sin conexión")


class _Pool:
    def __init__(self, falla: bool) -> None:
        self.falla = falla

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[_Conexion]:
        yield _Conexion(self.falla)


@pytest.mark.parametrize(("falla", "estado"), [(False, 200), (True, 503)])
def test_health_verifica_la_base(api: TestClient, falla: bool, estado: int) -> None:
    api.app.state.pool = _Pool(falla)  # type: ignore[attr-defined]
    assert api.get("/health").status_code == estado


def test_sin_base_ni_repositorio_no_expone_casos_de_uso() -> None:
    with TestClient(crear_app(Settings())) as cliente:
        assert cliente.get("/health").json() == {"status": "ok"}
        assert not hasattr(cliente.app.state, "consultar_modelo")  # type: ignore[attr-defined]
