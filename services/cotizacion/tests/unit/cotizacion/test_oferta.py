"""La oferta de la cotización (HU-W04, N1): mapeo prima → oferta, vigencia en el instante exacto,
`asegurar_utilizable` y la oferta en la API interna."""

import dataclasses
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from cotizacion.adapters.inbound.http.cotizaciones import error_cotizacion
from cotizacion.config import Settings
from cotizacion.domain.cotizacion.errores import OfertaVencida
from cotizacion.domain.cotizacion.modelos import (
    CoberturaOfertada,
    CondicionesOferta,
    EstadoOferta,
    Exclusion,
)
from cotizacion.domain.cotizacion.oferta import (
    VERSION_OFERTA,
    crear_oferta,
    fin_de_vigencia,
    vigente_hasta_de,
)
from cotizacion.main import Puertos, crear_app
from fastapi.testclient import TestClient

pytestmark = pytest.mark.anyio
MICRO = timedelta(microseconds=1)
ASESOR = {"X-Actor-Id": "asesor-sintetico", "X-Actor-Rol": "ASESOR"}
C01 = {
    "clienteId": "00000000-0000-4000-8000-000000000001",
    "productoId": "vida-hipotecario",
    "datosAsegurado": {
        "fechaNacimiento": "1990-04-18",
        "plazoCreditoMeses": 180,
        "sumaAsegurada": 180000000,
    },
    "coberturasOpcionales": ["ENFERMEDADES_GRAVES"],
}


@pytest.fixture
def con_reglas_de_oferta(escenario_cotizacion: Any, semilla_w03: Any) -> Any:
    """El escenario con las reglas 2026.10.1 (con condiciones de oferta) vigentes."""
    _, reglas_semilla = semilla_w03
    escenario_cotizacion.productos.reglas["vida-hipotecario"] = reglas_semilla(
        indice=-1, vigente_desde=escenario_cotizacion.reloj.ahora()
    )
    return escenario_cotizacion


async def _calcular(escenario: Any, nueva_cotizacion: Any, **cambios: Any) -> Any:
    calculada, _, _ = await escenario.caso().ejecutar(nueva_cotizacion(**cambios))
    return calculada


# --- Mapeo prima → oferta ----------------------------------------------------------------


async def test_la_oferta_se_crea_con_la_prima_y_las_condiciones_de_las_reglas(
    con_reglas_de_oferta: Any, nueva_cotizacion: Any
) -> None:
    cotizacion = await _calcular(
        con_reglas_de_oferta, nueva_cotizacion, coberturas_opcionales=("ENFERMEDADES_GRAVES",)
    )
    oferta = cotizacion.oferta
    assert oferta.cotizacion_id == cotizacion.id
    assert oferta.precio == cotizacion.desglose.prima_total == Decimal(97460)
    assert (oferta.moneda, oferta.periodicidad) == ("COP", "MENSUAL")
    assert (oferta.version, oferta.version_reglas) == (VERSION_OFERTA, "2026.10.1")
    assert oferta.creada_en == cotizacion.calculado_en
    # 7 días desde el cálculo (vigenciaMinutos = 10080 en las reglas).
    assert oferta.vigente_hasta == cotizacion.calculado_en + timedelta(days=7)
    # Las coberturas incluidas, con la parte de la suma de cada una (sin entrada, toda la suma).
    assert oferta.coberturas == (
        CoberturaOfertada("MUERTE", "Muerte por cualquier causa", Decimal(180000000)),
        CoberturaOfertada(
            "INCAPACIDAD_TOTAL_PERMANENTE", "Incapacidad total y permanente", Decimal(180000000)
        ),
        CoberturaOfertada("ENFERMEDADES_GRAVES", "Enfermedades graves", Decimal(54000000)),
    )
    assert [e.codigo for e in oferta.exclusiones] == [
        "SUICIDIO_PRIMER_ANIO",
        "PREEXISTENCIAS_NO_DECLARADAS",
        "GUERRA_Y_TERRORISMO",
        "ACTIVIDADES_ALTO_RIESGO",
    ]
    # El repositorio asignó el código legible.
    assert oferta.codigo == "COT-2026-00001"


async def test_sin_condiciones_en_las_reglas_usa_la_vigencia_de_la_configuracion(
    escenario_cotizacion: Any, nueva_cotizacion: Any
) -> None:
    """Reglas 2026.09.1 (anteriores a HU-W04): 96 h de HU-W13, sin exclusiones y suma completa."""
    cotizacion = await _calcular(escenario_cotizacion, nueva_cotizacion)
    oferta = cotizacion.oferta
    assert oferta.version_reglas == "2026.09.1"
    assert oferta.vigente_hasta == cotizacion.calculado_en + timedelta(hours=96)
    assert oferta.exclusiones == ()
    assert {c.suma_asegurada for c in oferta.coberturas} == {Decimal(180000000)}


async def test_la_suma_de_cada_cobertura_se_redondea_como_la_prima(
    escenario_cotizacion: Any, nueva_cotizacion: Any, semilla_w03: Any
) -> None:
    _, reglas_semilla = semilla_w03
    reglas = reglas_semilla(
        redondeo_escala=-3,
        oferta=CondicionesOferta(60, (), {"MUERTE": Decimal("0.3333")}),
    )
    cotizacion = await _calcular(escenario_cotizacion, nueva_cotizacion)
    oferta = crear_oferta(cotizacion, reglas, uuid4(), 96)
    # 180.000.000 × 0,3333 = 59.994.000 → a miles (escala −3): 59.994.000.
    assert oferta.coberturas[0].suma_asegurada == Decimal("5.9994E+7")
    assert oferta.vigente_hasta == cotizacion.calculado_en + timedelta(minutes=60)


async def test_la_misma_clave_devuelve_la_misma_oferta(
    con_reglas_de_oferta: Any, nueva_cotizacion: Any
) -> None:
    caso = con_reglas_de_oferta.caso()
    primera, _, creada = await caso.ejecutar(nueva_cotizacion())
    repetida, _, otra_vez = await caso.ejecutar(nueva_cotizacion())
    assert (creada, otra_vez) == (True, False)
    assert repetida.oferta == primera.oferta


# --- Vigencia y asegurar_utilizable --------------------------------------------------------


async def test_vigencia_en_el_instante_exacto(
    con_reglas_de_oferta: Any, nueva_cotizacion: Any
) -> None:
    oferta = (await _calcular(con_reglas_de_oferta, nueva_cotizacion)).oferta
    hasta = oferta.vigente_hasta
    assert oferta.estado(hasta - MICRO) is EstadoOferta.VIGENTE
    assert oferta.estado(hasta) is EstadoOferta.VENCIDA
    assert oferta.estado(hasta + MICRO) is EstadoOferta.VENCIDA
    assert oferta.estado(oferta.creada_en) is EstadoOferta.VIGENTE


async def test_asegurar_utilizable(con_reglas_de_oferta: Any, nueva_cotizacion: Any) -> None:
    oferta = (await _calcular(con_reglas_de_oferta, nueva_cotizacion)).oferta
    oferta.asegurar_utilizable(oferta.vigente_hasta - MICRO)
    for ahora in (oferta.vigente_hasta, oferta.vigente_hasta + timedelta(days=30)):
        with pytest.raises(OfertaVencida) as vencida:
            oferta.asegurar_utilizable(ahora)
        assert (vencida.value.oferta_id, vencida.value.vigente_hasta) == (
            oferta.id,
            oferta.vigente_hasta,
        )


def test_oferta_vencida_es_409_con_la_accion_de_w13() -> None:
    error = error_cotizacion(OfertaVencida(uuid4(), datetime(2026, 10, 1, tzinfo=UTC)))
    assert (error.codigo, error.estado_http, error.detalles) == (
        "OFERTA_VENCIDA",
        409,
        {"accion": "NUEVA_COTIZACION"},
    )


async def test_fin_de_vigencia_y_vigente_hasta_de(
    escenario_cotizacion: Any, nueva_cotizacion: Any, semilla_w03: Any
) -> None:
    _, reglas_semilla = semilla_w03
    calculado = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
    sin_condiciones = reglas_semilla()
    con_condiciones = dataclasses.replace(
        sin_condiciones, oferta=CondicionesOferta(1, (Exclusion("X", "x"),), {})
    )
    assert fin_de_vigencia(calculado, sin_condiciones, 0) == calculado
    assert fin_de_vigencia(calculado, con_condiciones, 0) == calculado + timedelta(minutes=1)
    cotizacion = await _calcular(escenario_cotizacion, nueva_cotizacion)
    assert vigente_hasta_de(cotizacion, 1) == cotizacion.oferta.vigente_hasta
    anterior = dataclasses.replace(cotizacion, oferta=None)
    assert vigente_hasta_de(anterior, 1) == cotizacion.calculado_en + timedelta(hours=1)


# --- API interna -------------------------------------------------------------------------


@pytest.fixture
def api(escenario: Any, con_reglas_de_oferta: Any) -> Iterator[TestClient]:
    puertos = Puertos(
        identidad=con_reglas_de_oferta.identidad,
        consentimiento=escenario.consentimiento,
        modelos=escenario.modelos,
        ejecutor=escenario.ejecutor,
        perfiles=con_reglas_de_oferta.perfiles,
        reloj=con_reglas_de_oferta.reloj,
        productos=con_reglas_de_oferta.productos,
        cotizaciones=con_reglas_de_oferta.repo,
    )
    with TestClient(crear_app(Settings(), puertos=puertos)) as cliente:
        yield cliente


def _cotizar(api: TestClient, **encabezados: str) -> dict[str, Any]:
    respuesta = api.post(
        "/v1/cotizaciones",
        json=C01,
        headers={**ASESOR, "Idempotency-Key": str(uuid4()), **encabezados},
    )
    assert respuesta.status_code == 201, respuesta.text
    creada: dict[str, Any] = respuesta.json()
    return creada


def test_la_respuesta_trae_la_oferta_vigente(api: TestClient) -> None:
    creada = _cotizar(api)
    oferta = creada["oferta"]
    assert set(oferta) == {
        "id",
        "codigo",
        "cotizacionId",
        "precio",
        "moneda",
        "periodicidad",
        "coberturas",
        "exclusionesResumidas",
        "version",
        "versionReglas",
        "creadaEn",
        "vigenteHasta",
        "estado",
    }
    assert (oferta["cotizacionId"], oferta["precio"], oferta["estado"]) == (
        creada["id"],
        creada["primaTotal"],
        "VIGENTE",
    )
    assert oferta["coberturas"][-1] == {
        "codigo": "ENFERMEDADES_GRAVES",
        "nombre": "Enfermedades graves",
        "sumaAsegurada": "54000000",
    }
    assert oferta["vigenteHasta"] == creada["vigenteHasta"] == "2026-10-22T15:00:00Z"
    assert oferta["codigo"] == "COT-2026-00001"


def test_al_leer_despues_del_vencimiento_la_oferta_esta_vencida(
    api: TestClient, con_reglas_de_oferta: Any
) -> None:
    """El portal (C08) la sigue leyendo: 200 con estado VENCIDA; el 410 es del canal del socio."""
    creada = _cotizar(api)
    ruta = f"/v1/cotizaciones/{creada['id']}"
    hasta = datetime.fromisoformat(creada["oferta"]["vigenteHasta"])
    con_reglas_de_oferta.reloj.instante = hasta - MICRO
    assert api.get(ruta, headers=ASESOR).json()["oferta"]["estado"] == "VIGENTE"
    con_reglas_de_oferta.reloj.instante = hasta
    leida = api.get(ruta, headers=ASESOR)
    assert leida.status_code == 200
    assert leida.json()["oferta"] == {**creada["oferta"], "estado": "VENCIDA"}


def test_una_cotizacion_anterior_no_trae_oferta(api: TestClient, con_reglas_de_oferta: Any) -> None:
    creada = _cotizar(api)
    repo = con_reglas_de_oferta.repo
    cotizacion_id = UUID(creada["id"])
    repo.cotizaciones[cotizacion_id] = dataclasses.replace(
        repo.cotizaciones[cotizacion_id], oferta=None
    )
    leida = api.get(f"/v1/cotizaciones/{creada['id']}", headers=ASESOR).json()
    assert leida["oferta"] is None
    assert leida["vigenteHasta"] == "2026-10-19T15:00:00Z"  # 96 h de HU-W13


def test_el_socio_no_lee_la_oferta_de_otro(api: TestClient) -> None:
    socio = {"X-Actor-Id": "SOC-000001", "X-Actor-Rol": "SOCIO"}
    creada = _cotizar(api, **socio)
    assert creada["socioId"] == "SOC-000001" and creada["oferta"]["estado"] == "VIGENTE"
    otro = {"X-Actor-Id": "SOC-000002", "X-Actor-Rol": "SOCIO"}
    ajena = api.get(f"/v1/cotizaciones/{creada['id']}", headers=otro)
    assert (ajena.status_code, ajena.json()["codigo"]) == (404, "COTIZACION_NO_ENCONTRADA")
