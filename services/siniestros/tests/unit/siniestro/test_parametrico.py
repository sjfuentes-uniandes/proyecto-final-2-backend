"""Siniestro paramétrico del dominio (HU-W22, UT-W22-01): la clave idempotente, el valor según la
cobertura y el siniestro que nace `APROBADO`."""

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from siniestros.domain.siniestro.errores import SiniestroInvalido
from siniestros.domain.siniestro.modelos import (
    EntradaHistorial,
    EstadoPaso,
    EstadoSiniestro,
    EventoParametrico,
    OrigenSiniestro,
    PasoTramite,
)
from siniestros.domain.siniestro.parametrico import (
    CARENCIA_PARAMETRICO_DIAS,
    CUENTA_PARAMETRICA,
    PLAZO_AVISO_PARAMETRICO_DIAS,
    DatosParametrico,
    beneficiario_de,
    clave_idempotente,
    nuevo_siniestro,
    validar_valor,
)
from siniestros.domain.siniestro.tramite import ACCION_CREADO_AUTOMATICAMENTE, pasos_tramite
from siniestros.domain.siniestro.transiciones import referencia_pago

EVENTO = UUID("bafc2b26-7c88-5ae3-b5ac-26fdf5427a09")
POLIZA = UUID("b2100000-0000-4000-8000-000000000101")


def test_la_clave_idempotente_es_evento_dos_puntos_poliza() -> None:
    assert clave_idempotente(EVENTO, POLIZA) == f"{EVENTO}:{POLIZA}"


def test_la_clave_del_caso_sale_de_los_datos(
    datos_parametrico: Callable[..., DatosParametrico],
) -> None:
    assert datos_parametrico().clave == f"{EVENTO}:{POLIZA}"


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        ("1400000", "1400000"),
        ("1400000.00", "1400000.00"),
        ("0.01", "0.01"),
        ("620000.5", "620000.5"),
    ],
)
def test_el_valor_de_la_cobertura_es_un_decimal_positivo_de_dos_decimales(
    texto: str, esperado: str
) -> None:
    assert validar_valor(texto) == Decimal(esperado)


@pytest.mark.parametrize("texto", ["0", "0.00", "-5", "abc", "", "NaN", "Infinity", "1.234"])
def test_un_valor_invalido_se_rechaza(texto: str) -> None:
    with pytest.raises(SiniestroInvalido):
        validar_valor(texto)


def test_el_beneficiario_es_el_titular_con_referencia_opaca_y_cuenta_sintetica() -> None:
    cliente = uuid4()
    beneficiario = beneficiario_de(cliente)
    assert beneficiario.beneficiario_ref == f"ben-par-{cliente}"
    assert beneficiario.cuenta_mascarada == CUENTA_PARAMETRICA == "****0000"


def test_el_siniestro_nace_aprobado_sin_analista_con_el_valor_de_la_cobertura(
    datos_parametrico: Callable[..., DatosParametrico],
) -> None:
    ahora = datetime(2026, 11, 10, 14, 5, tzinfo=UTC)
    datos = datos_parametrico()
    siniestro_id = uuid4()

    s = nuevo_siniestro(
        datos, siniestro_id=siniestro_id, codigo="88-2026-1001", ahora=ahora, correlation_id="c-1"
    )

    assert (s.id, s.codigo, s.origen, s.estado) == (
        siniestro_id,
        "88-2026-1001",
        OrigenSiniestro.PARAMETRICO,
        EstadoSiniestro.APROBADO,
    )
    assert s.analista_id is None and s.asignado_en is None
    # El valor de la cobertura queda como valor a indemnizar, sin deducible (la CHECK de W20).
    assert (s.valor_indemnizar, s.deducible, s.valor_reclamado) == (
        Decimal("1400000.00"),
        Decimal("0.00"),
        None,
    )
    assert (s.moneda, s.decidido_en, s.reportado_en) == ("COP", ahora, ahora)
    assert (s.poliza_id, s.cliente_id) == (POLIZA, datos.cliente_id)
    assert s.evento == datos.evento
    assert referencia_pago(s.codigo) == "IND-88-2026-1001-01"


def test_la_copia_de_la_poliza_lleva_el_amparo_de_la_cobertura(
    datos_parametrico: Callable[..., DatosParametrico],
) -> None:
    ahora = datetime(2026, 11, 10, 14, 5, tzinfo=UTC)
    datos = datos_parametrico()
    s = nuevo_siniestro(
        datos, siniestro_id=uuid4(), codigo="88-2026-1001", ahora=ahora, correlation_id="c-1"
    )
    copia = s.copia_poliza

    assert (copia.numero, copia.ramo, copia.moneda) == ("250-2026-000101", "Paramétrico", "COP")
    assert [(a.codigo, a.suma_asegurada) for a in copia.amparos] == [
        ("LLUVIA_ACUMULADA", "1400000.00")
    ]
    assert s.causa == "LLUVIA_ACUMULADA"
    assert (copia.carencia_dias, copia.plazo_aviso_dias, copia.primas_al_dia) == (
        CARENCIA_PARAMETRICO_DIAS,
        PLAZO_AVISO_PARAMETRICO_DIAS,
        True,
    )
    assert copia.beneficiario == beneficiario_de(datos.cliente_id)
    assert (s.fecha_evento, s.ubicacion_declarada.descripcion) == (
        datos.evento.observado_en,
        "Zona 11001",
    )


def test_una_medicion_sin_zona_horaria_se_rechaza() -> None:
    with pytest.raises(SiniestroInvalido, match="observado_en"):
        EventoParametrico(
            evento_id=EVENTO,
            fuente="IDEAM-SIM",
            tipo_evento="LLUVIA_ACUMULADA",
            valor_observado="135",
            umbral="120",
            unidad="mm",
            zona="11001",
            observado_en=datetime(2026, 11, 10, 14, 0),  # noqa: DTZ001 - sin zona a propósito
            evento_origen="EVT-2026-0913",
            regla="R",
            version_regla="1",
        )


def test_la_medicion_solo_cabe_en_un_siniestro_de_origen_parametrico(
    datos_parametrico: Callable[..., DatosParametrico], siniestro: Callable[..., object]
) -> None:
    with pytest.raises(SiniestroInvalido, match="evento"):
        siniestro(evento=datos_parametrico().evento)  # origen MOVIL por omisión


def test_el_historial_automatico_completa_los_pasos_hasta_la_decision(
    entrada: Callable[..., EntradaHistorial],
) -> None:
    ahora = datetime(2026, 11, 10, 14, 5, tzinfo=UTC)
    creado = entrada(
        ACCION_CREADO_AUTOMATICAMENTE,
        ahora,
        estado_nuevo=EstadoSiniestro.APROBADO,
        actor_rol="SISTEMA_PARAMETRICO",
    )

    pasos = pasos_tramite(EstadoSiniestro.APROBADO, [creado])

    # Los cuatro primeros ocurrieron con la creación; el pago es el paso actual (espera a `dinero`).
    assert [p.estado for p in pasos] == [
        EstadoPaso.COMPLETADO,
        EstadoPaso.COMPLETADO,
        EstadoPaso.COMPLETADO,
        EstadoPaso.COMPLETADO,
        EstadoPaso.ACTUAL,
    ]
    assert pasos[0].paso is PasoTramite.RECIBIDO
    assert all(p.ocurrido_en == ahora for p in pasos[:4])
    assert all(p.actor_nombre is None for p in pasos)
