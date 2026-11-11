"""El pago único de la indemnización en el dominio (HU-W23; ADR 0015): lo que ve el detalle, el
primer resultado gana y el paso del trámite."""

from collections.abc import Callable
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from siniestros.domain.siniestro.modelos import (
    EntradaHistorial,
    EstadoPaso,
    EstadoSiniestro,
    PasoTramite,
)
from siniestros.domain.siniestro.pago import (
    ACCION_PAGO_CONFIRMADO,
    ACCION_PAGO_RECHAZADO,
    EstadoPago,
    PagoSiniestro,
    ResultadoPago,
    acepta_resultado,
    pago_de,
)
from siniestros.domain.siniestro.tramite import pasos_tramite

CODIGO = "88-2026-0442"
REFERENCIA = "IND-88-2026-0442-01"


def _resultado(ahora: datetime, estado: EstadoPago = EstadoPago.CONFIRMADO) -> ResultadoPago:
    confirmado = estado is EstadoPago.CONFIRMADO
    return ResultadoPago(
        siniestro_id=uuid4(),
        estado=estado,
        referencia=REFERENCIA,
        transaccion_id="TRX-1" if confirmado else None,
        motivo=None if confirmado else "BENEFICIARIO_NO_HABILITADO",
        intentos=1,
        duplicados_descartados=0,
        ocurrido_en=ahora,
    )


def test_un_caso_que_no_esta_aprobado_no_tiene_pago() -> None:
    for estado in (
        EstadoSiniestro.REPORTADO,
        EstadoSiniestro.EN_EVALUACION,
        EstadoSiniestro.FALTA_SOPORTE,
        EstadoSiniestro.RECHAZADO,
    ):
        assert pago_de(estado, CODIGO, None) is None


def test_un_caso_aprobado_sin_resultado_de_dinero_esta_pendiente_con_la_referencia_prevista() -> (
    None
):
    assert pago_de(EstadoSiniestro.APROBADO, CODIGO, None) == PagoSiniestro(
        EstadoPago.PENDIENTE, REFERENCIA, None, None, 0, 0
    )


def test_el_pago_guardado_manda_sobre_el_derivado() -> None:
    guardado = PagoSiniestro(EstadoPago.CONFIRMADO, REFERENCIA, "TRX-1", None, 2, 1)
    assert pago_de(EstadoSiniestro.APROBADO, CODIGO, guardado) is guardado


@pytest.mark.parametrize(
    ("guardado", "acepta"),
    [
        (None, True),
        (EstadoPago.SIN_PAGO, True),
        (EstadoPago.PENDIENTE, True),
        (EstadoPago.CONFIRMADO, False),
        (EstadoPago.RECHAZADO, False),
    ],
)
def test_solo_el_primer_resultado_cambia_algo(guardado: EstadoPago | None, acepta: bool) -> None:
    assert acepta_resultado(guardado) is acepta


def test_un_resultado_es_confirmado_o_rechazado(ahora: datetime) -> None:
    assert _resultado(ahora).accion == ACCION_PAGO_CONFIRMADO
    assert _resultado(ahora, EstadoPago.RECHAZADO).accion == ACCION_PAGO_RECHAZADO
    for invalido in (EstadoPago.PENDIENTE, EstadoPago.SIN_PAGO):
        with pytest.raises(ValueError, match="estado"):
            ResultadoPago(uuid4(), invalido, REFERENCIA, None, None, 1, 0, ahora)


# --- Trámite ------------------------------------------------------------------------------------
def _historial(entrada: Callable[..., EntradaHistorial], ahora: datetime) -> list[EntradaHistorial]:
    return [
        entrada("REPORTADO", ahora - timedelta(hours=5), estado_nuevo=EstadoSiniestro.REPORTADO),
        entrada("EVIDENCIAS_VALIDADAS", ahora - timedelta(hours=4)),
        entrada(
            "ASIGNADO",
            ahora - timedelta(hours=3),
            estado_nuevo=EstadoSiniestro.EN_EVALUACION,
            actor_nombre="Luis Mora",
        ),
        entrada(
            "APROBADO",
            ahora - timedelta(hours=1),
            estado_anterior=EstadoSiniestro.EN_EVALUACION,
            estado_nuevo=EstadoSiniestro.APROBADO,
            actor_nombre="Luis Mora",
        ),
    ]


def _estados(pasos: tuple) -> str:  # type: ignore[type-arg]
    return "".join(p.estado.value[0] for p in pasos)


def test_tras_aprobar_el_pago_es_el_paso_actual(
    entrada: Callable[..., EntradaHistorial], ahora: datetime
) -> None:
    pasos = pasos_tramite(EstadoSiniestro.APROBADO, _historial(entrada, ahora))
    assert _estados(pasos) == "CCCCA"
    assert pasos[-1].paso is PasoTramite.PAGO_INDEMNIZACION and pasos[-1].ocurrido_en is None


def test_la_confirmacion_completa_el_paso_del_pago(
    entrada: Callable[..., EntradaHistorial], ahora: datetime
) -> None:
    historial = [
        *_historial(entrada, ahora),
        entrada(ACCION_PAGO_CONFIRMADO, ahora, actor_rol="SISTEMA_PAGOS"),
    ]
    pasos = pasos_tramite(EstadoSiniestro.APROBADO, historial)
    assert _estados(pasos) == "CCCCC"
    assert pasos[-1].estado is EstadoPaso.COMPLETADO and pasos[-1].ocurrido_en == ahora


def test_un_rechazo_de_la_pasarela_deja_el_pago_por_gestionar(
    entrada: Callable[..., EntradaHistorial], ahora: datetime
) -> None:
    historial = [*_historial(entrada, ahora), entrada(ACCION_PAGO_RECHAZADO, ahora)]
    pasos = pasos_tramite(EstadoSiniestro.APROBADO, historial)
    assert _estados(pasos) == "CCCCA"  # sigue siendo el paso actual
