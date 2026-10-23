"""Insumos de la oferta para la suscripción (HU-W13, W13-B12) y `vigenteHasta` (DS2-20) sobre la
app real con los puertos en fakes (C01 el 2026-10-15)."""

import dataclasses
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from cotizacion.application.suscripcion.insumos import (
    ConfigInsumos,
    ConsultarInsumos,
    coincidencias_listas,
)
from cotizacion.config import Settings
from cotizacion.domain.cotizacion.errores import CotizacionNoEncontrada, RolNoPermitido
from cotizacion.domain.cotizacion.modelos import Actor, RolActor
from cotizacion.domain.perfilamiento.errores import ClienteNoEncontrado, DependenciaNoDisponible
from cotizacion.domain.perfilamiento.modelos import (
    EstadoCampo,
    EstadoFuente,
    EstadoIdentidad,
    Fuente,
    ValorCampo,
)
from cotizacion.main import Puertos, crear_app
from fastapi.testclient import TestClient

ASESOR = {"X-Actor-Id": "asesor-sintetico", "X-Actor-Rol": "ASESOR"}
ACTOR = Actor("asesor-sintetico", RolActor.ASESOR)
pytestmark = pytest.mark.anyio


async def _cotizacion(escenario_cotizacion: Any, nueva_cotizacion: Any, **cambios: Any) -> Any:
    cotizacion, _, _ = await escenario_cotizacion.caso().ejecutar(nueva_cotizacion(**cambios))
    return cotizacion


def _caso(escenario_cotizacion: Any, **config: int) -> ConsultarInsumos:
    return ConsultarInsumos(
        escenario_cotizacion.repo,
        escenario_cotizacion.perfiles,
        escenario_cotizacion.identidad,
        ConfigInsumos(**config),
    )


async def test_insumos_con_perfil(
    escenario_cotizacion: Any, nueva_cotizacion: Any, perfil_c01: Any
) -> None:
    cotizacion = await _cotizacion(escenario_cotizacion, nueva_cotizacion, perfil_id=perfil_c01.id)
    insumos = await _caso(escenario_cotizacion).ejecutar(cotizacion.id, ACTOR)
    assert insumos.cotizacion == cotizacion
    assert insumos.perfil == perfil_c01
    assert insumos.estado_identidad is EstadoIdentidad.VERIFICADO
    assert insumos.vigente_hasta == datetime(2026, 10, 19, 15, 0, tzinfo=UTC)
    assert coincidencias_listas(perfil_c01) == 0


async def test_insumos_sin_perfil_y_otra_vigencia(
    escenario_cotizacion: Any, nueva_cotizacion: Any
) -> None:
    cotizacion = await _cotizacion(escenario_cotizacion, nueva_cotizacion)
    insumos = await _caso(escenario_cotizacion, oferta_vigencia_horas=24).ejecutar(
        cotizacion.id, ACTOR
    )
    assert insumos.perfil is None
    assert insumos.vigente_hasta == datetime(2026, 10, 16, 15, 0, tzinfo=UTC)
    assert coincidencias_listas(None) is None


async def test_solo_el_asesor_y_cotizacion_inexistente(
    escenario_cotizacion: Any, nueva_cotizacion: Any
) -> None:
    cotizacion = await _cotizacion(escenario_cotizacion, nueva_cotizacion)
    with pytest.raises(RolNoPermitido):
        await _caso(escenario_cotizacion).ejecutar(
            cotizacion.id, Actor("SOC-000001", RolActor.SOCIO)
        )
    with pytest.raises(CotizacionNoEncontrada):
        await _caso(escenario_cotizacion).ejecutar(uuid4(), ACTOR)


@pytest.mark.parametrize(
    "falla",
    [DependenciaNoDisponible("clientes"), ClienteNoEncontrado(), "lento"],
)
async def test_la_identidad_falla_cerrada(
    escenario_cotizacion: Any, nueva_cotizacion: Any, falla: Any
) -> None:
    """Timeout, falla técnica o un cliente que clientes no conoce: 503 clientes.identidad."""
    cotizacion = await _cotizacion(escenario_cotizacion, nueva_cotizacion)
    identidad = escenario_cotizacion.identidad
    if falla == "lento":
        identidad.demora_s = 0.2
    else:
        identidad.respuesta = falla
    with pytest.raises(DependenciaNoDisponible) as error:
        await _caso(escenario_cotizacion, dependencia_timeout_ms=20).ejecutar(cotizacion.id, ACTOR)
    assert error.value.dependencia == "clientes.identidad"


def _con_listas(perfil: Any, estado: EstadoFuente, campo: ValorCampo | None) -> Any:
    fuentes = tuple(
        dataclasses.replace(
            f,
            estado=estado,
            campos={} if campo is None else {"coincidencias_listas_restrictivas": campo},
        )
        if f.fuente is Fuente.DA_REGISTROS_PUBLICOS
        else f
        for f in perfil.fuentes
    )
    return dataclasses.replace(perfil, fuentes=fuentes)


@pytest.mark.parametrize(
    ("estado", "campo", "esperado"),
    [
        (EstadoFuente.CONSULTADA, ValorCampo(EstadoCampo.DISPONIBLE, 2), 2),
        (EstadoFuente.CONSULTADA, ValorCampo(EstadoCampo.NO_DISPONIBLE, None), None),
        (EstadoFuente.CONSULTADA, None, None),
        (EstadoFuente.TIEMPO_AGOTADO, ValorCampo(EstadoCampo.DISPONIBLE, 1), None),
    ],
)
def test_coincidencias_solo_si_se_consultaron(
    perfil_c01: Any, estado: EstadoFuente, campo: ValorCampo | None, esperado: int | None
) -> None:
    assert coincidencias_listas(_con_listas(perfil_c01, estado, campo)) == esperado


def test_sin_la_fuente_de_registros_publicos(perfil_c01: Any) -> None:
    sin_registros = dataclasses.replace(
        perfil_c01,
        fuentes=tuple(
            f for f in perfil_c01.fuentes if f.fuente is not Fuente.DA_REGISTROS_PUBLICOS
        ),
    )
    assert coincidencias_listas(sin_registros) is None


# --- API ------------------------------------------------------------------------------------


@pytest.fixture
def api(escenario: Any, escenario_cotizacion: Any) -> Iterator[TestClient]:
    puertos = Puertos(
        identidad=escenario_cotizacion.identidad,
        consentimiento=escenario.consentimiento,
        modelos=escenario.modelos,
        ejecutor=escenario.ejecutor,
        perfiles=escenario_cotizacion.perfiles,
        reloj=escenario_cotizacion.reloj,
        productos=escenario_cotizacion.productos,
        cotizaciones=escenario_cotizacion.repo,
    )
    with TestClient(crear_app(Settings(), puertos=puertos)) as cliente:
        yield cliente


def _cotizar(api: TestClient, perfil_id: str | None = None) -> dict[str, Any]:
    cuerpo: dict[str, Any] = {
        "clienteId": "00000000-0000-4000-8000-000000000001",
        "productoId": "vida-hipotecario",
        "datosAsegurado": {
            "fechaNacimiento": "1990-04-18",
            "plazoCreditoMeses": 180,
            "sumaAsegurada": 180000000,
        },
    }
    if perfil_id:
        cuerpo["perfilId"] = perfil_id
    respuesta = api.post(
        "/v1/cotizaciones", json=cuerpo, headers={**ASESOR, "Idempotency-Key": str(uuid4())}
    )
    assert respuesta.status_code == 201
    creada: dict[str, Any] = respuesta.json()
    return creada


def test_ruta_de_insumos_con_perfil(api: TestClient, perfil_c01: Any) -> None:
    creada = _cotizar(api, str(perfil_c01.id))
    respuesta = api.get(f"/v1/cotizaciones/{creada['id']}/insumos-suscripcion", headers=ASESOR)
    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["edad"] == 36 and "fechaNacimiento" not in str(cuerpo)
    assert (cuerpo["sumaAsegurada"], cuerpo["plazoCreditoMeses"]) == ("180000000", 180)
    assert (cuerpo["estadoIdentidad"], cuerpo["vigenteHasta"]) == (
        "VERIFICADO",
        "2026-10-19T15:00:00Z",
    )
    assert (cuerpo["ofertaId"], cuerpo["codigo"], cuerpo["socioId"]) == (None, None, None)
    assert cuerpo["primaTotal"] == creada["primaTotal"]
    assert cuerpo["perfil"] == {
        "id": str(perfil_c01.id),
        "estado": perfil_c01.resultado.estado.value,
        "nivelRiesgo": perfil_c01.resultado.nivel.value,
        "confianza": str(perfil_c01.resultado.confianza),
        "fuentesConsultadas": 4,
        "fuentesDeclaradas": 4,
    }
    assert cuerpo["coincidenciasListasRestrictivas"] == 0


def test_ruta_de_insumos_errores(api: TestClient, escenario_cotizacion: Any) -> None:
    creada = _cotizar(api)
    ruta = f"/v1/cotizaciones/{creada['id']}/insumos-suscripcion"
    sin_perfil = api.get(ruta, headers=ASESOR).json()
    assert (sin_perfil["perfil"], sin_perfil["coincidenciasListasRestrictivas"]) == (None, None)
    socio = api.get(ruta, headers={"X-Actor-Id": "SOC-000001", "X-Actor-Rol": "SOCIO"})
    assert (socio.status_code, socio.json()["codigo"]) == (403, "ROL_NO_AUTORIZADO")
    assert api.get(ruta).json()["codigo"] == "ACTOR_REQUERIDO"
    inexistente = api.get(f"/v1/cotizaciones/{uuid4()}/insumos-suscripcion", headers=ASESOR)
    assert (inexistente.status_code, inexistente.json()["codigo"]) == (
        404,
        "COTIZACION_NO_ENCONTRADA",
    )
    escenario_cotizacion.identidad.respuesta = DependenciaNoDisponible("clientes")
    caida = api.get(ruta, headers=ASESOR)
    assert (caida.status_code, caida.json()["detalles"]) == (
        503,
        {"dependencia": "clientes.identidad"},
    )


def test_sin_puertos_responde_503() -> None:
    with TestClient(crear_app(Settings())) as api:
        respuesta = api.get(f"/v1/cotizaciones/{uuid4()}/insumos-suscripcion", headers=ASESOR)
    assert respuesta.status_code == 503
