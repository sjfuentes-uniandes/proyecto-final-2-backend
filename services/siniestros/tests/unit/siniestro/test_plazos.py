"""Reloj del compromiso de 48 h y prioridad derivada (DS3-04)."""

from collections.abc import Callable
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from siniestros.domain.siniestro.errores import SiniestroInvalido
from siniestros.domain.siniestro.modelos import EstadoSiniestro, PrioridadSiniestro, Siniestro
from siniestros.domain.siniestro.plazos import prioridad, tiempo_restante

ANALISTA = uuid4()
HORA = timedelta(hours=1)


def test_un_caso_reportado_hace_14_horas_tiene_34_horas(
    siniestro: Callable[..., Siniestro], ahora: datetime
) -> None:
    # La semilla EVA-01 sale con 2040 minutos (34 h).
    assert tiempo_restante(siniestro(), ahora) == 2040


def test_un_caso_vencido_hace_dos_horas_sale_negativo(
    siniestro: Callable[..., Siniestro], ahora: datetime
) -> None:
    caso = siniestro(reportado_en=ahora - 50 * HORA, fecha_evento=ahora - 51 * HORA)
    assert tiempo_restante(caso, ahora) == -120


def test_el_tiempo_se_redondea_hacia_abajo(
    siniestro: Callable[..., Siniestro], ahora: datetime
) -> None:
    justo_vencido = siniestro(
        reportado_en=ahora - 48 * HORA - timedelta(seconds=30),
        fecha_evento=ahora - 49 * HORA,
    )
    assert tiempo_restante(justo_vencido, ahora) == -1
    quedan_30_s = siniestro(
        reportado_en=ahora - 48 * HORA + timedelta(seconds=30), fecha_evento=ahora - 49 * HORA
    )
    assert tiempo_restante(quedan_30_s, ahora) == 0


def test_la_pausa_acumulada_no_cuenta(siniestro: Callable[..., Siniestro], ahora: datetime) -> None:
    caso = siniestro(pausa_acumulada_s=int(10 * HORA.total_seconds()))
    assert tiempo_restante(caso, ahora) == 2040 + 10 * 60


def test_un_caso_pausado_congela_el_reloj(
    siniestro: Callable[..., Siniestro], ahora: datetime
) -> None:
    pausado = siniestro(
        estado=EstadoSiniestro.FALTA_SOPORTE,
        analista_id=ANALISTA,
        asignado_en=ahora - 13 * HORA,
        pausado_desde=ahora - 4 * HORA,
    )
    # 14 h reportado, 4 h en pausa: corrieron 10 h, quedan 38 h.
    assert tiempo_restante(pausado, ahora) == 38 * 60
    # Una hora después sigue igual.
    assert tiempo_restante(pausado, ahora + HORA) == 38 * 60


def test_un_caso_decidido_detiene_el_reloj_en_la_decision(
    siniestro: Callable[..., Siniestro], ahora: datetime
) -> None:
    decidido = siniestro(
        estado=EstadoSiniestro.APROBADO,
        analista_id=ANALISTA,
        asignado_en=ahora - 13 * HORA,
        decidido_en=ahora - 4 * HORA,
    )
    assert tiempo_restante(decidido, ahora) == 38 * 60
    assert tiempo_restante(decidido, ahora + 100 * HORA) == 38 * 60


def test_el_compromiso_es_configurable(
    siniestro: Callable[..., Siniestro], ahora: datetime
) -> None:
    assert tiempo_restante(siniestro(), ahora, compromiso_horas=24) == 10 * 60


def test_un_reloj_atrasado_no_suma_tiempo(
    siniestro: Callable[..., Siniestro], ahora: datetime
) -> None:
    assert tiempo_restante(siniestro(), ahora - 100 * HORA) == 48 * 60


def test_ahora_exige_zona(siniestro: Callable[..., Siniestro], ingenuo: datetime) -> None:
    with pytest.raises(SiniestroInvalido, match="ahora"):
        tiempo_restante(siniestro(), ingenuo)


@pytest.mark.parametrize(
    ("minutos", "esperada"),
    [
        (-1000, PrioridadSiniestro.ALTA),
        (-1, PrioridadSiniestro.ALTA),
        (0, PrioridadSiniestro.ALTA),
        (479, PrioridadSiniestro.ALTA),
        (480, PrioridadSiniestro.MEDIA),
        (1439, PrioridadSiniestro.MEDIA),
        (1440, PrioridadSiniestro.BAJA),
        (2040, PrioridadSiniestro.BAJA),
    ],
)
def test_la_prioridad_se_deriva_con_limites_estrictos(
    minutos: int, esperada: PrioridadSiniestro
) -> None:
    assert prioridad(minutos) is esperada


def test_los_umbrales_de_la_prioridad_son_configurables() -> None:
    assert prioridad(100, alta_min=60, media_min=120) is PrioridadSiniestro.MEDIA
    assert prioridad(120, alta_min=60, media_min=120) is PrioridadSiniestro.BAJA
