"""Bandeja e indicadores (CF-S3-05 y CF-S3-06)."""

from collections.abc import Callable
from datetime import datetime, timedelta
from uuid import UUID

import pytest
from siniestros.application.siniestro.bandeja import ConsultarResumen, ListarBandeja
from siniestros.application.siniestro.errores import SolicitudInvalida
from siniestros.application.siniestro.vistas import (
    FiltroBandeja,
    Indicadores,
    PaginaSiniestros,
    Plazos,
    TipoBandeja,
)
from siniestros.domain.siniestro.modelos import PrioridadSiniestro, Siniestro

pytestmark = pytest.mark.anyio
ANALISTA = UUID("a0a0a0a0-0000-4000-8000-000000000001")


def filtro(ahora: datetime, plazos: Plazos, **cambios: object) -> FiltroBandeja:
    return FiltroBandeja(TipoBandeja.MIA, ANALISTA, ahora, plazos, **cambios)  # type: ignore[arg-type]


async def test_deriva_tiempo_restante_y_prioridad_con_el_reloj(
    repo, reloj, plazos: Plazos, siniestro: Callable[..., Siniestro], ahora: datetime
) -> None:
    vencido = siniestro(
        reportado_en=ahora - timedelta(hours=50), fecha_evento=ahora - timedelta(hours=51)
    )
    medio = siniestro(
        reportado_en=ahora - timedelta(hours=30), fecha_evento=ahora - timedelta(hours=31)
    )
    repo.pagina = PaginaSiniestros((vencido, medio, siniestro()), total=3, siguiente_cursor="c2")
    pagina = await ListarBandeja(repo, reloj, plazos).ejecutar(filtro(ahora, plazos))
    assert [(r.tiempo_restante_min, r.prioridad) for r in pagina.items] == [
        (-120, PrioridadSiniestro.ALTA),
        (18 * 60, PrioridadSiniestro.MEDIA),
        (34 * 60, PrioridadSiniestro.BAJA),
    ]
    assert (pagina.total, pagina.siguiente_cursor) == (3, "c2")


async def test_entrega_el_filtro_tal_cual_al_repositorio(
    repo, reloj, plazos: Plazos, ahora: datetime
) -> None:
    pedido = filtro(ahora, plazos, texto="0442", limite=5)
    await ListarBandeja(repo, reloj, plazos).ejecutar(pedido)
    assert repo.filtros == [pedido]


async def test_un_cursor_invalido_se_propaga(repo, reloj, plazos: Plazos, ahora: datetime) -> None:
    with pytest.raises(SolicitudInvalida) as error:
        await ListarBandeja(repo, reloj, plazos).ejecutar(
            filtro(ahora, plazos, cursor="cursor-invalido")
        )
    assert error.value.detalles == {"campos": ["cursor"]}


async def test_el_resumen_consulta_con_el_compromiso_configurado(
    repo, reloj, plazos: Plazos, ahora: datetime
) -> None:
    repo.indicadores_ = Indicadores(5, 1, 1, 0, 0, None, 48, 5, 3)
    desde, hasta = ahora.replace(hour=5), ahora.replace(hour=5) + timedelta(days=1)
    resultado = await ConsultarResumen(repo, reloj, plazos).ejecutar(ANALISTA, desde, hasta)
    assert resultado is repo.indicadores_
    assert repo.consultas_indicadores == [(ANALISTA, desde, hasta, ahora, 48)]


@pytest.mark.parametrize("delta", [timedelta(0), timedelta(hours=-1)])
async def test_el_resumen_rechaza_un_rango_vacio_o_invertido(
    repo, reloj, plazos: Plazos, ahora: datetime, delta: timedelta
) -> None:
    with pytest.raises(SolicitudInvalida) as error:
        await ConsultarResumen(repo, reloj, plazos).ejecutar(ANALISTA, ahora, ahora + delta)
    assert error.value.detalles == {"campos": ["desde", "hasta"]}
    assert repo.consultas_indicadores == []
