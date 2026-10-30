"""Casos de uso de lectura de pólizas (HU-M10): propia, ajena, inexistente, vencidas y orden."""

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from consulta.application.lectura.casos import ConsultarPoliza, ListarPolizas
from consulta.application.lectura.puertos import PolizaNoAutorizada, PolizaNoEncontrada
from consulta.domain.polizas.modelos import CoberturaProyectada, PolizaProyectada, Vigencia

pytestmark = pytest.mark.anyio
AHORA = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


class RelojFijo:
    def ahora(self) -> datetime:
        return AHORA


def poliza(cliente_id: UUID, desde: date, hasta: date) -> PolizaProyectada:
    return PolizaProyectada(
        poliza_id=uuid4(),
        cliente_id=cliente_id,
        numero=f"100-2026-{uuid4().hex[:6]}",
        estado="ACTIVA",
        producto_id="vida-deudor",
        producto_version="1.0.0",
        producto_nombre="Vida deudor",
        ramo="VIDA",
        vigencia=Vigencia(desde, hasta),
        prima=Decimal("64638.10"),
        suma_asegurada=Decimal(50000000),
        moneda="COP",
        periodicidad="MENSUAL",
        coberturas=(CoberturaProyectada("MUERTE", "Muerte", True),),
        documento_huella="sha256:" + "a" * 64,
        firma_id="FIRMA-0001",
        emitida_en=AHORA,
        proyectada_en=AHORA,
    )


class LecturaEnMemoria:
    def __init__(self, *polizas: PolizaProyectada) -> None:
        self.polizas = list(polizas)

    async def por_id(self, poliza_id: UUID) -> PolizaProyectada | None:
        return next((p for p in self.polizas if p.poliza_id == poliza_id), None)

    async def por_cliente(self, cliente_id: UUID) -> list[PolizaProyectada]:
        propias = [p for p in self.polizas if p.cliente_id == cliente_id]
        return sorted(propias, key=lambda p: p.vigencia.hasta, reverse=True)


async def test_la_lista_trae_solo_las_del_cliente_con_vigente_y_orden() -> None:
    cliente = uuid4()
    vencida = poliza(cliente, date(2025, 1, 1), date(2025, 12, 31))
    vigente = poliza(cliente, date(2026, 10, 25), date(2027, 10, 24))
    ajena = poliza(uuid4(), date(2026, 1, 1), date(2030, 1, 1))
    caso = ListarPolizas(LecturaEnMemoria(vencida, ajena, vigente), RelojFijo())

    resultado = await caso.ejecutar(cliente)

    assert [(r.poliza.poliza_id, r.vigente) for r in resultado] == [
        (vigente.poliza_id, True),
        (vencida.poliza_id, False),
    ]


async def test_sin_polizas_la_lista_es_vacia() -> None:
    assert await ListarPolizas(LecturaEnMemoria(), RelojFijo()).ejecutar(uuid4()) == []


async def test_el_ultimo_dia_de_vigencia_sigue_vigente_en_fecha_de_bogota() -> None:
    cliente = uuid4()

    # 25-oct 02:00 UTC es 24-oct 21:00 en Bogotá: el último día aún rige.
    class Reloj:
        def ahora(self) -> datetime:
            return datetime(2026, 10, 25, 2, 0, tzinfo=UTC)

    p = poliza(cliente, date(2025, 10, 25), date(2026, 10, 24))
    (r,) = await ListarPolizas(LecturaEnMemoria(p), Reloj()).ejecutar(cliente)
    assert r.vigente is True


async def test_el_detalle_de_la_propia_devuelve_la_poliza() -> None:
    cliente = uuid4()
    p = poliza(cliente, date(2026, 10, 25), date(2027, 10, 24))
    r = await ConsultarPoliza(LecturaEnMemoria(p), RelojFijo()).ejecutar(cliente, p.poliza_id)
    assert r.poliza == p
    assert r.vigente is True


async def test_el_detalle_de_una_ajena_es_no_autorizado() -> None:
    p = poliza(uuid4(), date(2026, 10, 25), date(2027, 10, 24))
    with pytest.raises(PolizaNoAutorizada):
        await ConsultarPoliza(LecturaEnMemoria(p), RelojFijo()).ejecutar(uuid4(), p.poliza_id)


async def test_el_detalle_de_una_inexistente_es_no_encontrado() -> None:
    with pytest.raises(PolizaNoEncontrada):
        await ConsultarPoliza(LecturaEnMemoria(), RelojFijo()).ejecutar(uuid4(), uuid4())
