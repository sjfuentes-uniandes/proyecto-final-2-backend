from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from bff_movil.application.siniestros.casos import CrearBorrador
from bff_movil.domain.polizas.modelos import Cobertura, Poliza, Vigencia
from bff_movil.domain.siniestros.errores import PolizaNoVigente

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
        self.llamadas: list[tuple[str, UUID, Poliza, UUID]] = []

    async def crear_borrador(
        self, actor_id: str, cliente_id: UUID, poliza: Poliza, clave: UUID
    ) -> dict[str, Any]:
        self.llamadas.append((actor_id, cliente_id, poliza, clave))
        return {"polizaId": str(poliza.poliza_id)}

    async def limites(self, actor_id: str) -> dict[str, Any]:
        raise AssertionError("no se deben consultar límites")

    async def agregar_evidencia(
        self, actor_id: str, borrador_id: UUID, solicitud: dict[str, Any]
    ) -> dict[str, Any]:
        raise AssertionError("no se debe agregar evidencia")


async def test_crea_borrador_con_cliente_resuelto_y_poliza_vigente() -> None:
    consulta, siniestros = Consulta(poliza()), Siniestros()

    resultado = await CrearBorrador(Resolutor(), consulta, siniestros).ejecutar(
        SUB, POLIZA_ID, CLAVE
    )

    assert resultado == {"polizaId": str(POLIZA_ID)}
    assert consulta.llamadas == [(CLIENTE_ID, POLIZA_ID, SUB)]
    assert siniestros.llamadas == [(SUB, CLIENTE_ID, poliza(), CLAVE)]


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
        await CrearBorrador(Resolutor(), consulta, siniestros).ejecutar(SUB, POLIZA_ID, CLAVE)

    assert consulta.llamadas == [(CLIENTE_ID, POLIZA_ID, SUB)]
    assert siniestros.llamadas == []
