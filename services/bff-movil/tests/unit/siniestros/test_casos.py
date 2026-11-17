import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx
import pytest
from bff_movil.adapters.inbound.http.siniestros import SolicitudBorrador
from bff_movil.adapters.outbound.siniestros_http import SiniestrosHttp
from bff_movil.application.siniestros.casos import CrearBorrador
from bff_movil.domain.polizas.modelos import Cobertura, Poliza, Vigencia
from bff_movil.domain.siniestros.errores import PolizaNoVigente
from pydantic import ValidationError

pytestmark = pytest.mark.anyio

SUB = "sub-cliente"
CLIENTE_ID = UUID("00000000-0000-4000-8000-000000000001")
POLIZA_ID = UUID("00000000-0000-4000-8000-000000000002")
CLAVE = UUID("00000000-0000-4000-8000-000000000003")


def poliza(**cambios: Any) -> Poliza:
    datos: dict[str, Any] = {
        "poliza_id": POLIZA_ID,
        "numero": "POL-1",
        "estado": "EMITIDA",
        "producto_nombre": "Producto",
        "ramo": "AUTO",
        "vigencia": Vigencia(date(2026, 1, 1), date(2026, 12, 31)),
        "vigente": True,
        "prima": Decimal("100.00"),
        "suma_asegurada": Decimal("10000.00"),
        "moneda": "COP",
        "periodicidad": "MENSUAL",
        "coberturas": (Cobertura("COLISION", "Colisión", True),),
        "documento_huella": "sha256:abc",
        "firma_id": "FIRMA-1",
        "emitida_en": datetime(2026, 1, 1, tzinfo=UTC),
    }
    return Poliza(**{**datos, **cambios})


class Resolutor:
    async def resolver(self, sub: str) -> UUID:
        assert sub == SUB
        return CLIENTE_ID


class Consulta:
    def __init__(self, poliza_consultada: Poliza) -> None:
        self.poliza = poliza_consultada
        self.llamadas: list[tuple[UUID, UUID, str]] = []

    async def obtener(self, cliente_id: UUID, poliza_id: UUID, actor_id: str) -> Poliza:
        self.llamadas.append((cliente_id, poliza_id, actor_id))
        return self.poliza

    async def listar(self, cliente_id: UUID, actor_id: str) -> list[Poliza]:
        raise AssertionError("no se debe listar pólizas")


class Siniestros:
    def __init__(self) -> None:
        self.llamadas: list[tuple[str, UUID, Poliza, str, UUID]] = []

    async def crear_borrador(
        self, actor_id: str, cliente_id: UUID, poliza: Poliza, descripcion: str, clave: UUID
    ) -> dict[str, Any]:
        self.llamadas.append((actor_id, cliente_id, poliza, descripcion, clave))
        return {"polizaId": str(poliza.poliza_id)}

    async def limites(self, actor_id: str) -> dict[str, Any]:
        raise AssertionError("no se deben consultar límites")

    async def listar_borradores(self, actor_id: str) -> dict[str, Any]:
        raise AssertionError("no se deben listar borradores")

    async def consultar_borrador(self, actor_id: str, borrador_id: UUID) -> dict[str, Any]:
        raise AssertionError("no se debe consultar un borrador")

    async def agregar_evidencia(
        self, actor_id: str, borrador_id: UUID, solicitud: dict[str, Any]
    ) -> dict[str, Any]:
        raise AssertionError("no se debe agregar evidencia")

    async def confirmar_evidencia(
        self, actor_id: str, borrador_id: UUID, evidencia_id: UUID
    ) -> dict[str, Any]:
        raise AssertionError("no se debe confirmar evidencia")

    async def renovar_carga(
        self, actor_id: str, borrador_id: UUID, evidencia_id: UUID
    ) -> dict[str, Any]:
        raise AssertionError("no se debe renovar la carga")


async def test_crea_borrador_con_cliente_resuelto_y_poliza_vigente() -> None:
    consulta, siniestros = Consulta(poliza()), Siniestros()
    descripcion = "El vehículo recibió un impacto en la puerta."

    resultado = await CrearBorrador(Resolutor(), consulta, siniestros).ejecutar(
        SUB, POLIZA_ID, descripcion, CLAVE
    )

    assert resultado == {"polizaId": str(POLIZA_ID)}
    assert consulta.llamadas == [(CLIENTE_ID, POLIZA_ID, SUB)]
    assert siniestros.llamadas == [(SUB, CLIENTE_ID, poliza(), descripcion, CLAVE)]


def test_descripcion_del_borrador_se_normaliza_y_tiene_maximo_de_2000_caracteres() -> None:
    texto = "  El vehículo recibió un impacto.  "
    solicitud = SolicitudBorrador(polizaId=POLIZA_ID, descripcion=texto)
    assert solicitud.descripcion == texto.strip()
    assert len(SolicitudBorrador(polizaId=POLIZA_ID, descripcion="x" * 2000).descripcion) == 2000

    with pytest.raises(ValidationError):
        SolicitudBorrador(polizaId=POLIZA_ID, descripcion=" " * 4)
    with pytest.raises(ValidationError):
        SolicitudBorrador(polizaId=POLIZA_ID, descripcion="x" * 2001)


async def test_reenvia_la_descripcion_al_servicio_siniestros() -> None:
    recibidas: list[httpx.Request] = []

    def manejar(request: httpx.Request) -> httpx.Response:
        recibidas.append(request)
        return httpx.Response(
            201,
            json={
                "borradorId": str(CLAVE),
                "polizaId": str(POLIZA_ID),
                "estado": "ABIERTO",
            },
        )

    descripcion = "El vehículo recibió un impacto."
    async with httpx.AsyncClient(transport=httpx.MockTransport(manejar)) as cliente:
        await SiniestrosHttp(cliente, "http://siniestros.test", timeout_ms=500).crear_borrador(
            SUB, CLIENTE_ID, poliza(), descripcion, CLAVE
        )

    assert json.loads(recibidas[0].content)["descripcion"] == descripcion


async def test_consulta_borradores_confirma_y_renueva_por_el_actor() -> None:
    recibidas: list[httpx.Request] = []

    def manejar(request: httpx.Request) -> httpx.Response:
        recibidas.append(request)
        return httpx.Response(200, json={"items": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(manejar)) as cliente:
        api = SiniestrosHttp(cliente, "http://siniestros.test", timeout_ms=500)
        await api.listar_borradores(SUB)
        await api.consultar_borrador(SUB, CLAVE)
        await api.confirmar_evidencia(SUB, CLAVE, POLIZA_ID)
        await api.renovar_carga(SUB, CLAVE, POLIZA_ID)

    assert [(request.method, request.url.path) for request in recibidas] == [
        ("GET", "/v1/siniestros/borradores"),
        ("GET", f"/v1/siniestros/borradores/{CLAVE}"),
        ("POST", f"/v1/siniestros/borradores/{CLAVE}/evidencias/{POLIZA_ID}/confirmacion"),
        ("POST", f"/v1/siniestros/borradores/{CLAVE}/evidencias/{POLIZA_ID}/carga"),
    ]
    assert all(request.headers["X-Actor-Id"] == SUB for request in recibidas)
    assert all(request.headers["X-Actor-Rol"] == "CLIENTE" for request in recibidas)


@pytest.mark.parametrize(
    "datos",
    [
        {"estado": "CANCELADA", "vigente": False},
        {"estado": "EMITIDA", "vigente": False},
    ],
)
async def test_no_crea_borrador_para_poliza_no_vigente(datos: dict[str, Any]) -> None:
    consulta, siniestros = Consulta(poliza(**datos)), Siniestros()

    with pytest.raises(PolizaNoVigente):
        await CrearBorrador(Resolutor(), consulta, siniestros).ejecutar(
            SUB, POLIZA_ID, "Descripción del siniestro", CLAVE
        )

    assert consulta.llamadas == [(CLIENTE_ID, POLIZA_ID, SUB)]
    assert siniestros.llamadas == []
