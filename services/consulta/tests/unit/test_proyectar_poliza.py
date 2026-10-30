from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from consulta.application.proyeccion.comandos import PolizaEmitida
from consulta.application.proyeccion.proyectar_poliza import ProyectarPoliza
from consulta.application.proyeccion.puertos import (
    DependenciaNoDisponible,
    ProductoCatalogo,
    VersionProductoDistinta,
)
from consulta.domain.polizas.modelos import CoberturaProyectada, PolizaProyectada

AHORA = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
pytestmark = pytest.mark.anyio
COBERTURAS = (
    CoberturaProyectada("MUERTE", "Muerte por cualquier causa", True),
    CoberturaProyectada("INCAPACIDAD", "Incapacidad total", False),
)


class CatalogoFalso:
    def __init__(self, version: str = "1.0.0", error: Exception | None = None) -> None:
        self._version = version
        self._error = error

    async def producto(self, producto_id: str) -> ProductoCatalogo:
        if self._error is not None:
            raise self._error
        return ProductoCatalogo(producto_id, self._version, "Vida deudor", "VIDA", COBERTURAS)


class ProyeccionFalsa:
    def __init__(self) -> None:
        self.polizas: dict[Any, PolizaProyectada] = {}

    async def insertar(self, poliza: PolizaProyectada, tx: object) -> bool:
        if poliza.poliza_id in self.polizas:
            return False
        self.polizas[poliza.poliza_id] = poliza
        return True


class RelojFijo:
    def ahora(self) -> datetime:
        return AHORA


def _evento(**cambios: Any) -> PolizaEmitida:
    datos: dict[str, Any] = {
        "poliza_id": uuid4(),
        "cliente_id": uuid4(),
        "numero": "100-2026-000001",
        "producto_id": "vida-deudor",
        "producto_version": "1.0.0",
        "vigencia_desde": date(2026, 10, 25),
        "vigencia_hasta": date(2027, 10, 24),
        "prima": "64638.10",
        "suma_asegurada": "50000000",
        "moneda": "COP",
        "periodicidad": "MENSUAL",
        "documento_huella": "sha256:" + "a" * 64,
        "firma_id": "FIRMA-0001",
        "emitida_en": datetime(2026, 10, 24, 15, 0, tzinfo=UTC),
    }
    return PolizaEmitida(**(datos | cambios))


def _caso(catalogo: CatalogoFalso, proyeccion: ProyeccionFalsa) -> ProyectarPoliza[object]:
    return ProyectarPoliza(catalogo, proyeccion, RelojFijo())


async def test_mapea_el_evento_y_copia_ramo_y_coberturas_del_catalogo() -> None:
    proyeccion = ProyeccionFalsa()
    evento = _evento()

    await _caso(CatalogoFalso(), proyeccion).ejecutar(evento, object())

    poliza = proyeccion.polizas[evento.poliza_id]
    assert poliza.cliente_id == evento.cliente_id
    assert poliza.estado == "ACTIVA"
    assert (poliza.producto_nombre, poliza.ramo) == ("Vida deudor", "VIDA")
    assert poliza.coberturas == COBERTURAS
    assert (poliza.prima, poliza.suma_asegurada) == (Decimal("64638.10"), Decimal(50000000))
    assert (poliza.vigencia.desde, poliza.vigencia.hasta) == (
        evento.vigencia_desde,
        evento.vigencia_hasta,
    )
    # `emitida_en` es la fecha del documento; `proyectada_en`, la del reloj.
    assert poliza.emitida_en == evento.emitida_en
    assert poliza.proyectada_en == AHORA


async def test_un_duplicado_no_modifica_la_poliza() -> None:
    proyeccion = ProyeccionFalsa()
    evento = _evento()
    await _caso(CatalogoFalso(), proyeccion).ejecutar(evento, object())
    primera = proyeccion.polizas[evento.poliza_id]

    await _caso(CatalogoFalso(), proyeccion).ejecutar(
        _evento(poliza_id=evento.poliza_id, prima="1"), object()
    )

    assert proyeccion.polizas == {evento.poliza_id: primera}


async def test_version_distinta_a_la_vigente_lanza_y_no_proyecta() -> None:
    proyeccion = ProyeccionFalsa()

    with pytest.raises(VersionProductoDistinta) as error:
        await _caso(CatalogoFalso(version="2.0.0"), proyeccion).ejecutar(_evento(), object())

    assert (error.value.esperada, error.value.vigente) == ("1.0.0", "2.0.0")
    assert proyeccion.polizas == {}


async def test_catalogo_caido_propaga_el_error_y_no_proyecta() -> None:
    proyeccion = ProyeccionFalsa()

    with pytest.raises(DependenciaNoDisponible):
        await _caso(CatalogoFalso(error=DependenciaNoDisponible("catalogo")), proyeccion).ejecutar(
            _evento(), object()
        )

    assert proyeccion.polizas == {}
