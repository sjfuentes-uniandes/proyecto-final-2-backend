"""Endpoints /v1/polizas con el adaptador de `consulta` simulado (HU-M10)."""

import logging
from collections.abc import Callable
from typing import Any
from uuid import UUID

import pytest
from bff_movil.domain.polizas.modelos import Poliza, PolizaNoAutorizada, PolizaNoEncontrada
from bff_movil.domain.sesion.errores import ServicioNoDisponible

Entorno = Any
CLIENTE_ID = UUID("00000000-0000-4000-8000-000000000001")
POLIZA_ID = UUID("00000000-0000-4000-8000-0000000000b1")


def _poliza() -> Poliza:
    from datetime import UTC, date, datetime
    from decimal import Decimal

    from bff_movil.domain.polizas.modelos import Cobertura, Vigencia

    return Poliza(
        POLIZA_ID,
        "POL-2026-000001",
        "EMITIDA",
        "Vida Protegida",
        "VIDA",
        Vigencia(date(2026, 1, 1), date(2026, 12, 31)),
        True,
        Decimal("120000.00"),
        Decimal("50000000.00"),
        "COP",
        "MENSUAL",
        (Cobertura("MUERTE", "Muerte", True),),
        "sha256:huella-completa-sintetica",
        "FIRMA-1",
        datetime(2026, 1, 1, 8, 0, tzinfo=UTC),
    )


class ConsultaFalsa:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.llamadas: list[tuple[UUID, str]] = []

    async def listar(self, cliente_id: UUID, actor_id: str) -> list[Poliza]:
        self.llamadas.append((cliente_id, actor_id))
        if self.error:
            raise self.error
        return [_poliza()]

    async def obtener(self, cliente_id: UUID, poliza_id: UUID, actor_id: str) -> Poliza:
        self.llamadas.append((cliente_id, actor_id))
        if self.error:
            raise self.error
        return _poliza()


def test_lista_las_polizas_del_titular(crear_entorno: Callable[..., Entorno]) -> None:
    consulta = ConsultaFalsa()
    entorno = crear_entorno(consulta_polizas=consulta)

    respuesta = entorno.pedir("GET", "/v1/polizas")

    assert respuesta.status_code == 200
    assert respuesta.json()[0]["resumenCobertura"] == {
        "sumaAsegurada": "50000000.00",
        "moneda": "COP",
        "totalCoberturas": 1,
    }
    assert consulta.llamadas == [(CLIENTE_ID, entorno.sub)]


def test_detalle_incluye_credencial(crear_entorno: Callable[..., Entorno]) -> None:
    entorno = crear_entorno(consulta_polizas=ConsultaFalsa())

    respuesta = entorno.pedir("GET", f"/v1/polizas/{POLIZA_ID}")

    assert respuesta.status_code == 200
    assert respuesta.json()["credencial"]["huella"] == "sha256:huella-completa-sintetica"
    assert respuesta.json()["producto"] == "Vida Protegida"
    assert respuesta.json()["ramo"] == "VIDA"


@pytest.mark.parametrize(
    ("error", "estado", "codigo"),
    [
        (PolizaNoAutorizada(), 403, "POLIZA_NO_AUTORIZADA"),
        (PolizaNoEncontrada(), 404, "POLIZA_NO_ENCONTRADA"),
        (ServicioNoDisponible("ERROR"), 503, "SERVICIO_NO_DISPONIBLE"),
        (ServicioNoDisponible("TIEMPO_AGOTADO"), 503, "SERVICIO_NO_DISPONIBLE"),
    ],
)
def test_detalle_traduce_los_errores(
    crear_entorno: Callable[..., Entorno], error: Exception, estado: int, codigo: str
) -> None:
    entorno = crear_entorno(consulta_polizas=ConsultaFalsa(error))

    respuesta = entorno.pedir("GET", f"/v1/polizas/{POLIZA_ID}")

    assert respuesta.status_code == estado
    assert respuesta.json()["codigo"] == codigo


def test_lista_con_caida_responde_503(crear_entorno: Callable[..., Entorno]) -> None:
    entorno = crear_entorno(consulta_polizas=ConsultaFalsa(ServicioNoDisponible("ERROR")))

    respuesta = entorno.pedir("GET", "/v1/polizas")

    assert respuesta.status_code == 503


def test_cliente_inexistente_responde_404_como_en_sesion(
    crear_entorno: Callable[..., Entorno],
) -> None:
    consulta = ConsultaFalsa()
    entorno = crear_entorno(con_cliente=False, consulta_polizas=consulta)

    for ruta in ("/v1/polizas", f"/v1/polizas/{POLIZA_ID}"):
        respuesta = entorno.pedir("GET", ruta)
        assert respuesta.status_code == 404
        assert respuesta.json()["codigo"] == "CLIENTE_NO_ENCONTRADO"
    assert consulta.llamadas == []


def test_sin_token_responde_401(crear_entorno: Callable[..., Entorno]) -> None:
    entorno = crear_entorno(consulta_polizas=ConsultaFalsa())

    respuesta = entorno.cliente.get("/v1/polizas")

    assert respuesta.status_code == 401


def test_el_identificador_debe_ser_uuid(crear_entorno: Callable[..., Entorno]) -> None:
    entorno = crear_entorno(consulta_polizas=ConsultaFalsa())

    assert entorno.pedir("GET", "/v1/polizas/no-es-uuid").status_code == 422


def test_no_registra_numero_huella_ni_cliente_id(
    crear_entorno: Callable[..., Entorno], caplog: pytest.LogCaptureFixture
) -> None:
    entorno = crear_entorno(consulta_polizas=ConsultaFalsa())
    caplog.set_level(logging.DEBUG)

    entorno.pedir("GET", "/v1/polizas")
    entorno.pedir("GET", f"/v1/polizas/{POLIZA_ID}")

    registrado = caplog.text + entorno.telemetria.texto_logs() + entorno.telemetria.texto_spans()
    for secreto in ("POL-2026-000001", "huella-completa-sintetica", str(CLIENTE_ID)):
        assert secreto not in registrado


def test_el_sub_del_token_distinto_al_encabezado_responde_401(
    crear_entorno: Callable[..., Entorno],
) -> None:
    consulta = ConsultaFalsa()
    entorno = crear_entorno(consulta_polizas=consulta)

    for ruta in ("/v1/polizas", f"/v1/polizas/{POLIZA_ID}"):
        respuesta = entorno.pedir("GET", ruta, sub="otro-titular")
        assert respuesta.status_code == 401
        assert "POL-2026-000001" not in respuesta.text
    assert consulta.llamadas == []


def test_se_ignora_el_cliente_id_enviado_por_el_movil(
    crear_entorno: Callable[..., Entorno],
) -> None:
    consulta = ConsultaFalsa()
    entorno = crear_entorno(consulta_polizas=consulta)
    ajeno = "00000000-0000-4000-8000-0000000000ff"

    entorno.pedir(
        "GET",
        "/v1/polizas",
        params={"clienteId": ajeno, "cliente_id": ajeno},
        encabezados={"X-Cliente-Id": ajeno, "X-Actor-Id": ajeno},
    )
    entorno.pedir(
        "GET",
        f"/v1/polizas/{POLIZA_ID}",
        params={"clienteId": ajeno},
        encabezados={"X-Cliente-Id": ajeno},
    )

    assert consulta.llamadas == [(CLIENTE_ID, entorno.sub)] * 2


def test_sin_cliente_la_falla_es_cerrada_y_sin_datos(
    crear_entorno: Callable[..., Entorno],
) -> None:
    entorno = crear_entorno(con_cliente=False, consulta_polizas=ConsultaFalsa())

    respuesta = entorno.pedir("GET", "/v1/polizas")

    assert respuesta.status_code == 404
    assert "POL-2026-000001" not in respuesta.text
