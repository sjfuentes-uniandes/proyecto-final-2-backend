"""Cambios de estado, decisión del analista y código del siniestro (DS3-02, DS3-11, DS3-12, DS3-13).

UT-W20-01: la tabla de transiciones con **cada par** (estado × acción), válido e inválido.
UT-W20-02: validaciones de justificación, causal, documentos, valor (0, igual al reclamado, suma
asegurada + 1) y días hábiles (cruce de fin de semana).
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from itertools import product
from typing import Any
from uuid import UUID

import pytest
from siniestros.domain.siniestro.errores import (
    CasoAsignadoAOtro,
    CausalInvalida,
    DecisionInvalida,
    JustificacionRequerida,
    SiniestroInvalido,
    TransicionInvalida,
    ValorFueraDeRango,
)
from siniestros.domain.siniestro.modelos import (
    AccionSiniestro,
    CausalRechazo,
    EstadoSiniestro,
    Siniestro,
)
from siniestros.domain.siniestro.plazos import tiempo_restante
from siniestros.domain.siniestro.transiciones import (
    DESTINO_NOMINAL,
    TRANSICIONES,
    Liquidacion,
    aprobar,
    asignar,
    codigo_siniestro,
    estado_destino,
    plazo_soporte_vencido,
    rechazar,
    referencia_pago,
    solicitar_soporte,
    sumar_dias_habiles,
    validar_causal,
    validar_documentos,
    validar_justificacion,
    validar_liquidacion,
    validar_plazo,
    validar_texto_cliente,
    vencer_plazo_soporte,
    verificar_decision,
)

ANALISTA = UUID("a0a0a0a0-0000-4000-8000-000000000001")
OTRO = UUID("a0a0a0a0-0000-4000-8000-000000000002")
E = EstadoSiniestro
A = AccionSiniestro
HORA = timedelta(hours=1)
DIA = timedelta(days=1)


@pytest.fixture
def en_estado(siniestro: Callable[..., Siniestro], ahora: datetime) -> Callable[..., Siniestro]:
    """Un siniestro en el estado pedido, con lo que su estado exige."""

    def fabricar(estado: EstadoSiniestro, **cambios: Any) -> Siniestro:
        datos: dict[str, Any] = {"estado": estado}
        if estado is not E.REPORTADO and estado not in (E.APROBADO, E.RECHAZADO):
            datos["analista_id"] = ANALISTA
            datos["asignado_en"] = ahora - 13 * HORA
        if estado is E.FALTA_SOPORTE:
            datos["pausado_desde"] = ahora - 4 * HORA
            datos["plazo_soporte_vence_en"] = ahora + 6 * DIA
        if estado in (E.APROBADO, E.RECHAZADO):
            datos["analista_id"] = ANALISTA
            datos["asignado_en"] = ahora - 13 * HORA
            datos["decidido_en"] = ahora - HORA
        if estado is E.APROBADO:
            datos["valor_indemnizar"] = Decimal("2500000.00")
        datos.update(cambios)
        return siniestro(**datos)

    return fabricar


# --- UT-W20-01: la tabla de transiciones -----------------------------------------------------------

VALIDOS = {
    (E.REPORTADO, A.ASIGNAR): E.EN_EVALUACION,
    (E.EN_EVALUACION, A.APROBAR): E.APROBADO,
    (E.EN_EVALUACION, A.RECHAZAR): E.RECHAZADO,
    (E.EN_EVALUACION, A.SOLICITAR_SOPORTE): E.FALTA_SOPORTE,
    (E.FALTA_SOPORTE, A.VENCER_PLAZO_SOPORTE): E.EN_EVALUACION,
}
PARES = list(product(E, A))


def test_hay_25_pares_y_solo_5_son_validos() -> None:
    assert len(PARES) == 25
    assert dict(TRANSICIONES) == VALIDOS


@pytest.mark.parametrize(("estado", "accion"), PARES)
def test_cada_par_estado_accion_es_valido_o_invalido(estado: E, accion: A) -> None:
    esperado = VALIDOS.get((estado, accion))
    if esperado is None:
        with pytest.raises(TransicionInvalida) as error:
            estado_destino(estado, accion)
        # Informa el estado vigente y a dónde llevaría la acción; nada más.
        assert error.value.desde is estado
        assert error.value.hacia == DESTINO_NOMINAL[accion]
    else:
        assert estado_destino(estado, accion) is esperado


@pytest.mark.parametrize("estado", list(E))
def test_asignar_coincide_con_la_tabla(
    en_estado: Callable[..., Siniestro], ahora: datetime, estado: E
) -> None:
    caso = en_estado(estado)
    if (estado, A.ASIGNAR) in VALIDOS:
        assert asignar(caso, ANALISTA, ahora).estado is VALIDOS[(estado, A.ASIGNAR)]
    else:
        with pytest.raises(TransicionInvalida):
            asignar(caso, ANALISTA, ahora)


def test_los_terminales_no_admiten_ninguna_accion() -> None:
    for estado, accion in product((E.APROBADO, E.RECHAZADO), A):
        assert (estado, accion) not in TRANSICIONES


def test_la_transicion_se_comprueba_antes_que_el_analista(
    en_estado: Callable[..., Siniestro],
) -> None:
    # Un caso ya decidido es siempre TRANSICION, aunque lo pida otro analista.
    decidido = en_estado(E.APROBADO)
    with pytest.raises(TransicionInvalida):
        verificar_decision(decidido, A.APROBAR, OTRO)
    with pytest.raises(TransicionInvalida):
        verificar_decision(en_estado(E.REPORTADO), A.APROBAR, ANALISTA)
    with pytest.raises(TransicionInvalida):
        verificar_decision(en_estado(E.FALTA_SOPORTE), A.RECHAZAR, ANALISTA)


def test_solo_decide_el_analista_asignado(en_estado: Callable[..., Siniestro]) -> None:
    caso = en_estado(E.EN_EVALUACION)
    assert verificar_decision(caso, A.APROBAR, ANALISTA) is E.APROBADO
    assert verificar_decision(caso, A.SOLICITAR_SOPORTE, ANALISTA) is E.FALTA_SOPORTE
    with pytest.raises(CasoAsignadoAOtro):
        verificar_decision(caso, A.RECHAZAR, OTRO)


# --- Tomar el siguiente (W19) ----------------------------------------------------------------------


def test_tomar_un_reportado_lo_pasa_a_evaluacion(
    siniestro: Callable[..., Siniestro], ahora: datetime
) -> None:
    caso = siniestro()
    asignado = asignar(caso, ANALISTA, ahora)
    assert asignado.estado is E.EN_EVALUACION
    assert asignado.analista_id == ANALISTA
    assert asignado.asignado_en == ahora
    assert asignado.version == caso.version + 1
    # El original no cambia (inmutable) y lo demás se conserva.
    assert caso.estado is E.REPORTADO
    assert asignado.codigo == caso.codigo
    assert asignado.reportado_en == caso.reportado_en


@pytest.mark.parametrize("estado", [E.EN_EVALUACION, E.FALTA_SOPORTE, E.APROBADO])
def test_solo_se_asigna_un_reportado(
    en_estado: Callable[..., Siniestro], ahora: datetime, estado: E
) -> None:
    with pytest.raises(TransicionInvalida) as error:
        asignar(en_estado(estado), ANALISTA, ahora)
    assert error.value.desde is estado
    assert str(error.value) == f"{estado} -> {E.EN_EVALUACION}"


def test_asignar_exige_hora_con_zona(
    siniestro: Callable[..., Siniestro], ingenuo: datetime
) -> None:
    with pytest.raises(SiniestroInvalido, match="ahora"):
        asignar(siniestro(), ANALISTA, ingenuo)


# --- Aprobar y rechazar ----------------------------------------------------------------------------

LIQUIDACION = Liquidacion(Decimal("2850000.00"), Decimal("350000.00"), Decimal("2500000.00"), "COP")


def test_aprobar_guarda_la_liquidacion_y_detiene_el_reloj(
    en_estado: Callable[..., Siniestro], ahora: datetime
) -> None:
    caso = en_estado(E.EN_EVALUACION)
    aprobado = aprobar(caso, LIQUIDACION, ahora)
    assert aprobado.estado is E.APROBADO
    assert aprobado.decidido_en == ahora
    assert aprobado.deducible == Decimal("350000.00")
    assert aprobado.valor_indemnizar == Decimal("2500000.00")
    assert aprobado.version == caso.version + 1
    # El mismo analista y sin pausa; el reloj ya no avanza.
    assert aprobado.analista_id == ANALISTA
    assert tiempo_restante(aprobado, ahora) == tiempo_restante(aprobado, ahora + 100 * HORA)
    assert caso.estado is E.EN_EVALUACION and caso.valor_indemnizar is None


def test_rechazar_detiene_el_reloj_y_no_deja_liquidacion(
    en_estado: Callable[..., Siniestro], ahora: datetime
) -> None:
    rechazado = rechazar(en_estado(E.EN_EVALUACION), ahora)
    assert rechazado.estado is E.RECHAZADO
    assert rechazado.decidido_en == ahora
    assert rechazado.valor_indemnizar is None and rechazado.deducible is None
    assert tiempo_restante(rechazado, ahora) == tiempo_restante(rechazado, ahora + 50 * HORA)


@pytest.mark.parametrize("estado", [E.REPORTADO, E.FALTA_SOPORTE, E.APROBADO, E.RECHAZADO])
def test_aprobar_y_rechazar_solo_desde_evaluacion(
    en_estado: Callable[..., Siniestro], ahora: datetime, estado: E
) -> None:
    caso = en_estado(estado)
    with pytest.raises(TransicionInvalida):
        aprobar(caso, LIQUIDACION, ahora)
    with pytest.raises(TransicionInvalida):
        rechazar(caso, ahora)
    with pytest.raises(TransicionInvalida):
        solicitar_soporte(caso, 10, ahora)


def test_las_decisiones_exigen_hora_con_zona(
    en_estado: Callable[..., Siniestro], ingenuo: datetime
) -> None:
    caso = en_estado(E.EN_EVALUACION)
    for accion in (
        lambda: aprobar(caso, LIQUIDACION, ingenuo),
        lambda: rechazar(caso, ingenuo),
        lambda: solicitar_soporte(caso, 5, ingenuo),
        lambda: plazo_soporte_vencido(caso, ingenuo),
    ):
        with pytest.raises(SiniestroInvalido, match="ahora"):
            accion()


# --- Solicitar soporte y vencer el plazo -----------------------------------------------------------


def test_solicitar_soporte_pausa_el_reloj_y_fija_el_plazo(
    en_estado: Callable[..., Siniestro], ahora: datetime
) -> None:
    caso = en_estado(E.EN_EVALUACION)
    pedido = solicitar_soporte(caso, 10, ahora)
    assert pedido.estado is E.FALTA_SOPORTE
    assert pedido.reloj_pausado
    assert pedido.pausado_desde == ahora
    assert pedido.plazo_soporte_vence_en == sumar_dias_habiles(ahora, 10)
    assert pedido.analista_id == ANALISTA
    assert pedido.version == caso.version + 1
    # Pausado: el tiempo restante no baja.
    assert tiempo_restante(pedido, ahora + 30 * HORA) == tiempo_restante(pedido, ahora)


@pytest.mark.parametrize("dias", [0, 1, 7, 20, -5])
def test_el_plazo_es_de_5_10_o_15_dias_habiles(
    en_estado: Callable[..., Siniestro], ahora: datetime, dias: int
) -> None:
    with pytest.raises(DecisionInvalida) as error:
        solicitar_soporte(en_estado(E.EN_EVALUACION), dias, ahora)
    assert error.value.campos == ("plazoDiasHabiles",)
    with pytest.raises(DecisionInvalida):
        validar_plazo(dias)


@pytest.mark.parametrize("dias", [5, 10, 15])
def test_los_plazos_validos(dias: int) -> None:
    assert validar_plazo(dias) == dias


def test_vencer_el_plazo_devuelve_el_caso_al_mismo_analista_con_el_reloj_reanudado(
    en_estado: Callable[..., Siniestro], ahora: datetime
) -> None:
    pedido = solicitar_soporte(en_estado(E.EN_EVALUACION, pausa_acumulada_s=600), 5, ahora)
    vencido = pedido.plazo_soporte_vence_en
    assert vencido is not None
    despues = vencido + 90 * timedelta(minutes=1)
    assert plazo_soporte_vencido(pedido, despues)
    devuelto = vencer_plazo_soporte(pedido, despues)
    assert devuelto.estado is E.EN_EVALUACION
    assert devuelto.analista_id == ANALISTA and devuelto.asignado_en == pedido.asignado_en
    assert devuelto.pausado_desde is None and devuelto.plazo_soporte_vence_en is None
    pausa = int((despues - ahora).total_seconds())
    assert devuelto.pausa_acumulada_s == 600 + pausa
    assert devuelto.version == pedido.version + 1
    # Queda lo que había al pausar, no 48 h nuevas: la pausa no se cuenta.
    assert tiempo_restante(devuelto, despues) == tiempo_restante(pedido, despues)
    assert tiempo_restante(devuelto, despues + 2 * HORA) == tiempo_restante(devuelto, despues) - 120


def test_el_plazo_vence_cuando_ahora_es_posterior_no_igual(
    en_estado: Callable[..., Siniestro], ahora: datetime
) -> None:
    pedido = solicitar_soporte(en_estado(E.EN_EVALUACION), 5, ahora)
    limite = pedido.plazo_soporte_vence_en
    assert limite is not None
    assert not plazo_soporte_vencido(pedido, limite - timedelta(seconds=1))
    assert not plazo_soporte_vencido(pedido, limite)
    assert plazo_soporte_vencido(pedido, limite + timedelta(seconds=1))
    with pytest.raises(SiniestroInvalido, match="no ha vencido"):
        vencer_plazo_soporte(pedido, limite)


def test_solo_un_caso_en_falta_de_soporte_vence(en_estado: Callable[..., Siniestro]) -> None:
    tarde = datetime(2030, 1, 1, tzinfo=UTC)
    for estado in (E.REPORTADO, E.EN_EVALUACION, E.APROBADO, E.RECHAZADO):
        caso = en_estado(estado)
        assert not plazo_soporte_vencido(caso, tarde)
        with pytest.raises(TransicionInvalida):
            vencer_plazo_soporte(caso, tarde)


def test_una_pausa_con_reloj_atrasado_no_resta(
    en_estado: Callable[..., Siniestro], ahora: datetime
) -> None:
    # `ahora` anterior a `pausado_desde` (reloj atrasado): la pausa suma 0, no un negativo.
    caso = en_estado(
        E.FALTA_SOPORTE,
        pausado_desde=ahora + HORA,
        plazo_soporte_vence_en=ahora - HORA,
    )
    assert vencer_plazo_soporte(caso, ahora).pausa_acumulada_s == caso.pausa_acumulada_s


# --- UT-W20-02: justificación, causal, documentos y textos al cliente -------------------------------


@pytest.mark.parametrize("texto", ["", " ", "\n\t  "])
def test_la_justificacion_es_obligatoria(texto: str) -> None:
    with pytest.raises(JustificacionRequerida):
        validar_justificacion(texto)


def test_la_justificacion_se_guarda_sin_espacios_en_los_extremos() -> None:
    assert validar_justificacion("  Daño verificado.\n") == "Daño verificado."


@pytest.mark.parametrize("causal", list(CausalRechazo))
def test_las_siete_causales_son_validas(causal: CausalRechazo) -> None:
    assert validar_causal(f" {causal.value} ") is causal


@pytest.mark.parametrize("causal", ["", "OTRA", "poliza_no_vigente", "POLIZA NO VIGENTE"])
def test_otra_causal_es_invalida(causal: str) -> None:
    with pytest.raises(CausalInvalida):
        validar_causal(causal)


def test_las_causales_son_las_de_ds3_12() -> None:
    assert [c.value for c in CausalRechazo] == [
        "POLIZA_NO_VIGENTE",
        "EVENTO_NO_CUBIERTO",
        "PERIODO_DE_CARENCIA",
        "AVISO_EXTEMPORANEO",
        "PRIMAS_EN_MORA",
        "EVIDENCIA_INCONSISTENTE",
        "EXCLUSION_CONTRACTUAL",
    ]


@pytest.mark.parametrize("texto", ["", "   "])
def test_el_texto_al_cliente_no_puede_ir_vacio(texto: str) -> None:
    with pytest.raises(DecisionInvalida) as error:
        validar_texto_cliente("explicacionCliente", texto)
    assert error.value.campos == ("explicacionCliente",)


def test_el_texto_al_cliente_se_guarda_limpio() -> None:
    assert validar_texto_cliente("mensajeCliente", " Hola \n") == "Hola"


def test_los_documentos_son_uno_o_mas_sin_repetidos() -> None:
    assert validar_documentos(["PERITAJE", "DENUNCIA_AUTORIDAD"]) == (
        "PERITAJE",
        "DENUNCIA_AUTORIDAD",
    )
    for malos in ([], ["PERITAJE", "PERITAJE"]):
        with pytest.raises(DecisionInvalida) as error:
            validar_documentos(malos)
        assert error.value.campos == ("documentos",)


# --- UT-W20-02: la liquidación ---------------------------------------------------------------------


def liquidar(**cambios: Any) -> Liquidacion:
    datos: dict[str, Any] = {
        "valor_reclamado": "2850000",
        "deducible": "350000",
        "valor_indemnizar": "2500000",
        "moneda": "COP",
        "suma_asegurada": "3000000.00",
        "moneda_poliza": "COP",
    }
    datos.update(cambios)
    return validar_liquidacion(**datos)


def test_una_liquidacion_valida_normaliza_a_dos_decimales() -> None:
    assert liquidar() == LIQUIDACION
    assert liquidar(valor_indemnizar="2500000.5").valor_indemnizar == Decimal("2500000.50")
    # Ceros de más se aceptan: es el mismo valor.
    assert liquidar(valor_indemnizar="2500000.000").valor_indemnizar == Decimal("2500000.00")


@pytest.mark.parametrize(
    ("indemnizar", "ok"),
    [
        ("0", False),
        ("0.00", False),
        ("-1", False),
        ("0.01", True),
        ("2849999.99", True),
        ("2850000", True),  # igual al reclamado
        ("2850000.01", False),  # reclamado + 1 centavo
    ],
)
def test_el_valor_a_indemnizar_esta_entre_cero_y_el_reclamado(indemnizar: str, ok: bool) -> None:
    if ok:
        assert liquidar(valor_indemnizar=indemnizar)
    else:
        with pytest.raises(ValorFueraDeRango) as error:
            liquidar(valor_indemnizar=indemnizar)
        assert error.value.campos == ("liquidacion.valorIndemnizar",)


@pytest.mark.parametrize(
    ("indemnizar", "ok"),
    [("3000000", True), ("3000000.01", False)],
)
def test_el_valor_a_indemnizar_no_pasa_de_la_suma_asegurada(indemnizar: str, ok: bool) -> None:
    # Con el reclamado por encima de la suma asegurada, la suma es el techo.
    datos = {"valor_reclamado": "3500000", "valor_indemnizar": indemnizar}
    if ok:
        assert liquidar(**datos).valor_indemnizar == Decimal("3000000.00")
    else:
        with pytest.raises(ValorFueraDeRango) as error:
            liquidar(**datos)
        assert error.value.campos == ("liquidacion.valorIndemnizar",)


def test_la_suma_asegurada_mas_uno_es_fuera_de_rango() -> None:
    with pytest.raises(ValorFueraDeRango):
        liquidar(
            valor_reclamado="4000000",
            valor_indemnizar="3000001",
            suma_asegurada="3000000",
        )


@pytest.mark.parametrize(
    ("deducible", "ok"), [("0", True), ("2850000", True), ("2850000.01", False), ("-1", False)]
)
def test_el_deducible_esta_entre_cero_y_el_reclamado(deducible: str, ok: bool) -> None:
    if ok:
        assert liquidar(deducible=deducible).deducible == Decimal(deducible).quantize(
            Decimal("0.01")
        )
    else:
        with pytest.raises(ValorFueraDeRango) as error:
            liquidar(deducible=deducible)
        assert error.value.campos == ("liquidacion.deducible",)


def test_no_se_exige_que_reclamado_menos_deducible_sea_el_valor_a_indemnizar() -> None:
    assert liquidar(deducible="350000", valor_indemnizar="2000000").valor_indemnizar == Decimal(
        "2000000.00"
    )


@pytest.mark.parametrize("reclamado", ["0", "-5", "abc", ""])
def test_el_valor_reclamado_es_positivo(reclamado: str) -> None:
    with pytest.raises(ValorFueraDeRango) as error:
        liquidar(valor_reclamado=reclamado)
    assert "liquidacion.valorReclamado" in error.value.campos


@pytest.mark.parametrize("texto", ["1e5", "1,5", " 5", "5.", ".5", "2500000.001", "NaN"])
def test_un_monto_mal_escrito_o_con_mas_de_dos_decimales_se_rechaza(texto: str) -> None:
    with pytest.raises(ValorFueraDeRango) as error:
        liquidar(valor_indemnizar=texto)
    assert "liquidacion.valorIndemnizar" in error.value.campos


def test_mas_de_16_digitos_enteros_se_rechaza() -> None:
    with pytest.raises(ValorFueraDeRango) as error:
        liquidar(
            valor_reclamado="10000000000000000",
            valor_indemnizar="1",
            deducible="0",
            suma_asegurada="99999999999999999",
        )
    assert "liquidacion.valorReclamado" in error.value.campos
    assert liquidar(
        valor_reclamado="9999999999999999.99",
        valor_indemnizar="1",
        deducible="0",
        suma_asegurada="3000000",
    )


def test_la_moneda_es_la_de_la_poliza_y_la_del_siniestro() -> None:
    with pytest.raises(ValorFueraDeRango) as error:
        liquidar(moneda="USD")
    assert error.value.campos == ("liquidacion.moneda",)
    with pytest.raises(ValorFueraDeRango):
        liquidar(moneda="COP", moneda_registrada="USD")
    assert liquidar(moneda_registrada="COP").moneda == "COP"


def test_si_el_siniestro_ya_trae_el_valor_reclamado_debe_coincidir() -> None:
    assert liquidar(valor_reclamado_registrado=Decimal("2850000.00"))
    with pytest.raises(ValorFueraDeRango) as error:
        liquidar(valor_reclamado_registrado=Decimal("2800000.00"))
    assert error.value.campos == ("liquidacion.valorReclamado",)
    # Sin dato registrado se acepta el de la solicitud.
    assert liquidar(valor_reclamado_registrado=None)


def test_se_informan_todos_los_campos_que_fallan_en_orden() -> None:
    with pytest.raises(ValorFueraDeRango) as error:
        liquidar(valor_reclamado="100", deducible="200", valor_indemnizar="300", moneda="USD")
    assert error.value.campos == (
        "liquidacion.deducible",
        "liquidacion.valorIndemnizar",
        "liquidacion.moneda",
    )


# --- UT-W20-02: días hábiles (DS3-12, R-68) --------------------------------------------------------

# 2026-11-09 es lunes. Hora de Colombia = UTC − 5.


def cot(dia: int, hora: int, minuto: int = 0) -> datetime:
    """`dia` de noviembre de 2026 a la `hora` de Colombia, como instante UTC."""
    return datetime(2026, 11, dia, hora, minuto, tzinfo=timezone(timedelta(hours=-5))).astimezone(
        UTC
    )


@pytest.mark.parametrize(
    ("solicitud", "dias", "vence"),
    [
        (cot(9, 10), 5, cot(16, 10)),  # lunes + 5 = lunes siguiente
        (cot(12, 16), 5, cot(19, 16)),  # jueves + 5 cruza un fin de semana
        (cot(13, 16), 5, cot(20, 16)),  # viernes + 5
        (cot(14, 9), 5, cot(20, 9)),  # sábado: cuenta desde el lunes
        (cot(15, 23, 30), 10, cot(27, 23, 30)),  # domingo + 10 = viernes de la segunda semana
        (cot(9, 10), 10, cot(23, 10)),  # lunes + 10
        (cot(9, 10), 15, cot(30, 10)),  # lunes + 15
        (cot(9, 10), 1, cot(10, 10)),  # lunes + 1 = martes
        (cot(13, 10), 1, cot(16, 10)),  # viernes + 1 = lunes
    ],
)
def test_los_dias_habiles_saltan_fines_de_semana_y_conservan_la_hora(
    solicitud: datetime, dias: int, vence: datetime
) -> None:
    assert sumar_dias_habiles(solicitud, dias) == vence


def test_la_fecha_es_la_de_colombia_no_la_de_utc() -> None:
    # Viernes 20:00 en Colombia ya es sábado 01:00 en UTC: sigue siendo viernes.
    viernes_noche = datetime(2026, 11, 14, 1, 0, tzinfo=UTC)
    assert viernes_noche.astimezone(UTC).weekday() == 5
    assert sumar_dias_habiles(viernes_noche, 1) == datetime(2026, 11, 17, 1, 0, tzinfo=UTC)


def test_el_resultado_conserva_la_zona_del_argumento() -> None:
    bogota = datetime(2026, 11, 9, 10, 0, tzinfo=timezone(timedelta(hours=-5)))
    vence = sumar_dias_habiles(bogota, 5)
    assert vence == datetime(2026, 11, 16, 10, 0, tzinfo=timezone(timedelta(hours=-5)))
    assert vence.utcoffset() == bogota.utcoffset()


def test_los_dias_habiles_exigen_zona_y_al_menos_un_dia(ingenuo: datetime, ahora: datetime) -> None:
    with pytest.raises(SiniestroInvalido, match="desde"):
        sumar_dias_habiles(ingenuo, 5)
    with pytest.raises(SiniestroInvalido, match="dias"):
        sumar_dias_habiles(ahora, 0)


# --- Referencia de pago y código (DS3-11) ----------------------------------------------------------


@pytest.mark.parametrize(
    ("codigo", "esperada"),
    [("88-2026-0442", "IND-88-2026-0442-01"), ("88-2026-12345", "IND-88-2026-12345-01")],
)
def test_la_referencia_de_pago_es_ind_codigo_01(codigo: str, esperada: str) -> None:
    assert referencia_pago(codigo) == esperada


@pytest.mark.parametrize(
    ("anio", "consecutivo", "esperado"),
    [
        (2026, 1000, "88-2026-1000"),
        (2026, 442, "88-2026-0442"),
        (2027, 1, "88-2027-0001"),
        (2026, 12345, "88-2026-12345"),
    ],
)
def test_el_codigo_es_88_anio_consecutivo(anio: int, consecutivo: int, esperado: str) -> None:
    assert codigo_siniestro(anio, consecutivo) == esperado


@pytest.mark.parametrize(("anio", "consecutivo"), [(1999, 1), (10000, 1), (2026, 0), (2026, -3)])
def test_el_codigo_rechaza_valores_fuera_de_rango(anio: int, consecutivo: int) -> None:
    with pytest.raises(SiniestroInvalido, match="codigo"):
        codigo_siniestro(anio, consecutivo)
