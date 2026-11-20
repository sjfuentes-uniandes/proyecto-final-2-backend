"""El detalle después de la decisión (HU-W20): liquidación, soportes solicitados y trámite."""

from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from siniestros.application.siniestro.detalle import ConsultarDetalle, soportes_solicitados
from siniestros.application.siniestro.vistas import Actor, LiquidacionAprobada
from siniestros.domain.siniestro.cobertura import soportes_pendientes
from siniestros.domain.siniestro.modelos import (
    EntradaHistorial,
    EstadoPaso,
    EstadoSiniestro,
    EstadoSoporte,
    PasoTramite,
    Siniestro,
    TipoEvidencia,
    TipoSoporte,
)
from siniestros.domain.siniestro.transiciones import normalizar_monto

pytestmark = pytest.mark.anyio
ANALISTA = UUID("a0a0a0a0-0000-4000-8000-000000000001")
ACTOR = Actor(str(ANALISTA), "ANALISTA_SINIESTROS", "Luis Mora")
E = EstadoSiniestro


@pytest.fixture
def consultar(repo, almacen, soportes, reloj, plazos):  # type: ignore[no-untyped-def]
    return ConsultarDetalle(repo, almacen, soportes, reloj, plazos)


def en_soporte(siniestro: Callable[..., Siniestro], ahora: datetime) -> Siniestro:
    return siniestro(
        estado=E.FALTA_SOPORTE,
        analista_id=ANALISTA,
        asignado_en=ahora - timedelta(hours=13),
        pausado_desde=ahora,
        plazo_soporte_vence_en=ahora + timedelta(days=14),
    )


# --- Soportes solicitados -------------------------------------------------------------------------


def test_los_soportes_pedidos_salen_solicitados_aunque_el_amparo_no_los_exija() -> None:
    pendientes = soportes_pendientes(
        [TipoSoporte.FACTURA_COMPRA, TipoSoporte.CERTIFICADO_REPARACION],
        [],
        [TipoSoporte.CERTIFICADO_REPARACION, TipoSoporte.PERITAJE],
    )
    assert [(p.tipo, p.estado) for p in pendientes] == [
        (TipoSoporte.FACTURA_COMPRA, EstadoSoporte.FALTA),
        (TipoSoporte.CERTIFICADO_REPARACION, EstadoSoporte.SOLICITADO),
        (TipoSoporte.PERITAJE, EstadoSoporte.SOLICITADO),
    ]


def test_sin_pedidos_los_pendientes_son_los_de_w19() -> None:
    pendientes = soportes_pendientes([TipoSoporte.FACTURA_COMPRA], [])
    assert [(p.tipo, p.estado) for p in pendientes] == [
        (TipoSoporte.FACTURA_COMPRA, EstadoSoporte.FALTA)
    ]


def test_un_soporte_pedido_que_ya_esta_cargado_no_queda_pendiente(
    evidencia: Callable[..., object],
) -> None:
    cargado = evidencia(tipo=TipoEvidencia.SOPORTE, soporte_tipo=TipoSoporte.PERITAJE)  # type: ignore[operator]
    assert soportes_pendientes([], [cargado], [TipoSoporte.PERITAJE]) == ()  # type: ignore[list-item]


def test_los_soportes_solicitados_salen_del_ultimo_pedido_mientras_el_caso_espera(
    siniestro: Callable[..., Siniestro],
    entrada: Callable[..., EntradaHistorial],
    ahora: datetime,
) -> None:
    caso = en_soporte(siniestro, ahora)
    viejo = entrada(
        "SOPORTE_SOLICITADO", ahora - timedelta(days=30), detalles={"documentos": ["PERITAJE"]}
    )
    nuevo = entrada(
        "SOPORTE_SOLICITADO",
        ahora - timedelta(hours=1),
        detalles={"documentos": ["DENUNCIA_AUTORIDAD", "PERITAJE"]},
    )
    asignado = entrada("ASIGNADO", ahora - timedelta(days=40))
    assert soportes_solicitados(caso, (viejo, nuevo, asignado)) == (
        TipoSoporte.DENUNCIA_AUTORIDAD,
        TipoSoporte.PERITAJE,
    )
    assert soportes_solicitados(caso, (asignado,)) == ()
    assert soportes_solicitados(caso, ()) == ()


def test_fuera_de_falta_de_soporte_no_hay_soportes_solicitados(
    siniestro: Callable[..., Siniestro],
    entrada: Callable[..., EntradaHistorial],
    ahora: datetime,
) -> None:
    pedido = entrada("SOPORTE_SOLICITADO", ahora, detalles={"documentos": ["PERITAJE"]})
    caso = siniestro(estado=E.EN_EVALUACION, analista_id=ANALISTA, asignado_en=ahora)
    assert soportes_solicitados(caso, (pedido,)) == ()


async def test_el_detalle_marca_como_solicitados_los_documentos_del_pedido(
    consultar: ConsultarDetalle,
    repo,  # type: ignore[no-untyped-def]
    siniestro: Callable[..., Siniestro],
    entrada: Callable[..., EntradaHistorial],
    ahora: datetime,
) -> None:
    caso = en_soporte(siniestro, ahora)
    repo.agregar(caso)
    repo.historial.append(
        entrada(
            "SOPORTE_SOLICITADO",
            ahora,
            siniestro_id=caso.id,
            detalles={"documentos": ["CERTIFICADO_REPARACION", "PERITAJE"]},
        )
    )
    detalle = await consultar.ejecutar(caso.id, ACTOR)
    estados = {p.tipo: p.estado for p in detalle.soportes_pendientes}
    assert estados[TipoSoporte.CERTIFICADO_REPARACION] is EstadoSoporte.SOLICITADO
    assert estados[TipoSoporte.PERITAJE] is EstadoSoporte.SOLICITADO
    assert estados[TipoSoporte.FACTURA_COMPRA] is EstadoSoporte.FALTA
    # El reloj sigue pausado y el caso aún no se decidió.
    assert detalle.resumen.siniestro.reloj_pausado and detalle.liquidacion is None


# --- Liquidación y trámite ------------------------------------------------------------------------


async def test_un_caso_aprobado_trae_su_liquidacion_y_la_decision_completada(
    consultar: ConsultarDetalle,
    repo,  # type: ignore[no-untyped-def]
    siniestro: Callable[..., Siniestro],
    entrada: Callable[..., EntradaHistorial],
    ahora: datetime,
) -> None:
    caso = siniestro(
        estado=E.APROBADO,
        analista_id=ANALISTA,
        asignado_en=ahora - timedelta(hours=13),
        decidido_en=ahora,
        deducible=Decimal("350000.00"),
        valor_indemnizar=Decimal("2500000.00"),
    )
    repo.agregar(caso)
    repo.historial.append(
        entrada(
            "APROBADO",
            ahora,
            siniestro_id=caso.id,
            estado_anterior=E.EN_EVALUACION,
            estado_nuevo=E.APROBADO,
            actor_nombre="Luis Mora",
        )
    )
    liquidacion = LiquidacionAprobada(
        "COP",
        Decimal("2850000.00"),
        Decimal("350000.00"),
        Decimal("2500000.00"),
        "IND-88-2026-0442-01",
        ahora,
    )
    repo.liquidaciones[caso.id] = liquidacion
    detalle = await consultar.ejecutar(caso.id, ACTOR)
    assert detalle.liquidacion == liquidacion
    pasos = {p.paso: p for p in detalle.tramite}
    assert pasos[PasoTramite.DECISION].estado is EstadoPaso.COMPLETADO
    assert pasos[PasoTramite.DECISION].actor_nombre == "Luis Mora"
    # El pago es de W23: sigue sin ocurrir.
    assert pasos[PasoTramite.PAGO_INDEMNIZACION].ocurrido_en is None


# --- Montos ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        ("2500000", "2500000.00"),
        (" 2500000.5 ", "2500000.50"),
        ("0.1", "0.10"),
        ("12.340", "12.34"),
        ("-5", "-5.00"),
        ("2500000.001", "2500000.001"),
        (" abc ", "abc"),
        ("", ""),
        ("1e5", "1e5"),
    ],
)
def test_los_montos_se_normalizan_a_dos_decimales_y_lo_invalido_queda_tal_cual(
    texto: str, esperado: str
) -> None:
    assert normalizar_monto(texto) == esperado
