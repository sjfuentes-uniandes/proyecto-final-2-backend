"""UT-W10-20 a 25 con las fuentes y obligatorios del modelo candidato 2026.10.1 (04 §B.6.1)."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from cotizacion.domain.perfilamiento.modelo_riesgo import ModeloRiesgo
from cotizacion.domain.perfilamiento.modelos import (
    DecisionConsentimiento,
    Fuente,
    SolicitudFuente,
    VerificacionConsentimiento,
)
from cotizacion.domain.perfilamiento.plan_fuentes import plan_fuentes

P, H, I, D = (
    Fuente.FA_PRODUCTOS_VIGENTES,
    Fuente.FA_HISTORIAL_PAGOS_12M,
    Fuente.FA_INGRESOS_AGREGADOS,
    Fuente.DA_REGISTROS_PUBLICOS,
)
CAMPOS = {
    P: ("antiguedad_productos_anios", "cuota_mensual_obligaciones", "entidades_con_deuda"),
    H: ("moras_12m",),
    I: ("ingreso_mensual_estimado",),
    D: ("coincidencias_listas_restrictivas",),
}
MODELO = ModeloRiesgo(
    version="2026.10.1",
    hash="sha256:candidato",
    producto_id="vida-hipotecario",
    proposito="PERFILAMIENTO_PRECIO",
    fuentes=CAMPOS,
    declarados=("edad", "plazo_credito_meses", "suma_asegurada"),
    obligatorios=frozenset(
        {"edad", "plazo_credito_meses", "moras_12m", "cuota_mensual_obligaciones"}
    ),
    derivados={},
    factores=(),
    ajustes=(),
    niveles=(),
    penalizacion_no_autorizada=Decimal("0.05"),
    penalizacion_no_obtenida=Decimal("0.15"),
)


def _verificacion(
    decision: DecisionConsentimiento, permitidas: set[Fuente], causa: str | None = None
) -> VerificacionConsentimiento:
    return VerificacionConsentimiento(
        decision=decision,
        consentimiento_id=None if decision is DecisionConsentimiento.RECHAZADO else UUID(int=1),
        vigente_hasta=datetime(2027, 10, 1, tzinfo=UTC),
        alcances_permitidos=frozenset(permitidas),
        causa=causa,
    )


def test_ut_w10_20_permitido_las_cuatro_fuentes() -> None:
    plan = plan_fuentes(MODELO, _verificacion(DecisionConsentimiento.PERMITIDO, {P, H, I, D}))
    assert plan.a_consultar == tuple(SolicitudFuente(f, CAMPOS[f]) for f in (P, H, I, D))
    assert (plan.no_autorizadas, plan.no_consultadas) == ((), ())


def test_ut_w10_21_parcial_sin_ingresos_c02() -> None:
    plan = plan_fuentes(MODELO, _verificacion(DecisionConsentimiento.PERMITIDO_PARCIAL, {P, H, D}))
    assert [s.fuente for s in plan.a_consultar] == [P, H, D]
    assert plan.no_autorizadas == (I,)
    assert plan.no_consultadas == ()


@pytest.mark.parametrize("permitidas", [set(), {P, H, I, D}])
def test_ut_w10_22_rechazado_c08_ignora_los_alcances(permitidas: set[Fuente]) -> None:
    plan = plan_fuentes(
        MODELO, _verificacion(DecisionConsentimiento.RECHAZADO, permitidas, "REVOCADO")
    )
    assert plan.a_consultar == ()
    assert plan.no_autorizadas == (P, H, I, D)
    assert plan.no_consultadas == ()


def test_ut_w10_23_sin_historial_c16_no_consulta_nada() -> None:
    plan = plan_fuentes(MODELO, _verificacion(DecisionConsentimiento.PERMITIDO_PARCIAL, {P, I, D}))
    assert plan.a_consultar == ()
    assert plan.no_autorizadas == (H,)
    assert plan.no_consultadas == (P, I, D)


def test_ut_w10_24_solo_datos_abiertos_c17() -> None:
    plan = plan_fuentes(MODELO, _verificacion(DecisionConsentimiento.PERMITIDO_PARCIAL, {D}))
    assert plan.a_consultar == ()
    assert plan.no_autorizadas == (P, H, I)
    assert plan.no_consultadas == (D,)


def test_ut_w10_25_siempre_en_el_orden_del_modelo() -> None:
    invertido = ModeloRiesgo(
        **{
            **{f: getattr(MODELO, f) for f in MODELO.__slots__},  # type: ignore[attr-defined]
            "fuentes": {D: CAMPOS[D], I: CAMPOS[I], H: CAMPOS[H], P: CAMPOS[P]},
        }
    )
    verificacion = _verificacion(DecisionConsentimiento.PERMITIDO_PARCIAL, {H, P, D})
    plan = plan_fuentes(invertido, verificacion)
    assert [s.fuente for s in plan.a_consultar] == [D, H, P]
    assert plan.no_autorizadas == (I,)
    sin_ingresos_ni_productos = plan_fuentes(
        invertido, _verificacion(DecisionConsentimiento.PERMITIDO_PARCIAL, {D, H})
    )
    assert sin_ingresos_ni_productos.no_autorizadas == (I, P)
    assert sin_ingresos_ni_productos.no_consultadas == (D, H)
