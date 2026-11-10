"""Indemnizacion y la selección de pendientes a conciliar (UT-W23-01; ADR 0015)."""

import dataclasses
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from dinero.domain.indemnizacion.conciliacion import es_reclamable, seleccionar_pendientes
from dinero.domain.indemnizacion.errores import IndemnizacionInvalida, TransicionInvalida
from dinero.domain.indemnizacion.modelos import (
    MOTIVO_NO_INFORMADO,
    EstadoIndemnizacion,
    Indemnizacion,
    Moneda,
    motivo_de_rechazo,
)

AHORA = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)
DESPUES = AHORA + timedelta(seconds=2)
REFERENCIA = "IND-88-2026-0442-01"


def nueva(**cambios: Any) -> Indemnizacion:
    datos: dict[str, Any] = {
        "siniestro_id": uuid4(),
        "referencia": REFERENCIA,
        "monto": Decimal("2500000.00"),
        "moneda": "COP",
        "beneficiario_ref": "ben-sint-001",
        "correlation_id": "corr-w23",
        "ahora": AHORA,
    }
    datos.update(cambios)
    return Indemnizacion.pendiente(**datos)


def test_nace_pendiente_sin_resultado() -> None:
    i = nueva()
    assert i.estado is EstadoIndemnizacion.PENDIENTE and not i.final
    assert (i.transaccion_id, i.motivo, i.resuelto_en, i.reclamado_hasta) == (
        None,
        None,
        None,
        None,
    )
    assert (i.intentos, i.duplicados_descartados, i.ultimo_incierto) == (0, 0, None)
    assert (i.tipo, i.referencia, i.moneda) == ("INDEMNIZACION", REFERENCIA, Moneda.COP)
    assert i.creado_en == i.actualizado_en == AHORA
    assert isinstance(i.indemnizacion_id, UUID)


def test_la_clave_idempotente_es_la_referencia_y_el_id_se_puede_fijar() -> None:
    fijo = uuid4()
    assert nueva(indemnizacion_id=fijo).indemnizacion_id == fijo
    assert nueva().indemnizacion_id != nueva().indemnizacion_id


def test_confirmar_guarda_la_transaccion_y_suma_un_intento() -> None:
    c = nueva().confirmar("TRX-1", DESPUES)
    assert (c.estado, c.transaccion_id, c.motivo) == (EstadoIndemnizacion.CONFIRMADA, "TRX-1", None)
    assert (c.intentos, c.resuelto_en, c.actualizado_en, c.final) == (1, DESPUES, DESPUES, True)


def test_rechazar_guarda_el_motivo_y_la_transaccion_si_la_trae() -> None:
    r = nueva().rechazar("BENEFICIARIO_NO_HABILITADO", DESPUES, transaccion_id="TRX-R")
    assert (r.estado, r.motivo, r.transaccion_id) == (
        EstadoIndemnizacion.RECHAZADA,
        "BENEFICIARIO_NO_HABILITADO",
        "TRX-R",
    )
    assert nueva().rechazar("X_MOTIVO", DESPUES).transaccion_id is None


def test_un_incierto_sigue_pendiente_y_cuenta_el_intento() -> None:
    i = nueva().registrar_incierto("ADAPTADOR_TIEMPO_AGOTADO", DESPUES)
    assert (i.estado, i.ultimo_incierto, i.intentos, i.actualizado_en) == (
        EstadoIndemnizacion.PENDIENTE,
        "ADAPTADOR_TIEMPO_AGOTADO",
        1,
        DESPUES,
    )
    assert i.resuelto_en is None and i.motivo is None and i.transaccion_id is None
    # Un incierto de una consulta no es un intento de pago.
    assert nueva().registrar_incierto("HTTP_503", DESPUES, intento=False).intentos == 0


def test_resolver_por_consulta_no_suma_intento_pero_cuenta_al_menos_uno() -> None:
    """La pasarela ya conoce la orden: hubo al menos un intento, aunque no se haya registrado."""
    assert nueva().confirmar("T", DESPUES, intento=False).intentos == 1
    incierta = nueva().registrar_incierto("ADAPTADOR_TIEMPO_AGOTADO", DESPUES)
    assert incierta.confirmar("T", DESPUES, intento=False).intentos == 1
    assert incierta.rechazar("MOTIVO", DESPUES, intento=False).intentos == 1
    # Un reintento del desembolso sí suma.
    assert incierta.confirmar("T", DESPUES, intento=True).intentos == 2


def test_de_un_estado_final_no_se_sale() -> None:
    final = nueva().confirmar("TRX-1", DESPUES)
    for operacion in (
        lambda: final.confirmar("OTRA", DESPUES),
        lambda: final.rechazar("MOTIVO", DESPUES),
        lambda: final.registrar_incierto("CODIGO", DESPUES),
        lambda: final.reclamar(DESPUES),
    ):
        with pytest.raises(TransicionInvalida) as error:
            operacion()
        assert error.value.desde is EstadoIndemnizacion.CONFIRMADA
    rechazada = nueva().rechazar("MOTIVO", DESPUES)
    with pytest.raises(TransicionInvalida):
        rechazada.confirmar("TRX", DESPUES)


def test_registrar_duplicado_solo_suma_en_cualquier_estado() -> None:
    for i in (nueva(), nueva().confirmar("TRX-1", DESPUES), nueva().rechazar("M", DESPUES)):
        d = i.registrar_duplicado().registrar_duplicado()
        assert d.duplicados_descartados == 2
        assert dataclasses.replace(d, duplicados_descartados=0) == i


def test_reclamar_marca_el_reclamo_y_un_resultado_lo_limpia() -> None:
    reclamada = nueva().reclamar(DESPUES)
    assert reclamada.reclamado_hasta == DESPUES
    assert reclamada.confirmar("T", DESPUES).reclamado_hasta is None
    assert reclamada.rechazar("M", DESPUES).reclamado_hasta is None
    assert reclamada.registrar_incierto("C", DESPUES).reclamado_hasta is None


@pytest.mark.parametrize(
    ("cambios", "campo"),
    [
        ({"monto": Decimal(0)}, "monto"),
        ({"monto": Decimal(-1)}, "monto"),
        ({"monto": Decimal("1.234")}, "monto"),
        ({"monto": Decimal("NaN")}, "monto"),
        ({"monto": 100}, "monto"),
        ({"moneda": "USD"}, "moneda"),
        ({"referencia": "IND-0442-01"}, "referencia"),
        ({"referencia": 5}, "referencia"),
        ({"beneficiario_ref": ""}, "beneficiario_ref"),
        ({"beneficiario_ref": "x" * 129}, "beneficiario_ref"),
        ({"beneficiario_ref": 5}, "beneficiario_ref"),
        ({"correlation_id": ""}, "correlation_id"),
        ({"siniestro_id": "no-uuid"}, "siniestro_id"),
        ({"ahora": AHORA.replace(tzinfo=None)}, "creado_en"),
    ],
)
def test_invariantes_al_crear(cambios: dict[str, object], campo: str) -> None:
    with pytest.raises(IndemnizacionInvalida) as error:
        nueva(**cambios)
    assert str(error.value).startswith(campo)


def test_el_mensaje_nombra_el_campo_y_nunca_su_valor() -> None:
    with pytest.raises(IndemnizacionInvalida) as error:
        nueva(beneficiario_ref="")
    assert "ben-sint" not in str(error.value)


@pytest.mark.parametrize(
    ("cambios", "campo"),
    [
        ({"tipo": "RECAUDO"}, "tipo"),
        ({"estado": "PENDIENTE"}, "estado"),
        ({"intentos": -1}, "intentos"),
        ({"intentos": True}, "intentos"),
        ({"duplicados_descartados": -1}, "duplicados_descartados"),
        ({"ultimo_incierto": "texto libre"}, "ultimo_incierto"),
        ({"resuelto_en": DESPUES.replace(tzinfo=None)}, "resuelto_en"),
        ({"reclamado_hasta": DESPUES.replace(tzinfo=None)}, "reclamado_hasta"),
        ({"actualizado_en": AHORA.replace(tzinfo=None)}, "actualizado_en"),
        ({"transaccion_id": "TRX"}, "transaccion_id"),
        ({"motivo": "MOTIVO"}, "motivo"),
        ({"resuelto_en": DESPUES}, "resuelto_en"),
        ({"indemnizacion_id": "x"}, "indemnizacion_id"),
        ({"moneda": "COP"}, "moneda"),
    ],
)
def test_invariantes_al_reconstruir_una_pendiente(cambios: dict[str, object], campo: str) -> None:
    with pytest.raises(IndemnizacionInvalida) as error:
        dataclasses.replace(nueva(), **cambios)
    assert str(error.value).startswith(campo)


@pytest.mark.parametrize(
    ("cambios", "campo"),
    [
        ({"transaccion_id": None}, "transaccion_id"),
        ({"transaccion_id": " "}, "transaccion_id"),
        ({"transaccion_id": "x" * 129}, "transaccion_id"),
        ({"motivo": "MOTIVO"}, "motivo"),
        ({"resuelto_en": None}, "resuelto_en"),
    ],
)
def test_invariantes_de_una_confirmada(cambios: dict[str, object], campo: str) -> None:
    with pytest.raises(IndemnizacionInvalida) as error:
        dataclasses.replace(nueva().confirmar("TRX-1", DESPUES), **cambios)
    assert str(error.value).startswith(campo)


@pytest.mark.parametrize(
    ("cambios", "campo"),
    [
        ({"motivo": None}, "motivo"),
        ({"motivo": "texto libre"}, "motivo"),
        ({"transaccion_id": " "}, "transaccion_id"),
        ({"resuelto_en": None}, "resuelto_en"),
    ],
)
def test_invariantes_de_una_rechazada(cambios: dict[str, object], campo: str) -> None:
    with pytest.raises(IndemnizacionInvalida) as error:
        dataclasses.replace(nueva().rechazar("MOTIVO", DESPUES), **cambios)
    assert str(error.value).startswith(campo)


def test_el_motivo_se_normaliza_a_un_codigo() -> None:
    assert motivo_de_rechazo("FONDOS_INSUFICIENTES") == "FONDOS_INSUFICIENTES"
    for crudo in (None, "", "texto libre con espacios", "minuscula", "1NUMERO"):
        assert motivo_de_rechazo(crudo) == MOTIVO_NO_INFORMADO


# --- Selección de pendientes a conciliar (DS3-18) ---------------------------------------------------
MIN = timedelta(minutes=5)
AHORA_C = AHORA + timedelta(minutes=10)


def _creada_hace(minutos: float) -> Indemnizacion:
    return nueva(ahora=AHORA_C - timedelta(minutes=minutos))


def test_solo_se_reclama_una_pendiente_antigua() -> None:
    assert es_reclamable(_creada_hace(6), AHORA_C, MIN)
    assert not es_reclamable(_creada_hace(4), AHORA_C, MIN)
    # Justo en el límite no: debe ser anterior a `ahora - antigüedad`.
    assert not es_reclamable(_creada_hace(5), AHORA_C, MIN)


def test_una_resuelta_no_se_reclama() -> None:
    antigua = _creada_hace(60)
    assert not es_reclamable(antigua.confirmar("T", AHORA_C - timedelta(minutes=50)), AHORA_C, MIN)
    assert not es_reclamable(antigua.rechazar("M", AHORA_C - timedelta(minutes=50)), AHORA_C, MIN)


def test_un_reclamo_vigente_la_aparta_y_uno_vencido_no() -> None:
    antigua = _creada_hace(60)
    assert not es_reclamable(antigua.reclamar(AHORA_C + timedelta(seconds=1)), AHORA_C, MIN)
    assert not es_reclamable(antigua.reclamar(AHORA_C), AHORA_C, MIN)
    assert es_reclamable(antigua.reclamar(AHORA_C - timedelta(seconds=1)), AHORA_C, MIN)


def test_la_seleccion_toma_las_mas_antiguas_primero_hasta_el_lote() -> None:
    a, b, c = _creada_hace(30), _creada_hace(20), _creada_hace(10)
    nueva_ = _creada_hace(1)
    resuelta = _creada_hace(40).confirmar("T", AHORA_C)
    reclamada = _creada_hace(50).reclamar(AHORA_C + MIN)
    todas = [c, nueva_, a, resuelta, b, reclamada]
    assert seleccionar_pendientes(todas, AHORA_C, MIN, 2) == [a, b]
    assert seleccionar_pendientes(todas, AHORA_C, MIN, 10) == [a, b, c]
    assert seleccionar_pendientes(todas, AHORA_C, MIN, 0) == []
    assert seleccionar_pendientes([], AHORA_C, MIN, 5) == []


def test_el_desempate_por_id_es_estable() -> None:
    a = nueva(ahora=AHORA_C - timedelta(minutes=30), indemnizacion_id=UUID(int=2))
    b = nueva(ahora=AHORA_C - timedelta(minutes=30), indemnizacion_id=UUID(int=1))
    assert seleccionar_pendientes([a, b], AHORA_C, MIN, 2) == [b, a]
