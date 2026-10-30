"""Casos de uso, DTOs y adaptador HTTP de pólizas (HU-M10)."""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx
import pytest
from bff_movil.adapters.inbound.http.polizas import PolizaDetalleDto, PolizaResumenDto
from bff_movil.adapters.outbound.polizas_http import PolizasHttp
from bff_movil.application.polizas.casos import ConsultarPoliza, ListarPolizas
from bff_movil.domain.polizas.modelos import (
    Cobertura,
    Poliza,
    PolizaNoAutorizada,
    PolizaNoEncontrada,
    Vigencia,
)
from bff_movil.domain.sesion.errores import ClienteNoEncontrado, ServicioNoDisponible

SUB = "sub-sintetico"
CLIENTE_ID = UUID("00000000-0000-4000-8000-000000000001")
POLIZA_ID = UUID("00000000-0000-4000-8000-0000000000b1")
BASE = "http://consulta.test"


def poliza(**cambios: Any) -> Poliza:
    datos: dict[str, Any] = {
        "poliza_id": POLIZA_ID,
        "numero": "POL-2026-000001",
        "estado": "EMITIDA",
        "producto_nombre": "Vida Protegida",
        "ramo": "VIDA",
        "vigencia": Vigencia(date(2026, 1, 1), date(2026, 12, 31)),
        "vigente": True,
        "prima": Decimal("120000.00"),
        "suma_asegurada": Decimal("50000000.00"),
        "moneda": "COP",
        "periodicidad": "MENSUAL",
        "coberturas": (
            Cobertura("MUERTE", "Muerte", True),
            Cobertura("INCAP", "Incapacidad", False),
        ),
        "documento_huella": "sha256:abc",
        "firma_id": "FIRMA-1",
        "emitida_en": datetime(2026, 1, 1, 8, 0, tzinfo=UTC),
    }
    return Poliza(**{**datos, **cambios})


class Resolutor:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.subs: list[str] = []

    async def resolver(self, sub: str) -> UUID:
        self.subs.append(sub)
        if self.error:
            raise self.error
        return CLIENTE_ID


class Polizas:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.llamadas: list[tuple[UUID, str]] = []

    async def listar(self, cliente_id: UUID, actor_id: str) -> list[Poliza]:
        self.llamadas.append((cliente_id, actor_id))
        if self.error:
            raise self.error
        return [poliza()]

    async def obtener(self, cliente_id: UUID, poliza_id: UUID, actor_id: str) -> Poliza:
        self.llamadas.append((cliente_id, actor_id))
        if self.error:
            raise self.error
        return poliza(poliza_id=poliza_id)


@pytest.mark.anyio
async def test_listar_resuelve_el_cliente_desde_el_sub() -> None:
    resolutor, polizas = Resolutor(), Polizas()

    resultado = await ListarPolizas(resolutor, polizas).ejecutar(SUB)

    assert resultado == [poliza()]
    assert resolutor.subs == [SUB]
    assert polizas.llamadas == [(CLIENTE_ID, SUB)]


@pytest.mark.anyio
async def test_consultar_resuelve_el_cliente_desde_el_sub() -> None:
    resolutor, polizas = Resolutor(), Polizas()

    resultado = await ConsultarPoliza(resolutor, polizas).ejecutar(SUB, POLIZA_ID)

    assert resultado.poliza_id == POLIZA_ID
    assert polizas.llamadas == [(CLIENTE_ID, SUB)]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "error", [ClienteNoEncontrado(), ServicioNoDisponible("ERROR")], ids=["cliente", "caida"]
)
async def test_sin_cliente_no_consulta_polizas(error: Exception) -> None:
    polizas = Polizas()

    with pytest.raises(type(error)):
        await ListarPolizas(Resolutor(error), polizas).ejecutar(SUB)
    with pytest.raises(type(error)):
        await ConsultarPoliza(Resolutor(error), polizas).ejecutar(SUB, POLIZA_ID)

    assert polizas.llamadas == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "error", [PolizaNoAutorizada(), PolizaNoEncontrada(), ServicioNoDisponible("TIEMPO_AGOTADO")]
)
async def test_consultar_propaga_los_errores(error: Exception) -> None:
    with pytest.raises(type(error)):
        await ConsultarPoliza(Resolutor(), Polizas(error)).ejecutar(SUB, POLIZA_ID)


def test_dto_de_lista_resume_la_cobertura() -> None:
    cuerpo = PolizaResumenDto.desde(poliza()).model_dump(by_alias=True, mode="json")

    assert cuerpo == {
        "polizaId": str(POLIZA_ID),
        "numero": "POL-2026-000001",
        "estado": "EMITIDA",
        "producto": "Vida Protegida",
        "ramo": "VIDA",
        "vigencia": {"desde": "2026-01-01", "hasta": "2026-12-31"},
        "vigente": True,
        "resumenCobertura": {
            "sumaAsegurada": "50000000.00",
            "moneda": "COP",
            "totalCoberturas": 2,
        },
    }


def test_dto_de_detalle_incluye_credencial() -> None:
    cuerpo = PolizaDetalleDto.desde(poliza()).model_dump(by_alias=True, mode="json")

    assert cuerpo["credencial"] == {
        "numero": "POL-2026-000001",
        "huella": "sha256:abc",
        "firmaId": "FIRMA-1",
        "vigencia": {"desde": "2026-01-01", "hasta": "2026-12-31"},
    }
    assert cuerpo["coberturas"][0] == {"codigo": "MUERTE", "nombre": "Muerte", "obligatoria": True}
    assert set(cuerpo) == {
        "numero",
        "estado",
        "vigencia",
        "vigente",
        "prima",
        "sumaAsegurada",
        "moneda",
        "periodicidad",
        "emitidaEn",
        "coberturas",
        "credencial",
    }


def _json_poliza() -> dict[str, Any]:
    return {
        "polizaId": str(POLIZA_ID),
        "numero": "POL-2026-000001",
        "estado": "EMITIDA",
        "productoId": "VIDA-1",
        "productoVersion": "1",
        "productoNombre": "Vida Protegida",
        "ramo": "VIDA",
        "vigencia": {"desde": "2026-01-01", "hasta": "2026-12-31"},
        "vigente": True,
        "prima": "120000.00",
        "sumaAsegurada": "50000000.00",
        "moneda": "COP",
        "periodicidad": "MENSUAL",
        "coberturas": [{"codigo": "MUERTE", "nombre": "Muerte", "obligatoria": True}],
        "documentoHuella": "sha256:abc",
        "firmaId": "FIRMA-1",
        "emitidaEn": "2026-01-01T08:00:00Z",
    }


def _adaptador(respuesta: httpx.Response | Exception) -> tuple[PolizasHttp, list[httpx.Request]]:
    vistas: list[httpx.Request] = []

    def atender(request: httpx.Request) -> httpx.Response:
        vistas.append(request)
        if isinstance(respuesta, Exception):
            raise respuesta
        return respuesta

    cliente = httpx.AsyncClient(transport=httpx.MockTransport(atender))
    return PolizasHttp(cliente, BASE, 500), vistas


@pytest.mark.anyio
async def test_adaptador_lista_con_actor_cliente() -> None:
    adaptador, vistas = _adaptador(httpx.Response(200, json=[_json_poliza()]))

    resultado = await adaptador.listar(CLIENTE_ID, SUB)

    assert resultado[0].numero == "POL-2026-000001"
    assert str(vistas[0].url) == f"{BASE}/v1/clientes/{CLIENTE_ID}/polizas"
    assert vistas[0].headers["X-Actor-Id"] == SUB
    assert vistas[0].headers["X-Actor-Rol"] == "CLIENTE"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("estado", "codigo", "esperado"),
    [
        (404, "POLIZA_NO_ENCONTRADA", PolizaNoEncontrada),
        (403, "POLIZA_NO_AUTORIZADA", PolizaNoAutorizada),
        (403, "ROL_NO_AUTORIZADO", ServicioNoDisponible),
        (404, None, ServicioNoDisponible),
        (500, None, ServicioNoDisponible),
    ],
)
async def test_adaptador_traduce_los_errores_de_consulta(
    estado: int, codigo: str | None, esperado: type[Exception]
) -> None:
    adaptador, _ = _adaptador(httpx.Response(estado, json={"codigo": codigo} if codigo else {}))

    with pytest.raises(esperado):
        await adaptador.obtener(CLIENTE_ID, POLIZA_ID, SUB)


@pytest.mark.anyio
async def test_adaptador_falla_cerrado_ante_timeout_sin_reintentar() -> None:
    adaptador, vistas = _adaptador(httpx.ReadTimeout("lento"))

    with pytest.raises(ServicioNoDisponible) as info:
        await adaptador.listar(CLIENTE_ID, SUB)

    assert info.value.motivo == "TIEMPO_AGOTADO"
    assert len(vistas) == 1


@pytest.mark.anyio
async def test_adaptador_rechaza_cuerpo_invalido_o_de_otra_poliza() -> None:
    adaptador, _ = _adaptador(httpx.Response(200, json={"basura": 1}))
    with pytest.raises(ServicioNoDisponible):
        await adaptador.obtener(CLIENTE_ID, POLIZA_ID, SUB)

    otra = {**_json_poliza(), "polizaId": "00000000-0000-4000-8000-0000000000b2"}
    adaptador, _ = _adaptador(httpx.Response(200, json=otra))
    with pytest.raises(ServicioNoDisponible):
        await adaptador.obtener(CLIENTE_ID, POLIZA_ID, SUB)
