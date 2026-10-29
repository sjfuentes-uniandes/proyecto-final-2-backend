"""El movimiento de recaudo y sus transiciones (HU-W16, plan §d; ADR 0013, decisiones 4, 5 y 7)."""

import dataclasses
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from dinero.domain.recaudo.errores import RecaudoInvalido, TransicionInvalida
from dinero.domain.recaudo.modelos import EstadoRecaudo, Moneda, Recaudo

AHORA = datetime(2026, 10, 28, 15, 0, tzinfo=UTC)
DESPUES = AHORA + timedelta(seconds=3)
Fabrica = Callable[..., Recaudo]


# --- Creación --------------------------------------------------------------------------------
def test_un_movimiento_nuevo_nace_pendiente_con_lo_que_trae_el_evento(pendiente: Fabrica) -> None:
    clave, cotizacion = uuid4(), uuid4()
    recaudo = pendiente(clave_recaudo=clave, cotizacion_id=cotizacion)
    assert isinstance(recaudo.recaudo_id, UUID)
    assert recaudo.recaudo_id != clave
    assert (recaudo.clave_recaudo, recaudo.cotizacion_id) == (clave, cotizacion)
    assert recaudo.estado is EstadoRecaudo.PENDIENTE
    assert recaudo.moneda is Moneda.COP
    assert (recaudo.monto, recaudo.periodicidad) == (Decimal(64638), "MENSUAL")
    assert (recaudo.transaccion_id, recaudo.motivo, recaudo.ultimo_incierto) == (None, None, None)
    assert (recaudo.creado_en, recaudo.actualizado_en, recaudo.resuelto_en) == (AHORA, AHORA, None)
    assert recaudo.correlation_id == "corr-w16"
    assert recaudo.final is False


def test_cada_movimiento_nuevo_tiene_su_propio_recaudo_id(pendiente: Fabrica) -> None:
    assert pendiente().recaudo_id != pendiente().recaudo_id


def test_el_recaudo_id_se_puede_fijar(pendiente: Fabrica) -> None:
    fijo = uuid4()
    assert pendiente(recaudo_id=fijo).recaudo_id == fijo


@pytest.mark.parametrize("texto", ["64638", "0.01", "999999999.99", "64638.00", "1.005"])
def test_el_monto_se_conserva_exacto(pendiente: Fabrica, texto: str) -> None:
    recaudo = pendiente(monto=Decimal(texto))
    assert recaudo.monto == Decimal(texto)
    assert str(recaudo.monto) == texto  # ni redondeo ni cambio de escala


@pytest.mark.parametrize(
    "monto",
    [
        64638.0,  # float: nunca (ADR 0013)
        64638,  # int: el contrato trae texto decimal y se convierte a Decimal
        "64638",
        True,
        Decimal(0),
        Decimal(-1),
        Decimal("NaN"),
        Decimal("Infinity"),
    ],
)
def test_el_monto_debe_ser_un_decimal_finito_mayor_que_cero(pendiente: Fabrica, monto: Any) -> None:
    with pytest.raises(RecaudoInvalido, match="monto"):
        pendiente(monto=monto)


@pytest.mark.parametrize("moneda", ["USD", "cop", ""])
def test_solo_cop(pendiente: Fabrica, moneda: str) -> None:
    with pytest.raises(RecaudoInvalido, match="moneda"):
        pendiente(moneda=moneda)


def test_la_moneda_puede_venir_como_enum(pendiente: Fabrica) -> None:
    assert pendiente(moneda=Moneda.COP).moneda is Moneda.COP


@pytest.mark.parametrize(
    ("campo", "valor"),
    [
        ("periodicidad", ""),
        ("correlation_id", ""),
        ("clave_recaudo", "no-es-uuid"),
        ("cotizacion_id", None),
        ("pagador_ref", "C01"),
        ("ahora", AHORA.replace(tzinfo=None)),  # sin zona horaria
    ],
)
def test_campos_obligatorios(pendiente: Fabrica, campo: str, valor: Any) -> None:
    with pytest.raises(RecaudoInvalido):
        pendiente(**{campo: valor})


# --- Transiciones válidas ------------------------------------------------------------------------
def test_pendiente_a_confirmado_con_transaccion(pendiente: Fabrica) -> None:
    original = pendiente()
    confirmado = original.confirmar("TX-SINT-C01", DESPUES)
    assert confirmado.estado is EstadoRecaudo.CONFIRMADO
    assert (confirmado.transaccion_id, confirmado.motivo) == ("TX-SINT-C01", None)
    assert (confirmado.actualizado_en, confirmado.resuelto_en) == (DESPUES, DESPUES)
    assert confirmado.creado_en == AHORA
    assert confirmado.final is True
    # Lo que trae el evento no cambia, y el original sigue igual (inmutable).
    assert (
        dataclasses.replace(
            confirmado,
            estado=original.estado,
            transaccion_id=None,
            actualizado_en=AHORA,
            resuelto_en=None,
        )
        == original
    )
    assert original.estado is EstadoRecaudo.PENDIENTE


def test_pendiente_a_rechazado_con_motivo(pendiente: Fabrica) -> None:
    rechazado = pendiente().rechazar("FONDOS_INSUFICIENTES", DESPUES)
    assert rechazado.estado is EstadoRecaudo.RECHAZADO
    assert (rechazado.motivo, rechazado.transaccion_id) == ("FONDOS_INSUFICIENTES", None)
    assert (rechazado.actualizado_en, rechazado.resuelto_en) == (DESPUES, DESPUES)
    assert rechazado.final is True


def test_un_rechazo_puede_traer_la_transaccion_del_proveedor(pendiente: Fabrica) -> None:
    # El adaptador admite transaccionId nulo o texto en un RECHAZADO (plan §c).
    rechazado = pendiente().rechazar("FONDOS_INSUFICIENTES", DESPUES, transaccion_id="TX-R")
    assert rechazado.transaccion_id == "TX-R"


@pytest.mark.parametrize("codigo", ["TIEMPO_AGOTADO", "FUENTE_NO_DISPONIBLE", "RESPUESTA_INVALIDA"])
def test_un_incierto_deja_pendiente_y_registra_el_resultado(
    pendiente: Fabrica, codigo: str
) -> None:
    incierto = pendiente().registrar_incierto(codigo, DESPUES)
    assert incierto.estado is EstadoRecaudo.PENDIENTE  # nunca se asume rechazo
    assert incierto.ultimo_incierto == codigo
    assert (incierto.transaccion_id, incierto.motivo, incierto.resuelto_en) == (None, None, None)
    assert incierto.actualizado_en == DESPUES
    assert incierto.final is False


def test_tras_un_incierto_se_puede_confirmar_y_queda_el_ultimo_incierto(pendiente: Fabrica) -> None:
    recaudo = (
        pendiente()
        .registrar_incierto("TIEMPO_AGOTADO", DESPUES)
        .registrar_incierto("FUENTE_NO_DISPONIBLE", DESPUES)
        .confirmar("TX-SINT-C06", DESPUES + timedelta(minutes=5))
    )
    assert recaudo.estado is EstadoRecaudo.CONFIRMADO
    assert recaudo.ultimo_incierto == "FUENTE_NO_DISPONIBLE"


# --- Transiciones inválidas ----------------------------------------------------------------------
def _finales(pendiente: Fabrica) -> list[Recaudo]:
    return [
        pendiente().confirmar("TX-SINT-C01", DESPUES),
        pendiente().rechazar("FONDOS_INSUFICIENTES", DESPUES),
    ]


@pytest.mark.parametrize("indice", [0, 1], ids=["CONFIRMADO", "RECHAZADO"])
@pytest.mark.parametrize(
    "hecho",
    [
        lambda r: r.confirmar("TX-OTRA", DESPUES),
        lambda r: r.rechazar("FONDOS_INSUFICIENTES", DESPUES),
        lambda r: r.registrar_incierto("TIEMPO_AGOTADO", DESPUES),
    ],
    ids=["confirmar", "rechazar", "incierto"],
)
def test_de_un_estado_final_no_se_sale(
    pendiente: Fabrica, indice: int, hecho: Callable[[Recaudo], Recaudo]
) -> None:
    final = _finales(pendiente)[indice]
    with pytest.raises(TransicionInvalida) as error:
        hecho(final)
    assert error.value.desde is final.estado


@pytest.mark.parametrize("transaccion", ["", "   ", None, "x" * 129])
def test_confirmar_exige_transaccion(pendiente: Fabrica, transaccion: Any) -> None:
    with pytest.raises(RecaudoInvalido, match="transaccion_id"):
        pendiente().confirmar(transaccion, DESPUES)


@pytest.mark.parametrize(
    "motivo",
    ["", None, "fondos insuficientes", "Juan Pérez", "4111111111111111", "A" * 65],
)
def test_rechazar_exige_un_motivo_con_forma_de_codigo(pendiente: Fabrica, motivo: Any) -> None:
    # Un código, nunca texto libre: así no puede entrar PII del proveedor.
    with pytest.raises(RecaudoInvalido, match="motivo"):
        pendiente().rechazar(motivo, DESPUES)


@pytest.mark.parametrize("transaccion", ["", " ", "x" * 129])
def test_la_transaccion_de_un_rechazo_si_viene_no_esta_vacia(
    pendiente: Fabrica, transaccion: str
) -> None:
    with pytest.raises(RecaudoInvalido, match="transaccion_id"):
        pendiente().rechazar("FONDOS_INSUFICIENTES", DESPUES, transaccion_id=transaccion)


@pytest.mark.parametrize("codigo", ["", "timeout tras 5000 ms", "cliente@correo.co"])
def test_el_incierto_es_un_codigo(pendiente: Fabrica, codigo: str) -> None:
    with pytest.raises(RecaudoInvalido, match="ultimo_incierto"):
        pendiente().registrar_incierto(codigo, DESPUES)


def test_una_transicion_exige_hora_con_zona(pendiente: Fabrica) -> None:
    with pytest.raises(RecaudoInvalido, match="actualizado_en"):
        pendiente().confirmar("TX-SINT-C01", DESPUES.replace(tzinfo=None))


# --- Invariantes al construir (p. ej., al leer de la base) -------------------------------------
@pytest.mark.parametrize(
    ("cambios", "campo"),
    [
        ({"estado": EstadoRecaudo.CONFIRMADO, "resuelto_en": AHORA}, "transaccion_id"),
        ({"estado": EstadoRecaudo.RECHAZADO, "resuelto_en": AHORA}, "motivo"),
        ({"estado": EstadoRecaudo.CONFIRMADO, "transaccion_id": "TX"}, "resuelto_en"),
        ({"transaccion_id": "TX"}, "transaccion_id"),
        ({"motivo": "FONDOS_INSUFICIENTES"}, "motivo"),
        ({"resuelto_en": AHORA}, "resuelto_en"),
        (
            {
                "estado": EstadoRecaudo.CONFIRMADO,
                "transaccion_id": "TX",
                "motivo": "FONDOS_INSUFICIENTES",
                "resuelto_en": AHORA,
            },
            "motivo",
        ),
        ({"estado": "PENDIENTE"}, "estado"),
        ({"moneda": "COP"}, "moneda"),
        ({"creado_en": AHORA.replace(tzinfo=None)}, "creado_en"),
        ({"resuelto_en": AHORA.replace(tzinfo=None)}, "resuelto_en"),
    ],
)
def test_invariantes_de_la_construccion_directa(
    pendiente: Fabrica, cambios: dict[str, Any], campo: str
) -> None:
    base = pendiente()
    datos = {f.name: getattr(base, f.name) for f in dataclasses.fields(Recaudo)}
    datos.update(cambios)
    with pytest.raises(RecaudoInvalido, match=campo):
        Recaudo(**datos)


def test_el_error_de_transicion_nombra_los_estados(pendiente: Fabrica) -> None:
    final = pendiente().confirmar("TX-SINT-C01", DESPUES)
    with pytest.raises(TransicionInvalida, match="CONFIRMADO -> RECHAZADO"):
        final.rechazar("FONDOS_INSUFICIENTES", DESPUES)


def test_el_dominio_no_tiene_float_ni_datos_de_pago() -> None:
    """ADR 0013: montos como Decimal; ningún dato de tarjeta ni de cuenta."""
    tipos = {f.name: f.type for f in dataclasses.fields(Recaudo)}
    assert "float" not in " ".join(str(t) for t in tipos.values())
    prohibidos = ("tarjeta", "cuenta", "card", "pan", "cvv", "iban", "nombre", "documento")
    assert not [c for c in tipos for p in prohibidos if p in c.lower()]
