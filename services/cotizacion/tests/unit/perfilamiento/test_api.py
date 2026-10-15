"""API /v1/perfiles (W11-P12): cada fila de 04 §B.7, idempotencia y minimización."""

from collections.abc import Iterator
from dataclasses import replace
from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg
import pytest
from cotizacion.adapters.inbound.http import perfilamiento
from cotizacion.application.perfilamiento.crear_perfil import ConfigPerfilamiento
from cotizacion.domain.perfilamiento.errores import (
    ClienteNoEncontrado,
    ConflictoIdempotencia,
    DatosDeclaradosInvalidos,
    DependenciaNoDisponible,
    IdentidadNoVerificada,
    IdentidadPendiente,
    ModeloNoDisponible,
    PerfilNoEncontrado,
)
from cotizacion.domain.perfilamiento.modelos import EstadoPerfil, ResultadoEvaluacion
from fastapi import FastAPI
from fastapi.testclient import TestClient
from solventa_common.errors import registrar_manejadores
from solventa_common.settings import ServiceSettings
from solventa_common.telemetry import instrumentar

ASESOR = {
    "X-Actor-Id": "asesor-sintetico",
    "X-Actor-Rol": "ASESOR",
    "Idempotency-Key": "00000000-0000-4000-b000-0000000000c1",
    "X-Correlation-Id": "corr-api",
}
C01 = {
    "clienteId": "00000000-0000-4000-8000-000000000001",
    "productoId": "vida-hipotecario",
    "datosDeclarados": {
        "fechaNacimiento": "1990-04-18",
        "plazoCreditoMeses": 180,
        "sumaAsegurada": 180000000,
    },
}


class CrearPerfilFalso:
    def __init__(self, perfil: Any) -> None:
        self.config = ConfigPerfilamiento()
        self.respuesta: Any = (perfil, True)
        self.comandos: list[Any] = []

    async def ejecutar(self, comando: Any) -> Any:
        self.comandos.append(comando)
        if isinstance(self.respuesta, BaseException):
            raise self.respuesta
        return self.respuesta


class ReconstruirFalso:
    def __init__(self) -> None:
        self.respuesta: Any = (True, "2026.10.1", "sha256:2026.10.1")

    async def ejecutar(self, perfil_id: UUID) -> Any:
        if isinstance(self.respuesta, BaseException):
            raise self.respuesta
        return self.respuesta


@pytest.fixture
def app(perfil_c01: Any, escenario: Any) -> FastAPI:
    aplicacion = instrumentar(FastAPI(), ServiceSettings(service_name="cotizacion"))
    registrar_manejadores(aplicacion)
    aplicacion.include_router(perfilamiento.router)
    aplicacion.state.crear_perfil = CrearPerfilFalso(perfil_c01)
    aplicacion.state.reconstruir_perfil = ReconstruirFalso()
    escenario.repo.perfiles[perfil_c01.id] = perfil_c01
    aplicacion.state.perfiles = escenario.repo
    return aplicacion


@pytest.fixture
def api(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, raise_server_exceptions=False) as cliente:
        yield cliente


def test_crear_responde_201_con_el_perfil(api: TestClient, app: FastAPI) -> None:
    respuesta = api.post("/v1/perfiles", json=C01, headers=ASESOR)
    assert respuesta.status_code == 201
    cuerpo = respuesta.json()
    assert (cuerpo["estado"], cuerpo["nivelRiesgo"], cuerpo["puntaje"], cuerpo["confianza"]) == (
        "CALCULADO",
        "BAJO",
        75.9,
        0.92,
    )
    assert (cuerpo["fuentesConsultadas"], cuerpo["fuentesDeclaradas"]) == (4, 4)
    assert (cuerpo["versionModelo"], cuerpo["presupuestoMs"], cuerpo["duracionMs"]) == (
        "2026.10.1",
        400,
        212,
    )
    assert cuerpo["calculadoEn"] == "2026-10-15T15:00:00.212000Z"
    assert cuerpo["factores"][0] == {
        "codigo": "PUNTUALIDAD_PAGOS",
        "fuente": "FA_HISTORIAL_PAGOS_12M",
        "peso": 0.3,
        "estado": "EVALUADO",
        "contribucion": 0.9,
        "direccion": "REDUCE_RIESGO",
        "motivo": None,
    }
    assert cuerpo["fuentes"][0] == {
        "fuente": "FA_PRODUCTOS_VIGENTES",
        "tipo": "FINANZAS_ABIERTAS",
        "estado": "CONSULTADA",
        "versionFuente": "3.1",
        "confianza": 0.95,
        "consultadaEn": "2026-10-15T15:00:00.100000Z",
        "duracionMs": 52,
    }
    assert cuerpo["consentimiento"] == {
        "decision": "PERMITIDO",
        "causa": None,
        "vigenteHasta": "2027-10-01T00:00:00Z",
    }
    (comando,) = app.state.crear_perfil.comandos
    assert (comando.actor_id, comando.correlation_id) == ("asesor-sintetico", "corr-api")
    assert comando.idempotency_key == UUID(ASESOR["Idempotency-Key"])
    assert str(comando.datos_declarados.suma_asegurada) == "180000000"


def test_la_respuesta_no_expone_insumos_ni_datos_declarados(api: TestClient) -> None:
    respuesta = api.post("/v1/perfiles", json=C01, headers=ASESOR)
    cuerpo = respuesta.json()
    assert not {"insumos", "datosDeclarados", "hashModelo"} & set(cuerpo)
    for centinela in ("1800000", "6500000", "180000000", "1990-04-18"):
        assert centinela not in respuesta.text


def test_misma_clave_y_mismo_cuerpo_responde_200(
    api: TestClient, app: FastAPI, perfil_c01: Any
) -> None:
    app.state.crear_perfil.respuesta = (perfil_c01, False)
    respuesta = api.post("/v1/perfiles", json=C01, headers=ASESOR)
    assert respuesta.status_code == 200
    assert respuesta.json()["id"] == str(perfil_c01.id)


@pytest.mark.parametrize(
    ("error", "estado", "codigo"),
    [
        (ConflictoIdempotencia(), 422, "IDEMPOTENCIA_CONFLICTO"),
        (ClienteNoEncontrado(), 404, "CLIENTE_NO_ENCONTRADO"),
        (IdentidadNoVerificada(), 403, "IDENTIDAD_NO_VERIFICADA"),
        (IdentidadPendiente(), 409, "IDENTIDAD_PENDIENTE"),
        (ModeloNoDisponible(), 422, "PRODUCTO_NO_DISPONIBLE"),
        (psycopg.OperationalError("conexión rechazada en 10.0.0.1"), 500, "ERROR_INTERNO"),
    ],
)
def test_errores_de_b7(
    api: TestClient, app: FastAPI, error: Exception, estado: int, codigo: str
) -> None:
    app.state.crear_perfil.respuesta = error
    respuesta = api.post("/v1/perfiles", json=C01, headers=ASESOR)
    assert respuesta.status_code == estado
    cuerpo = respuesta.json()
    assert set(cuerpo) == {"codigo", "mensaje", "correlationId", "detalles"}
    assert (cuerpo["codigo"], cuerpo["correlationId"], cuerpo["detalles"]) == (
        codigo,
        "corr-api",
        {},
    )
    assert "10.0.0.1" not in respuesta.text


def test_dependencia_no_disponible_responde_503_con_su_nombre(
    api: TestClient, app: FastAPI
) -> None:
    app.state.crear_perfil.respuesta = DependenciaNoDisponible("catalogo.modelo")
    respuesta = api.post("/v1/perfiles", json=C01, headers=ASESOR)
    assert respuesta.status_code == 503
    assert respuesta.json()["codigo"] == "DEPENDENCIA_NO_DISPONIBLE"
    assert respuesta.json()["detalles"] == {"dependencia": "catalogo.modelo"}


def test_datos_declarados_invalidos_responden_422_con_los_campos(
    api: TestClient, app: FastAPI
) -> None:
    app.state.crear_perfil.respuesta = DatosDeclaradosInvalidos(
        ("datosDeclarados.fechaNacimiento",)
    )
    respuesta = api.post("/v1/perfiles", json=C01, headers=ASESOR)
    assert respuesta.status_code == 422
    assert respuesta.json()["codigo"] == "SOLICITUD_INVALIDA"
    assert respuesta.json()["detalles"] == {"campos": ["datosDeclarados.fechaNacimiento"]}


def _con(**cambios: Any) -> dict[str, Any]:
    cuerpo = {**C01, "datosDeclarados": dict(C01["datosDeclarados"])}
    for clave, valor in cambios.items():
        if clave in cuerpo["datosDeclarados"]:
            cuerpo["datosDeclarados"][clave] = valor
        else:
            cuerpo[clave] = valor
    return cuerpo


@pytest.mark.parametrize(
    ("cuerpo", "campo"),
    [
        (_con(plazoCreditoMeses=11), "datosDeclarados.plazoCreditoMeses"),
        (_con(plazoCreditoMeses=361), "datosDeclarados.plazoCreditoMeses"),
        (_con(sumaAsegurada=0), "datosDeclarados.sumaAsegurada"),
        (_con(sumaAsegurada=5_000_000_001), "datosDeclarados.sumaAsegurada"),
        (_con(fechaNacimiento="18/04/1990"), "datosDeclarados.fechaNacimiento"),
        (_con(productoId="Vida Hipotecario"), "productoId"),
        (_con(clienteId="no-es-uuid"), "clienteId"),
    ],
)
def test_cuerpo_invalido_responde_422_sin_valores(
    api: TestClient, cuerpo: dict[str, Any], campo: str
) -> None:
    respuesta = api.post("/v1/perfiles", json=cuerpo, headers=ASESOR)
    assert respuesta.status_code == 422
    assert respuesta.json()["codigo"] == "SOLICITUD_INVALIDA"
    assert respuesta.json()["detalles"]["campos"] == [campo]
    assert "180000000" not in respuesta.text


@pytest.mark.parametrize("clave", [None, "no-es-uuid"])
def test_idempotency_key_obligatoria_y_uuid(api: TestClient, clave: str | None) -> None:
    encabezados = {k: v for k, v in ASESOR.items() if k != "Idempotency-Key"}
    if clave:
        encabezados["Idempotency-Key"] = clave
    respuesta = api.post("/v1/perfiles", json=C01, headers=encabezados)
    assert respuesta.status_code == 422
    assert respuesta.json()["detalles"]["campos"] == ["Idempotency-Key"]


@pytest.mark.parametrize(
    ("encabezados", "estado", "codigo"),
    [
        ({}, 401, "ACTOR_REQUERIDO"),
        ({"X-Actor-Id": "a"}, 401, "ACTOR_REQUERIDO"),
        ({"X-Actor-Rol": "ASESOR"}, 401, "ACTOR_REQUERIDO"),
        ({"X-Actor-Id": "a", "X-Actor-Rol": "ADMIN_REGLAS"}, 403, "ROL_NO_AUTORIZADO"),
    ],
)
@pytest.mark.parametrize(
    ("metodo", "ruta"),
    [
        ("post", "/v1/perfiles"),
        ("get", "/v1/perfiles?clienteId=00000000-0000-4000-8000-000000000001"),
        ("get", "/v1/perfiles/00000000-0000-4000-a000-000000000001"),
        ("get", "/v1/perfiles/00000000-0000-4000-a000-000000000001/reconstruccion"),
    ],
)
def test_actor_obligatorio_antes_de_validar(
    api: TestClient,
    metodo: str,
    ruta: str,
    encabezados: dict[str, str],
    estado: int,
    codigo: str,
) -> None:
    respuesta = api.request(metodo, ruta, json={}, headers=encabezados)
    assert respuesta.status_code == estado
    assert respuesta.json()["codigo"] == codigo


def test_obtener_por_id(api: TestClient, perfil_c01: Any) -> None:
    respuesta = api.get(f"/v1/perfiles/{perfil_c01.id}", headers=ASESOR)
    assert respuesta.status_code == 200
    assert respuesta.json()["id"] == str(perfil_c01.id)


def test_perfil_inexistente_responde_404(api: TestClient) -> None:
    respuesta = api.get(f"/v1/perfiles/{UUID(int=404)}", headers=ASESOR)
    assert respuesta.status_code == 404
    assert respuesta.json()["codigo"] == "PERFIL_NO_ENCONTRADO"


def test_listar_por_cliente(api: TestClient, perfil_c01: Any, escenario: Any) -> None:
    anterior = replace(
        perfil_c01, id=UUID(int=5), calculado_en=perfil_c01.calculado_en.replace(hour=9)
    )
    escenario.repo.perfiles[anterior.id] = anterior
    respuesta = api.get(
        "/v1/perfiles", params={"clienteId": str(perfil_c01.cliente_id)}, headers=ASESOR
    )
    items = respuesta.json()["items"]
    assert [i["id"] for i in items] == [str(perfil_c01.id), str(anterior.id)]
    assert set(items[0]) == {
        "id",
        "estado",
        "nivelRiesgo",
        "confianza",
        "versionModelo",
        "calculadoEn",
        "perfilAnteriorId",
    }
    limitado = api.get(
        "/v1/perfiles",
        params={"clienteId": str(perfil_c01.cliente_id), "limite": 1},
        headers=ASESOR,
    )
    assert len(limitado.json()["items"]) == 1


@pytest.mark.parametrize(
    "params",
    [
        {"clienteId": "x"},
        {},
        {"clienteId": str(UUID(int=1)), "limite": 0},
        {"clienteId": str(UUID(int=1)), "limite": 51},
    ],
)
def test_listar_con_parametros_invalidos(api: TestClient, params: dict[str, Any]) -> None:
    respuesta = api.get("/v1/perfiles", params=params, headers=ASESOR)
    assert respuesta.status_code == 422
    assert respuesta.json()["codigo"] == "SOLICITUD_INVALIDA"


def test_reconstruccion(api: TestClient, app: FastAPI, perfil_c01: Any) -> None:
    respuesta = api.get(f"/v1/perfiles/{perfil_c01.id}/reconstruccion", headers=ASESOR)
    assert respuesta.json() == {
        "coincide": True,
        "versionModelo": "2026.10.1",
        "hashModelo": "sha256:2026.10.1",
    }
    app.state.reconstruir_perfil.respuesta = PerfilNoEncontrado()
    respuesta = api.get(f"/v1/perfiles/{perfil_c01.id}/reconstruccion", headers=ASESOR)
    assert respuesta.status_code == 404
    assert respuesta.json()["codigo"] == "PERFIL_NO_ENCONTRADO"


def test_perfil_insuficiente_sin_consentimiento(
    api: TestClient, app: FastAPI, perfil_c01: Any
) -> None:

    vacio = ResultadoEvaluacion(
        EstadoPerfil.INFORMACION_INSUFICIENTE, None, None, Decimal("0.00"), (), (), (), None
    )
    app.state.crear_perfil.respuesta = (
        replace(perfil_c01, resultado=vacio, consentimiento=None),
        True,
    )
    cuerpo = api.post("/v1/perfiles", json=C01, headers=ASESOR).json()
    assert (cuerpo["nivelRiesgo"], cuerpo["puntaje"], cuerpo["consentimiento"]) == (
        None,
        None,
        None,
    )
    assert cuerpo["confianza"] == 0.0
