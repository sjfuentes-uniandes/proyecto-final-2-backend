"""De la activación al siniestro (HU-W22, UT-W22-01): la clave, la solicitud y la vigencia."""

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from pagos_parametricos.domain.parametrico.activacion import (
    REGLA_UMBRAL,
    ActivacionIncompleta,
    ActivacionPendiente,
    armar_solicitud,
    clave_idempotente,
    vigencia_de_ventana,
)
from pagos_parametricos.domain.parametrico.modelos import EventoObservado
from pagos_parametricos.domain.parametrico.ventana import ventana_de_vigencia

EVENTO = UUID("bafc2b26-7c88-5ae3-b5ac-26fdf5427a09")
POLIZA = UUID("b2100000-0000-4000-8000-000000000101")
CLIENTE = UUID("00000000-0000-4000-8000-000000000002")
DESDE, HASTA = ventana_de_vigencia(date(2026, 9, 1), date(2027, 8, 31))


def _evento(**cambios: object) -> EventoObservado:
    datos = {
        "evento_id": EVENTO,
        "fuente": "IDEAM-SIM",
        "id_externo": "IDEAM-2026-1110-0001",
        "evento_origen": "EVT-2026-0913",
        "tipo_evento": "LLUVIA_ACUMULADA",
        "valor_observado": Decimal(135),
        "unidad": "mm",
        "zona": "11001",
        "observado_en": datetime(2026, 11, 10, 14, 0, tzinfo=UTC),
    } | cambios
    return EventoObservado(**datos)  # type: ignore[arg-type]


def _pendiente(**cambios: object) -> ActivacionPendiente:
    datos = {
        "evento_id": EVENTO,
        "poliza_id": POLIZA,
        "valor_pago": Decimal("1400000.00"),
        "moneda": "COP",
        "version_regla": "1",
        "cliente_id": CLIENTE,
        "numero": "250-2026-000101",
        "ramo": "Paramétrico",
        "umbral": Decimal("120.000"),
        "vigente_desde": DESDE,
        "vigente_hasta": HASTA,
    } | cambios
    return ActivacionPendiente(**datos)  # type: ignore[arg-type]


def test_la_clave_es_evento_dos_puntos_poliza() -> None:
    assert clave_idempotente(EVENTO, POLIZA) == f"{EVENTO}:{POLIZA}"


def test_la_vigencia_de_la_ventana_recupera_las_fechas_de_la_poliza() -> None:
    assert vigencia_de_ventana(DESDE, HASTA) == (date(2026, 9, 1), date(2027, 8, 31))


@pytest.mark.parametrize(
    ("desde", "hasta"),
    [(date(2026, 11, 2), date(2027, 11, 1)), (date(2026, 1, 1), date(2026, 1, 1))],
)
def test_ida_y_vuelta_con_la_ventana_de_w21(desde: date, hasta: date) -> None:
    assert vigencia_de_ventana(*ventana_de_vigencia(desde, hasta)) == (desde, hasta)


def test_la_solicitud_lleva_el_valor_de_la_cobertura_y_la_medicion_del_evento() -> None:
    s = armar_solicitud(_evento(), _pendiente())

    assert s.clave == f"{EVENTO}:{POLIZA}"
    assert (s.cliente_id, s.numero, s.ramo, s.moneda) == (
        CLIENTE,
        "250-2026-000101",
        "Paramétrico",
        "COP",
    )
    assert (s.vigencia_desde, s.vigencia_hasta) == (date(2026, 9, 1), date(2027, 8, 31))
    # El valor es el de la cobertura al evaluar, no el actual; el umbral, sin ceros inventados.
    assert (s.valor, s.umbral, s.regla, s.version_regla) == (
        "1400000.00",
        "120.000",
        REGLA_UMBRAL,
        "1",
    )
    assert s.evento.valor_observado == Decimal(135)


def test_el_disparador_es_la_medicion_contra_el_umbral() -> None:
    d = armar_solicitud(_evento(), _pendiente()).disparador

    assert (d.tipo_evento, d.valor_observado, d.umbral, d.unidad) == (
        "LLUVIA_ACUMULADA",
        "135",
        "120.000",
        "mm",
    )


@pytest.mark.parametrize(
    "faltante",
    [
        {"cliente_id": None},
        {"numero": None},
        {"ramo": None},
        {"umbral": None},
        {"vigente_desde": None},
        {"vigente_hasta": None},
    ],
)
def test_una_cobertura_incompleta_no_puede_crear_el_siniestro(faltante: dict[str, None]) -> None:
    with pytest.raises(ActivacionIncompleta):
        armar_solicitud(_evento(), _pendiente(**faltante))
