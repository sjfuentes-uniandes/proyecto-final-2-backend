"""Decisión pura de la verificación (HU-W31 AC1–AC4; ADR 0010): cada causa y los límites exactos
de vigencia y de revocación, sin base de datos."""

from dataclasses import fields
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from clientes.domain.consentimientos.modelos import (
    ALCANCES,
    Consentimiento,
    EstadoConsentimiento,
    esta_vigente,
)
from clientes.domain.consentimientos.verificacion import (
    CausaRechazo,
    Decision,
    LecturaVerificacion,
    SolicitudVerificacion,
    Verificacion,
    decidir,
)

CLIENTE = UUID("00000000-0000-4000-8000-000000000001")
HASTA = datetime(2027, 10, 17, 12, tzinfo=UTC)
UN_MICRO = timedelta(microseconds=1)
CUATRO = (
    "FA_PRODUCTOS_VIGENTES",
    "FA_HISTORIAL_PAGOS_12M",
    "FA_INGRESOS_AGREGADOS",
    "DA_REGISTROS_PUBLICOS",
)
SOLICITUD = SolicitudVerificacion.crear("PERFILAMIENTO_PRECIO", CUATRO)


def _consentimiento(
    estado: EstadoConsentimiento = EstadoConsentimiento.ACTIVO,
    *,
    alcances: tuple[str, ...] = CUATRO,
    vigente_hasta: datetime = HASTA,
    revocado_en: datetime | None = None,
) -> Consentimiento:
    return Consentimiento(
        id=uuid4(),
        cliente_id=CLIENTE,
        proposito="PERFILAMIENTO_PRECIO",
        alcances=alcances,
        version_texto="v1",
        idioma="es-CO",
        estado=estado,
        otorgado_en=vigente_hasta - timedelta(days=365),
        vigente_hasta=vigente_hasta,
        revocado_en=revocado_en,
        leido_en=HASTA,
    )


def _lectura(
    consentimiento: Consentimiento | None,
    ahora: datetime = HASTA - timedelta(days=1),
    otros: tuple[datetime, ...] = (),
) -> LecturaVerificacion:
    return LecturaVerificacion(CLIENTE, ahora, consentimiento, otros)


def _rechazo(verificacion: Verificacion, causa: CausaRechazo) -> None:
    """Forma estable de un rechazo: sin id ni vigencia, ningún alcance permitido y todos los
    pedidos rechazados."""
    assert verificacion.decision is Decision.RECHAZADO
    assert verificacion.causa is causa
    assert verificacion.alcances_permitidos == ()
    assert verificacion.alcances_rechazados == SOLICITUD.alcances
    assert verificacion.consentimiento_id is None
    assert verificacion.vigente_hasta is None


def test_ac1_vigente_con_los_alcances_pedidos_permite() -> None:
    vigente = _consentimiento()
    ahora = HASTA - timedelta(days=1)

    verificacion = decidir(SOLICITUD, _lectura(vigente, ahora))

    assert verificacion == Verificacion(
        cliente_id=CLIENTE,
        proposito="PERFILAMIENTO_PRECIO",
        alcances_solicitados=CUATRO,
        decision=Decision.PERMITIDO,
        causa=None,
        alcances_permitidos=CUATRO,
        alcances_rechazados=(),
        consentimiento_id=vigente.id,
        vigente_hasta=HASTA,
        consentimiento_evaluado=vigente.id,
        verificado_en=ahora,
    )


def test_ac3_un_alcance_no_incluido_se_rechaza_solo_ese() -> None:
    sin_ingresos = _consentimiento(
        alcances=tuple(a for a in CUATRO if a != "FA_INGRESOS_AGREGADOS")
    )

    verificacion = decidir(SOLICITUD, _lectura(sin_ingresos))

    assert verificacion.decision is Decision.PERMITIDO_PARCIAL
    assert verificacion.causa is None
    assert verificacion.alcances_rechazados == ("FA_INGRESOS_AGREGADOS",)
    assert verificacion.alcances_permitidos == (
        "FA_PRODUCTOS_VIGENTES",
        "FA_HISTORIAL_PAGOS_12M",
        "DA_REGISTROS_PUBLICOS",
    )
    assert verificacion.consentimiento_id == sin_ingresos.id


def test_ac3_ningun_alcance_pedido_incluido_es_parcial_y_no_un_rechazo() -> None:
    """El consentimiento está vigente: no hay causa de rechazo, solo cada acceso se rechaza."""
    solo_registros = _consentimiento(alcances=("DA_REGISTROS_PUBLICOS",))
    solicitud = SolicitudVerificacion.crear("PERFILAMIENTO_PRECIO", ["FA_INGRESOS_AGREGADOS"])

    verificacion = decidir(solicitud, _lectura(solo_registros))

    assert verificacion.decision is Decision.PERMITIDO_PARCIAL
    assert (verificacion.alcances_permitidos, verificacion.alcances_rechazados) == (
        (),
        ("FA_INGRESOS_AGREGADOS",),
    )
    assert verificacion.causa is None


def test_un_subconjunto_cubierto_es_permitido_sin_alcances_extra() -> None:
    """Solo se informan los alcances pedidos, aunque el consentimiento cubra más (minimización)."""
    solicitud = SolicitudVerificacion.crear("PERFILAMIENTO_PRECIO", ["DA_REGISTROS_PUBLICOS"])

    verificacion = decidir(solicitud, _lectura(_consentimiento()))

    assert verificacion.decision is Decision.PERMITIDO
    assert verificacion.alcances_permitidos == ("DA_REGISTROS_PUBLICOS",)


def test_la_solicitud_quita_repetidos_y_conserva_el_orden() -> None:
    solicitud = SolicitudVerificacion.crear(
        "PERFILAMIENTO_PRECIO",
        ["FA_INGRESOS_AGREGADOS", "DA_REGISTROS_PUBLICOS", "FA_INGRESOS_AGREGADOS"],
    )

    assert solicitud.alcances == ("FA_INGRESOS_AGREGADOS", "DA_REGISTROS_PUBLICOS")
    assert decidir(solicitud, _lectura(_consentimiento())).alcances_permitidos == solicitud.alcances


def test_ac2_revocado_rechaza_con_causa_revocado_y_registra_el_evaluado() -> None:
    revocado = _consentimiento(
        EstadoConsentimiento.REVOCADO, revocado_en=HASTA - timedelta(days=10)
    )

    verificacion = decidir(SOLICITUD, _lectura(revocado))

    _rechazo(verificacion, CausaRechazo.REVOCADO)
    assert verificacion.consentimiento_evaluado == revocado.id


@pytest.mark.parametrize(
    "estado",
    [EstadoConsentimiento.VENCIDO, EstadoConsentimiento.ACTIVO],
    ids=["materializado", "derivado"],
)
def test_ac2_vencido_rechaza_con_causa_vencido(estado: EstadoConsentimiento) -> None:
    """Un `ACTIVO` almacenado que ya pasó su vigencia es `VENCIDO` aunque no se haya materializado."""
    vencido = _consentimiento(estado, vigente_hasta=HASTA)

    verificacion = decidir(SOLICITUD, _lectura(vencido, ahora=HASTA + timedelta(days=1)))

    _rechazo(verificacion, CausaRechazo.VENCIDO)
    assert verificacion.consentimiento_evaluado == vencido.id


# AC4 y ADR 0010: el límite exacto `ahora == vigenteHasta` ya no es vigente.
@pytest.mark.parametrize(
    ("ahora", "decision", "causa"),
    [
        (HASTA - UN_MICRO, Decision.PERMITIDO, None),
        (HASTA, Decision.RECHAZADO, CausaRechazo.VENCIDO),
        (HASTA + UN_MICRO, Decision.RECHAZADO, CausaRechazo.VENCIDO),
    ],
    ids=["un-instante-antes", "en-el-instante", "un-instante-despues"],
)
def test_ac4_limite_exacto_de_vigencia(
    ahora: datetime, decision: Decision, causa: CausaRechazo | None
) -> None:
    verificacion = decidir(SOLICITUD, _lectura(_consentimiento(), ahora=ahora))

    assert (verificacion.decision, verificacion.causa) == (decision, causa)
    assert verificacion.verificado_en == ahora
    assert (decision is Decision.PERMITIDO) is esta_vigente(
        EstadoConsentimiento.ACTIVO, HASTA, ahora
    )


@pytest.mark.parametrize(
    "desfase", [-UN_MICRO, timedelta(0), UN_MICRO], ids=["antes", "en-el-instante", "despues"]
)
def test_revocado_rechaza_en_cualquier_instante_respecto_de_revocado_en(desfase: timedelta) -> None:
    """Manda el estado almacenado, no `revocado_en`: leída la fila ya revocada, se rechaza aunque
    el reloj de la lectura quede un instante antes, igual o después de la revocación."""
    revocado_en = HASTA - timedelta(days=30)
    revocado = _consentimiento(EstadoConsentimiento.REVOCADO, revocado_en=revocado_en)

    verificacion = decidir(SOLICITUD, _lectura(revocado, ahora=revocado_en + desfase))

    _rechazo(verificacion, CausaRechazo.REVOCADO)


def test_activo_leido_justo_antes_del_commit_de_la_revocacion_permite() -> None:
    """ADR 0010: la lectura que ve la fila aún `ACTIVO` autoriza; la ventana dura lo que dura la
    transacción de la revocación."""
    verificacion = decidir(SOLICITUD, _lectura(_consentimiento(), ahora=HASTA - timedelta(days=30)))

    assert verificacion.decision is Decision.PERMITIDO


def test_sin_consentimientos_rechaza_sin_consentimiento() -> None:
    verificacion = decidir(SOLICITUD, _lectura(None))

    _rechazo(verificacion, CausaRechazo.SIN_CONSENTIMIENTO)
    assert verificacion.consentimiento_evaluado is None


def test_con_otro_proposito_vigente_rechaza_proposito_no_autorizado() -> None:
    verificacion = decidir(SOLICITUD, _lectura(None, otros=(HASTA,)))

    _rechazo(verificacion, CausaRechazo.PROPOSITO_NO_AUTORIZADO)
    assert verificacion.consentimiento_evaluado is None


@pytest.mark.parametrize(
    ("ahora", "causa"),
    [
        (HASTA - UN_MICRO, CausaRechazo.PROPOSITO_NO_AUTORIZADO),
        (HASTA, CausaRechazo.SIN_CONSENTIMIENTO),
    ],
    ids=["otro-vigente", "otro-en-el-limite"],
)
def test_el_otro_proposito_usa_el_mismo_limite_de_vigencia(
    ahora: datetime, causa: CausaRechazo
) -> None:
    """Un `ACTIVO` de otro propósito que ya venció no autoriza nada: no es otro propósito vigente."""
    verificacion = decidir(SOLICITUD, _lectura(None, ahora=ahora, otros=(HASTA,)))

    assert verificacion.causa is causa


def test_con_uno_del_proposito_revocado_manda_su_causa_aunque_haya_otro_proposito() -> None:
    revocado = _consentimiento(EstadoConsentimiento.REVOCADO, revocado_en=HASTA - timedelta(days=9))

    verificacion = decidir(SOLICITUD, _lectura(revocado, otros=(HASTA + timedelta(days=99),)))

    assert verificacion.causa is CausaRechazo.REVOCADO


def test_la_verificacion_no_lleva_valores_financieros() -> None:
    """Solo identificadores, códigos e instantes (AC3: sin exponer datos financieros)."""
    nombres = {f.name for f in fields(Verificacion)}

    assert nombres == {
        "cliente_id",
        "proposito",
        "alcances_solicitados",
        "decision",
        "causa",
        "alcances_permitidos",
        "alcances_rechazados",
        "consentimiento_id",
        "vigente_hasta",
        "consentimiento_evaluado",
        "verificado_en",
    }
    assert set(CUATRO) == ALCANCES
