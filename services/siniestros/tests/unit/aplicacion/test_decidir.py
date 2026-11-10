"""La decisión del analista (HU-W20, DS3-12): una transacción con la idempotencia, el bloqueo, la
validación, la decisión inmutable, el cambio de estado, el historial y el Outbox."""

from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from siniestros.application.siniestro.decidir import (
    ACCION_APROBADO,
    ACCION_RECHAZADO,
    ACCION_SOPORTE_SOLICITADO,
    Decidir,
    ResultadoDecision,
    SolicitudAprobar,
    SolicitudRechazar,
    SolicitudSoportar,
    huella_de,
)
from siniestros.application.siniestro.errores import (
    CasoAsignadoAOtro,
    CausalInvalida,
    IdempotenciaConflicto,
    JustificacionRequerida,
    SiniestroNoEncontrado,
    SolicitudInvalida,
    TransicionNoPermitida,
    ValorFueraDeRango,
)
from siniestros.application.siniestro.vistas import Actor
from siniestros.domain.siniestro.modelos import (
    Amparo,
    Beneficiario,
    CanalSoporte,
    CausalRechazo,
    CopiaPoliza,
    EstadoSiniestro,
    Siniestro,
    SoporteSolicitable,
)

pytestmark = pytest.mark.anyio
ANALISTA = UUID("a0a0a0a0-0000-4000-8000-000000000001")
OTRO = UUID("a0a0a0a0-0000-4000-8000-000000000002")
ACTOR = Actor(str(ANALISTA), "ANALISTA_SINIESTROS", "Luis Mora")
E = EstadoSiniestro


@pytest.fixture
def decidir(repo, reloj):  # type: ignore[no-untyped-def]
    return Decidir(repo, reloj)


@pytest.fixture
def en_evaluacion(
    repo,
    siniestro: Callable[..., Siniestro],
    ahora: datetime,  # type: ignore[no-untyped-def]
) -> Siniestro:
    caso = siniestro(
        estado=E.EN_EVALUACION, analista_id=ANALISTA, asignado_en=ahora - timedelta(hours=13)
    )
    repo.agregar(caso)
    return caso


def aprobar(**cambios: Any) -> SolicitudAprobar:
    datos: dict[str, Any] = {
        "justificacion": "Daño verificado con evidencia fotográfica.",
        "evidencias_consideradas": (),
        "valor_reclamado": "2850000",
        "deducible": "350000",
        "valor_indemnizar": "2500000",
        "moneda": "COP",
    }
    datos.update(cambios)
    return SolicitudAprobar(**datos)


def rechazar(**cambios: Any) -> SolicitudRechazar:
    datos: dict[str, Any] = {
        "justificacion": "La póliza había vencido.",
        "evidencias_consideradas": (),
        "causal": "POLIZA_NO_VIGENTE",
        "explicacion_cliente": "Tu póliza venció el 4 de agosto y no fue renovada.",
    }
    datos.update(cambios)
    return SolicitudRechazar(**datos)


def soporte(**cambios: Any) -> SolicitudSoportar:
    datos: dict[str, Any] = {
        "justificacion": "Falta el certificado del taller.",
        "evidencias_consideradas": (),
        "documentos": (SoporteSolicitable.CERTIFICADO_REPARACION,),
        "mensaje_cliente": "Necesitamos el certificado de reparación del taller.",
        "canal": CanalSoporte.PUSH_Y_CORREO,
        "plazo_dias_habiles": 10,
    }
    datos.update(cambios)
    return SolicitudSoportar(**datos)


# --- AC1: aprobar -----------------------------------------------------------------------------------


async def test_aprobar_deja_todo_en_una_sola_transaccion(
    decidir: Decidir,
    repo,
    en_evaluacion: Siniestro,
    ahora: datetime,  # type: ignore[no-untyped-def]
) -> None:
    clave = uuid4()
    resultado = await decidir.ejecutar(en_evaluacion.id, ACTOR, ANALISTA, clave, aprobar())
    assert repo.transacciones == 1 and repo.bloqueados == [en_evaluacion.id]

    assert resultado.estado_nuevo is E.APROBADO
    assert resultado.referencia_pago == "IND-88-2026-0442-01"
    assert resultado.plazo_soporte_vence_en is None
    assert resultado.registrada_en == ahora

    guardado = repo.siniestros[en_evaluacion.id]
    assert guardado.estado is E.APROBADO and guardado.decidido_en == ahora
    assert (guardado.deducible, guardado.valor_indemnizar) == (
        Decimal("350000.00"),
        Decimal("2500000.00"),
    )

    (entrada,) = repo.historial
    assert (entrada.accion, entrada.estado_anterior, entrada.estado_nuevo) == (
        ACCION_APROBADO,
        E.EN_EVALUACION,
        E.APROBADO,
    )
    assert entrada.justificacion == "Daño verificado con evidencia fotográfica."
    assert (entrada.actor_id, entrada.actor_rol, entrada.actor_nombre) == (
        str(ANALISTA),
        "ANALISTA_SINIESTROS",
        "Luis Mora",
    )
    assert entrada.detalles == {
        "valorIndemnizar": "2500000.00",
        "moneda": "COP",
        "referenciaPago": "IND-88-2026-0442-01",
    }

    (decision,) = repo.decisiones
    assert (decision.id, decision.tipo, decision.idempotency_key) == (
        resultado.decision_id,
        "APROBAR",
        clave,
    )
    assert decision.detalle["liquidacion"] == {
        "valorReclamado": "2850000.00",
        "deducible": "350000.00",
        "valorIndemnizar": "2500000.00",
        "moneda": "COP",
    }
    # Una sola fila del Outbox: SiniestroAprobado.
    assert repo.eventos == [
        ("SiniestroAprobado", en_evaluacion.id, "IND-88-2026-0442-01", ANALISTA, ahora)
    ]
    assert repo.idempotencia[f"decision:{clave}"].respuesta == resultado.a_json()


async def test_repetir_con_la_misma_clave_devuelve_lo_mismo_sin_escribir_nada(
    decidir: Decidir,
    repo,
    en_evaluacion: Siniestro,  # type: ignore[no-untyped-def]
) -> None:
    clave = uuid4()
    primero = await decidir.ejecutar(en_evaluacion.id, ACTOR, ANALISTA, clave, aprobar())
    escrito = (len(repo.historial), len(repo.eventos), len(repo.decisiones), len(repo.bloqueados))
    segundo = await decidir.ejecutar(en_evaluacion.id, ACTOR, ANALISTA, clave, aprobar())
    assert segundo == primero
    assert (len(repo.historial), len(repo.eventos), len(repo.decisiones)) == escrito[:3]
    # La repetición ni siquiera bloquea ni relee el siniestro.
    assert len(repo.bloqueados) == escrito[3]


async def test_la_repeticion_no_depende_de_como_se_escribieron_los_montos_ni_el_orden(
    decidir: Decidir,
    repo,
    en_evaluacion: Siniestro,
    evidencia,  # type: ignore[no-untyped-def]
) -> None:
    primera, segunda = (
        evidencia(siniestro_id=en_evaluacion.id),
        evidencia(siniestro_id=en_evaluacion.id),
    )
    repo.agregar(en_evaluacion, primera, segunda)
    e1, e2 = primera.id, segunda.id
    clave = uuid4()
    await decidir.ejecutar(
        en_evaluacion.id, ACTOR, ANALISTA, clave, aprobar(evidencias_consideradas=(e1, e2))
    )
    mismo = aprobar(
        justificacion="  Daño verificado con evidencia fotográfica.  ",
        valor_reclamado="2850000.00",
        deducible="350000.0",
        valor_indemnizar=" 2500000 ",
        evidencias_consideradas=(e2, e1, e1),
    )
    assert (await decidir.ejecutar(en_evaluacion.id, ACTOR, ANALISTA, clave, mismo)).referencia_pago


@pytest.mark.parametrize(
    "otro",
    [
        aprobar(valor_indemnizar="2000000"),
        aprobar(justificacion="Otra razón."),
        rechazar(),
        soporte(),
    ],
)
async def test_la_misma_clave_con_otro_cuerpo_es_conflicto(
    decidir: Decidir,
    repo,
    en_evaluacion: Siniestro,
    otro,  # type: ignore[no-untyped-def]
) -> None:
    clave = uuid4()
    await decidir.ejecutar(en_evaluacion.id, ACTOR, ANALISTA, clave, aprobar())
    escrito = len(repo.historial), len(repo.eventos), len(repo.decisiones)
    with pytest.raises(IdempotenciaConflicto):
        await decidir.ejecutar(en_evaluacion.id, ACTOR, ANALISTA, clave, otro)
    assert (len(repo.historial), len(repo.eventos), len(repo.decisiones)) == escrito


async def test_la_misma_clave_con_otro_siniestro_o_actor_es_conflicto(
    decidir: Decidir,
    repo,
    en_evaluacion: Siniestro,
    siniestro: Callable[..., Siniestro],
    ahora,  # type: ignore[no-untyped-def]
) -> None:
    clave = uuid4()
    await decidir.ejecutar(en_evaluacion.id, ACTOR, ANALISTA, clave, aprobar())
    otro_caso = siniestro(
        codigo="88-2026-0443",
        estado=E.EN_EVALUACION,
        analista_id=ANALISTA,
        asignado_en=ahora - timedelta(hours=1),
    )
    repo.agregar(otro_caso)
    with pytest.raises(IdempotenciaConflicto):
        await decidir.ejecutar(otro_caso.id, ACTOR, ANALISTA, clave, aprobar())
    with pytest.raises(IdempotenciaConflicto):
        await decidir.ejecutar(
            en_evaluacion.id, Actor(str(OTRO), "ANALISTA_SINIESTROS"), OTRO, clave, aprobar()
        )


def test_la_huella_depende_de_la_operacion_el_siniestro_el_actor_y_el_cuerpo() -> None:
    siniestro_id = uuid4()
    base = huella_de(siniestro_id, "a", {"tipo": "APROBAR"})
    assert huella_de(siniestro_id, "a", {"tipo": "APROBAR"}) == base
    assert huella_de(uuid4(), "a", {"tipo": "APROBAR"}) != base
    assert huella_de(siniestro_id, "b", {"tipo": "APROBAR"}) != base
    assert huella_de(siniestro_id, "a", {"tipo": "RECHAZAR"}) != base


# --- AC2: rechazar ----------------------------------------------------------------------------------


async def test_rechazar_guarda_la_razon_y_no_habilita_pago(
    decidir: Decidir,
    repo,
    en_evaluacion: Siniestro,
    ahora: datetime,  # type: ignore[no-untyped-def]
) -> None:
    resultado = await decidir.ejecutar(en_evaluacion.id, ACTOR, ANALISTA, uuid4(), rechazar())
    assert resultado.estado_nuevo is E.RECHAZADO and resultado.referencia_pago is None
    guardado = repo.siniestros[en_evaluacion.id]
    assert guardado.estado is E.RECHAZADO and guardado.valor_indemnizar is None
    (entrada,) = repo.historial
    assert (entrada.accion, entrada.justificacion) == (ACCION_RECHAZADO, "La póliza había vencido.")
    assert entrada.detalles == {"causal": "POLIZA_NO_VIGENTE"}
    (decision,) = repo.decisiones
    assert decision.detalle == {
        "causal": "POLIZA_NO_VIGENTE",
        "explicacionCliente": "Tu póliza venció el 4 de agosto y no fue renovada.",
    }
    assert repo.eventos == [
        ("SiniestroRechazado", en_evaluacion.id, CausalRechazo.POLIZA_NO_VIGENTE, ANALISTA)
    ]


async def test_la_explicacion_al_cliente_no_llega_al_historial_ni_a_los_eventos(
    decidir: Decidir,
    repo,
    en_evaluacion: Siniestro,  # type: ignore[no-untyped-def]
) -> None:
    await decidir.ejecutar(
        en_evaluacion.id,
        ACTOR,
        ANALISTA,
        uuid4(),
        rechazar(explicacion_cliente="TEXTO-PRIVADO-PARA-EL-CLIENTE"),
    )
    fuera_de_la_decision = repr([repo.historial, repo.eventos])
    assert "TEXTO-PRIVADO" not in fuera_de_la_decision


@pytest.mark.parametrize("causal", ["", "OTRA", "poliza_no_vigente"])
async def test_rechazar_con_una_causal_fuera_de_la_lista_es_422(
    decidir: Decidir,
    repo,
    en_evaluacion: Siniestro,
    causal: str,  # type: ignore[no-untyped-def]
) -> None:
    with pytest.raises(CausalInvalida):
        await decidir.ejecutar(en_evaluacion.id, ACTOR, ANALISTA, uuid4(), rechazar(causal=causal))
    assert repo.transacciones == 0  # ni siquiera abrió la transacción


@pytest.mark.parametrize("texto", ["", "   "])
async def test_rechazar_sin_explicacion_es_422_con_su_campo(
    decidir: Decidir,
    repo,
    en_evaluacion: Siniestro,
    texto: str,  # type: ignore[no-untyped-def]
) -> None:
    with pytest.raises(SolicitudInvalida) as error:
        await decidir.ejecutar(
            en_evaluacion.id, ACTOR, ANALISTA, uuid4(), rechazar(explicacion_cliente=texto)
        )
    assert error.value.detalles == {"campos": ["explicacionCliente"]}
    assert repo.historial == [] and repo.decisiones == [] and repo.eventos == []


# --- AC3: solicitar soporte -------------------------------------------------------------------------


async def test_solicitar_soporte_pausa_el_reloj_y_fija_el_plazo(
    decidir: Decidir,
    repo,
    en_evaluacion: Siniestro,
    ahora: datetime,  # type: ignore[no-untyped-def]
) -> None:
    resultado = await decidir.ejecutar(en_evaluacion.id, ACTOR, ANALISTA, uuid4(), soporte())
    assert resultado.estado_nuevo is E.FALTA_SOPORTE and resultado.referencia_pago is None
    guardado = repo.siniestros[en_evaluacion.id]
    assert guardado.reloj_pausado and guardado.pausado_desde == ahora
    assert resultado.plazo_soporte_vence_en == guardado.plazo_soporte_vence_en
    # 2026-11-05 es jueves: 10 días hábiles después es el jueves 19, a la misma hora local.
    assert resultado.plazo_soporte_vence_en == ahora + timedelta(days=14)
    (entrada,) = repo.historial
    assert entrada.accion == ACCION_SOPORTE_SOLICITADO
    assert entrada.detalles == {
        "documentos": ["CERTIFICADO_REPARACION"],
        "canal": "PUSH_Y_CORREO",
        "plazoDiasHabiles": 10,
        "plazoVenceEn": resultado.plazo_soporte_vence_en.isoformat(),
    }
    (decision,) = repo.decisiones
    assert decision.tipo == "SOLICITAR_SOPORTE"
    assert (
        decision.detalle["mensajeCliente"] == "Necesitamos el certificado de reparación del taller."
    )
    (evento,) = repo.eventos
    assert evento[0] == "SiniestroSoporteSolicitado"
    assert evento[2] == (SoporteSolicitable.CERTIFICADO_REPARACION,)
    assert evento[5] == guardado.plazo_soporte_vence_en
    assert "taller" not in repr([repo.historial[0].detalles, repo.eventos])


@pytest.mark.parametrize(
    ("cambio", "campo"),
    [
        ({"documentos": ()}, "documentos"),
        (
            {
                "documentos": (
                    SoporteSolicitable.PERITAJE,
                    SoporteSolicitable.PERITAJE,
                )
            },
            "documentos",
        ),
        ({"mensaje_cliente": " "}, "mensajeCliente"),
        ({"plazo_dias_habiles": 7}, "plazoDiasHabiles"),
    ],
)
async def test_un_pedido_de_soporte_invalido_es_422_sin_escribir(
    decidir: Decidir,
    repo,
    en_evaluacion: Siniestro,
    cambio,
    campo: str,  # type: ignore[no-untyped-def]
) -> None:
    with pytest.raises(SolicitudInvalida) as error:
        await decidir.ejecutar(en_evaluacion.id, ACTOR, ANALISTA, uuid4(), soporte(**cambio))
    assert error.value.detalles == {"campos": [campo]}
    assert repo.transacciones == 0


# --- Justificación y evidencias ---------------------------------------------------------------------


@pytest.mark.parametrize("construir", [aprobar, rechazar, soporte])
@pytest.mark.parametrize("texto", ["", "  \n "])
async def test_la_justificacion_es_obligatoria_en_las_tres_decisiones(
    decidir: Decidir,
    repo,
    en_evaluacion: Siniestro,
    construir,
    texto: str,  # type: ignore[no-untyped-def]
) -> None:
    with pytest.raises(JustificacionRequerida):
        await decidir.ejecutar(
            en_evaluacion.id, ACTOR, ANALISTA, uuid4(), construir(justificacion=texto)
        )
    assert repo.transacciones == 0


async def test_las_evidencias_consideradas_se_guardan_y_deben_ser_del_siniestro(
    decidir: Decidir,
    repo,
    en_evaluacion: Siniestro,
    evidencia,  # type: ignore[no-untyped-def]
) -> None:
    propia = evidencia(siniestro_id=en_evaluacion.id)
    repo.agregar(en_evaluacion, propia)
    await decidir.ejecutar(
        en_evaluacion.id, ACTOR, ANALISTA, uuid4(), aprobar(evidencias_consideradas=(propia.id,))
    )
    assert repo.historial[0].evidencias_consideradas == (propia.id,)


async def test_una_evidencia_ajena_es_422_y_no_escribe_nada(
    decidir: Decidir,
    repo,
    en_evaluacion: Siniestro,
    evidencia,  # type: ignore[no-untyped-def]
) -> None:
    ajena = evidencia(siniestro_id=uuid4())
    repo.agregar(en_evaluacion, ajena)
    with pytest.raises(SolicitudInvalida) as error:
        await decidir.ejecutar(
            en_evaluacion.id,
            ACTOR,
            ANALISTA,
            uuid4(),
            rechazar(evidencias_consideradas=(ajena.id,)),
        )
    assert error.value.detalles == {"campos": ["evidenciasConsideradas"]}
    assert repo.historial == [] and repo.decisiones == [] and repo.eventos == []
    assert repo.siniestros[en_evaluacion.id] == en_evaluacion


# --- La liquidación ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cambio", "campos"),
    [
        ({"valor_indemnizar": "0"}, ["liquidacion.valorIndemnizar"]),
        ({"valor_indemnizar": "2850000.01"}, ["liquidacion.valorIndemnizar"]),
        ({"deducible": "-1"}, ["liquidacion.deducible"]),
        ({"moneda": "USD"}, ["liquidacion.moneda"]),
        (
            {"valor_reclamado": "3000001", "valor_indemnizar": "3000001"},
            ["liquidacion.valorIndemnizar"],
        ),
    ],
)
async def test_una_liquidacion_fuera_de_rango_es_422_sin_escribir(
    decidir: Decidir,
    repo,
    en_evaluacion: Siniestro,
    cambio,
    campos,  # type: ignore[no-untyped-def]
) -> None:
    with pytest.raises(ValorFueraDeRango) as error:
        await decidir.ejecutar(en_evaluacion.id, ACTOR, ANALISTA, uuid4(), aprobar(**cambio))
    assert error.value.detalles == {"campos": campos}
    assert repo.historial == [] and repo.decisiones == [] and repo.eventos == []
    assert repo.siniestros[en_evaluacion.id] == en_evaluacion


async def test_el_valor_reclamado_debe_coincidir_con_el_que_ya_consta(
    decidir: Decidir,
    repo,
    siniestro: Callable[..., Siniestro],
    ahora: datetime,  # type: ignore[no-untyped-def]
) -> None:
    caso = siniestro(
        estado=E.EN_EVALUACION,
        analista_id=ANALISTA,
        asignado_en=ahora,
        valor_reclamado=Decimal("2800000.00"),
        moneda="COP",
    )
    repo.agregar(caso)
    with pytest.raises(ValorFueraDeRango) as error:
        await decidir.ejecutar(caso.id, ACTOR, ANALISTA, uuid4(), aprobar())
    assert error.value.detalles == {"campos": ["liquidacion.valorReclamado"]}
    await decidir.ejecutar(
        caso.id, ACTOR, ANALISTA, uuid4(), aprobar(valor_reclamado="2800000", deducible="300000")
    )
    assert repo.siniestros[caso.id].estado is E.APROBADO


async def test_la_copia_sin_el_amparo_reclamado_no_se_puede_aprobar(
    decidir: Decidir,
    repo,
    siniestro: Callable[..., Siniestro],
    copia,
    ahora: datetime,  # type: ignore[no-untyped-def]
) -> None:
    caso = siniestro(
        estado=E.EN_EVALUACION,
        analista_id=ANALISTA,
        asignado_en=ahora,
        copia_poliza=copia(amparos=(Amparo("ROBO", "Robo", "1000000.00"),)),
    )
    repo.agregar(caso)
    with pytest.raises(ValorFueraDeRango):
        await decidir.ejecutar(caso.id, ACTOR, ANALISTA, uuid4(), aprobar())


async def test_sin_beneficiario_no_se_puede_ordenar_el_pago(
    decidir: Decidir,
    repo,
    siniestro: Callable[..., Siniestro],
    copia,
    ahora: datetime,  # type: ignore[no-untyped-def]
) -> None:
    caso = siniestro(
        estado=E.EN_EVALUACION,
        analista_id=ANALISTA,
        asignado_en=ahora,
        copia_poliza=copia(beneficiario=None),
    )
    repo.agregar(caso)
    with pytest.raises(SolicitudInvalida) as error:
        await decidir.ejecutar(caso.id, ACTOR, ANALISTA, uuid4(), aprobar())
    assert error.value.detalles == {"campos": ["beneficiario"]}


# --- AC4 y el orden de las comprobaciones -----------------------------------------------------------


@pytest.mark.parametrize("construir", [aprobar, rechazar, soporte])
@pytest.mark.parametrize("estado", [E.APROBADO, E.RECHAZADO])
async def test_decidir_un_caso_ya_decidido_es_409_sin_tocar_nada(
    decidir: Decidir,
    repo,  # type: ignore[no-untyped-def]
    siniestro: Callable[..., Siniestro],
    ahora: datetime,
    construir,  # type: ignore[no-untyped-def]
    estado: EstadoSiniestro,
) -> None:
    extra: dict[str, Any] = {"valor_indemnizar": Decimal("100.00")} if estado is E.APROBADO else {}
    caso = siniestro(
        estado=estado, analista_id=ANALISTA, asignado_en=ahora, decidido_en=ahora, **extra
    )
    repo.agregar(caso)
    with pytest.raises(TransicionNoPermitida) as error:
        await decidir.ejecutar(caso.id, ACTOR, ANALISTA, uuid4(), construir())
    assert error.value.estado_http == 409
    assert error.value.detalles == {"estadoActual": estado.value}
    assert repo.siniestros[caso.id] == caso
    assert repo.historial == [] and repo.decisiones == [] and repo.eventos == []


@pytest.mark.parametrize("estado", [E.REPORTADO, E.FALTA_SOPORTE])
async def test_un_caso_sin_analista_o_esperando_soporte_no_se_decide(
    decidir: Decidir,
    repo,  # type: ignore[no-untyped-def]
    siniestro: Callable[..., Siniestro],
    ahora: datetime,
    estado: EstadoSiniestro,
) -> None:
    extra: dict[str, Any] = (
        {
            "analista_id": ANALISTA,
            "asignado_en": ahora,
            "pausado_desde": ahora,
            "plazo_soporte_vence_en": ahora + timedelta(days=7),
        }
        if estado is E.FALTA_SOPORTE
        else {}
    )
    caso = siniestro(estado=estado, **extra)
    repo.agregar(caso)
    with pytest.raises(TransicionNoPermitida) as error:
        await decidir.ejecutar(caso.id, ACTOR, ANALISTA, uuid4(), aprobar())
    assert error.value.detalles == {"estadoActual": estado.value}


async def test_un_analista_que_no_es_el_asignado_es_409(
    decidir: Decidir,
    repo,
    en_evaluacion: Siniestro,  # type: ignore[no-untyped-def]
) -> None:
    with pytest.raises(CasoAsignadoAOtro) as error:
        await decidir.ejecutar(
            en_evaluacion.id, Actor(str(OTRO), "ANALISTA_SINIESTROS"), OTRO, uuid4(), aprobar()
        )
    assert (error.value.estado_http, error.value.codigo) == (409, "CASO_ASIGNADO_A_OTRO")
    assert repo.historial == [] and repo.eventos == []


async def test_la_transicion_se_comprueba_antes_que_el_analista(
    decidir: Decidir,
    repo,
    siniestro: Callable[..., Siniestro],
    ahora: datetime,  # type: ignore[no-untyped-def]
) -> None:
    decidido = siniestro(
        estado=E.RECHAZADO, analista_id=ANALISTA, asignado_en=ahora, decidido_en=ahora
    )
    repo.agregar(decidido)
    with pytest.raises(TransicionNoPermitida):
        await decidir.ejecutar(
            decidido.id, Actor(str(OTRO), "ANALISTA_SINIESTROS"), OTRO, uuid4(), aprobar()
        )


async def test_un_siniestro_inexistente_es_404(decidir: Decidir) -> None:
    with pytest.raises(SiniestroNoEncontrado):
        await decidir.ejecutar(uuid4(), ACTOR, ANALISTA, uuid4(), aprobar())


async def test_el_contenido_invalido_se_rechaza_antes_que_la_idempotencia(
    decidir: Decidir,
    repo,
    en_evaluacion: Siniestro,  # type: ignore[no-untyped-def]
) -> None:
    clave = uuid4()
    await decidir.ejecutar(en_evaluacion.id, ACTOR, ANALISTA, clave, aprobar())
    with pytest.raises(JustificacionRequerida):
        await decidir.ejecutar(en_evaluacion.id, ACTOR, ANALISTA, clave, aprobar(justificacion=""))


# --- La carrera: la base es la segunda barrera ------------------------------------------------------


async def test_una_decision_que_pierde_la_carrera_es_409_y_no_deja_nada_escrito(
    decidir: Decidir,
    repo,
    en_evaluacion: Siniestro,  # type: ignore[no-untyped-def]
) -> None:
    repo.decision_perdida_en_carrera = True
    clave = uuid4()
    with pytest.raises(TransicionNoPermitida) as error:
        await decidir.ejecutar(en_evaluacion.id, ACTOR, ANALISTA, clave, aprobar())
    # La transacción se deshizo: ni el estado, ni el historial, ni el Outbox, ni la clave.
    assert error.value.detalles == {"estadoActual": "EN_EVALUACION"}
    assert repo.siniestros[en_evaluacion.id] == en_evaluacion
    assert repo.historial == [] and repo.eventos == [] and repo.decisiones == []
    assert f"decision:{clave}" not in repo.idempotencia


async def test_veinte_decisiones_con_claves_distintas_dejan_una_sola_decision_final(
    decidir: Decidir,
    repo,
    en_evaluacion: Siniestro,  # type: ignore[no-untyped-def]
) -> None:
    import asyncio

    pedidos = [aprobar() if i % 2 else rechazar() for i in range(20)]
    resultados = await asyncio.gather(
        *(decidir.ejecutar(en_evaluacion.id, ACTOR, ANALISTA, uuid4(), p) for p in pedidos),
        return_exceptions=True,
    )
    # El repositorio falso no serializa por sí mismo: la primera escribe y las demás ven el estado
    # nuevo o chocan con la restricción única. En ambos casos, una sola decisión final.
    exitosas = [r for r in resultados if isinstance(r, ResultadoDecision)]
    assert len(exitosas) >= 1
    assert len([d for d in repo.decisiones if d.tipo in ("APROBAR", "RECHAZAR")]) == 1
    assert all(isinstance(r, (ResultadoDecision, TransicionNoPermitida)) for r in resultados)


# --- Resultado serializable -------------------------------------------------------------------------


def test_el_resultado_se_guarda_y_se_lee_igual(ahora: datetime) -> None:
    para_aprobar = ResultadoDecision(
        uuid4(), E.APROBADO, uuid4(), "IND-88-2026-0442-01", None, ahora
    )
    para_soporte = ResultadoDecision(
        uuid4(), E.FALTA_SOPORTE, uuid4(), None, ahora + timedelta(days=14), ahora
    )
    for resultado in (para_aprobar, para_soporte):
        assert ResultadoDecision.de_json(resultado.a_json()) == resultado
    assert set(para_aprobar.a_json()) == {
        "siniestroId",
        "estadoNuevo",
        "decisionId",
        "referenciaPago",
        "plazoSoporteVenceEn",
        "registradaEn",
    }


def test_la_fabrica_de_prueba_trae_beneficiario(copia: Callable[..., CopiaPoliza]) -> None:
    assert copia().beneficiario == Beneficiario("ben-sint-004", "****1004")


def test_un_error_que_no_es_de_dominio_no_se_traduce() -> None:
    from siniestros.application.siniestro.decidir import _traducir

    with pytest.raises(ValueError, match="otro"):
        _traducir(ValueError("otro"))
