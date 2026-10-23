from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import schemathesis
from api_socios.adapters.inbound.dependencias import consultar_oferta, cotizar_oferta
from api_socios.application.ofertas import ConsultarOferta, CotizarOferta
from api_socios.domain.ofertas import (
    CoberturaOferta,
    CotizacionNoDisponible,
    CotizacionRechazada,
    EstadoOferta,
    ExclusionResumida,
    Oferta,
    OfertaNoEncontrada,
)
from api_socios.main import app
from hypothesis import HealthCheck, settings

CONTRATO = Path(__file__).parents[4] / "contracts" / "openapi" / "api-socios.yaml"
schema = schemathesis.openapi.from_path(CONTRATO)
schema.app = app


@pytest.fixture(autouse=True, scope="module")
def dobles(entorno_compartido):
    return entorno_compartido


@schema.parametrize()
@settings(max_examples=25, deadline=None, suppress_health_check=list(HealthCheck))
def test_contrato_api_socios(case: schemathesis.Case) -> None:
    # Starlette solo lista en Allow los métodos de la primera ruta que coincide.
    case.call_and_validate(excluded_checks=[schemathesis.checks.allow_header_conformance])


ALTA = {
    "nombre": "Fintech Caribe S.A.S.",
    "canal": "plataforma_digital",
    "plan": "basico",
    "permisos": ["cotizar"],
}


def _llamar(metodo: str, ruta: str, **kwargs: object):
    caso = schema[ruta][metodo].Case(**kwargs)
    respuesta = caso.call()
    caso.validate_response(respuesta)
    return respuesta


def test_codigos_de_la_historia_cumplen_el_contrato(dobles) -> None:
    alta = _llamar("POST", "/admin/socios", body=ALTA, media_type="application/json")
    assert alta.status_code == 201
    assert (
        _llamar(
            "POST", "/admin/socios", body=ALTA | {"permisos": []}, media_type="application/json"
        ).status_code
        == 422
    )
    assert (
        _llamar("GET", "/socio/me", headers={"X-Partner-Client-Id": "desconocido"}).status_code
        == 401
    )

    socio = dobles.repositorio.obtener(alta.json()["id"])  # solo con "cotizar"
    socio_headers = {
        "X-Partner-Client-Id": socio.client_id,
        "X-Partner-Key-Id": socio.api_key_id,
        "X-Partner-Scopes": "solventa/cotizar",
    }
    assert _llamar("GET", "/socio/me", headers=socio_headers).status_code == 200
    assert _llamar("POST", "/polizas", headers=socio_headers).status_code == 403


# --- HU-W04: la oferta, con cotizacion simulada --------------------------------------------

SOLICITUD = {
    "clienteId": "00000000-0000-4000-8000-000000000001",
    "productoId": "vida-hipotecario",
    "datosAsegurado": {
        "fechaNacimiento": "1990-04-18",
        "plazoCreditoMeses": 180,
        "sumaAsegurada": 180000000,
    },
    "coberturasOpcionales": ["ENFERMEDADES_GRAVES"],
}
VIGENTE = "00000000-0000-4000-d000-000000000001"
VENCIDA = "00000000-0000-4000-d000-000000000002"


def oferta_simulada(cotizacion_id: UUID, estado: EstadoOferta) -> Oferta:
    return Oferta(
        id=UUID("00000000-0000-4000-e000-000000000001"),
        codigo="COT-2026-00001",
        cotizacion_id=cotizacion_id,
        producto_id="vida-hipotecario",
        precio=Decimal(97460),
        moneda="COP",
        periodicidad="MENSUAL",
        coberturas=(CoberturaOferta("MUERTE", "Muerte", Decimal(180000000)),),
        exclusiones=(ExclusionResumida("SUICIDIO_PRIMER_ANIO", "Suicidio"),),
        version=1,
        version_reglas="2026.10.1",
        creada_en=datetime(2026, 10, 22, 15, tzinfo=UTC),
        vigente_hasta=datetime(2026, 10, 29, 15, tzinfo=UTC),
        estado=estado,
    )


class CotizacionSimulada:
    async def cotizar(self, solicitud: Any, socio_id: str, clave: UUID) -> tuple[Oferta, bool]:
        if solicitud.producto_id == "no-existe":
            raise CotizacionRechazada("PRODUCTO_NO_ENCONTRADO", 422, {})
        if solicitud.producto_id == "caido":
            raise CotizacionNoDisponible("prueba")
        return oferta_simulada(UUID(VIGENTE), EstadoOferta.VIGENTE), True

    async def oferta(self, cotizacion_id: UUID, socio_id: str) -> Oferta:
        if str(cotizacion_id) == VIGENTE:
            return oferta_simulada(cotizacion_id, EstadoOferta.VIGENTE)
        if str(cotizacion_id) == VENCIDA:
            return oferta_simulada(cotizacion_id, EstadoOferta.VENCIDA)
        raise OfertaNoEncontrada()


def test_hu_w04_codigos_de_la_oferta_cumplen_el_contrato(dobles) -> None:
    simulada = CotizacionSimulada()
    app.dependency_overrides[cotizar_oferta] = lambda: CotizarOferta(simulada)
    app.dependency_overrides[consultar_oferta] = lambda: ConsultarOferta(simulada)
    try:
        alta = _llamar("POST", "/admin/socios", body=ALTA, media_type="application/json")
        socio = dobles.repositorio.obtener(alta.json()["id"])
        headers = {
            "X-Partner-Client-Id": socio.client_id,
            "X-Partner-Key-Id": socio.api_key_id,
            "X-Partner-Scopes": "solventa/cotizar",
            "Idempotency-Key": "00000000-0000-4000-c000-000000000001",
        }

        def cotizar(producto: str) -> int:
            cuerpo = SOLICITUD | {"productoId": producto}
            caso = schema["/cotizaciones"]["POST"].Case(
                body=cuerpo, headers=headers, media_type="application/json"
            )
            respuesta = caso.call()
            # El 503 es la falla cerrada documentada: se valida su forma, no se trata como error.
            caso.validate_response(
                respuesta, excluded_checks=[schemathesis.checks.not_a_server_error]
            )
            return respuesta.status_code

        def consultar(cotizacion_id: str) -> int:
            return _llamar(
                "GET",
                "/cotizaciones/{cotizacion_id}",
                path_parameters={"cotizacion_id": cotizacion_id},
                headers=headers,
            ).status_code

        assert [cotizar("vida-hipotecario"), cotizar("no-existe"), cotizar("caido")] == [
            201,
            422,
            503,
        ]
        assert [consultar(VIGENTE), consultar(VENCIDA), consultar(str(UUID(int=9)))] == [
            200,
            410,
            404,
        ]
    finally:
        app.dependency_overrides.pop(cotizar_oferta)
        app.dependency_overrides.pop(consultar_oferta)
