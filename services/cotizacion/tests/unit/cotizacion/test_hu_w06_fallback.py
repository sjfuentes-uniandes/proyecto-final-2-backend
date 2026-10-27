"""N1 de HU-W06: respuesta degradada permitida (docs/hu-w06-fallback.md).

1. La tabla de `decidir_fallback` completa: opcional con y sin regla, obligatoria, reglas vencidas o
   futuras, varias fuentes caídas, malformado, ajuste explícito y nunca un valor inventado.
2. Las fuentes fallidas salen del linaje del perfil (el motor decide cuáles son obligatorias).
3. El caso de uso: oferta degradada, PENDIENTE registrado, falla cerrada de catalogo, telemetría.
4. La API: campos aditivos, 503 con la decisión y GET /v1/cotizaciones/{id}/decision.
5. ProductosCatalogo: las reglas de fallback de catalogo (404 = sin reglas, caché en memoria).
"""

import json
from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from cotizacion.adapters.outbound.productos_catalogo import ProductosCatalogo
from cotizacion.application.cotizacion.calcular_prima import DEPENDENCIA_FALLBACK
from cotizacion.config import Settings
from cotizacion.domain.cotizacion.fallback import (
    FACTOR_FALLBACK,
    AccionFallback,
    AccionSugerida,
    CotizacionPendiente,
    FuenteFallida,
    MensajeFallback,
    MotivoFalla,
    TipoDecision,
    decidir_fallback,
    fuentes_fallidas,
)
from cotizacion.domain.cotizacion.modelos import FactorAplicado
from cotizacion.domain.perfilamiento.errores import (
    ConflictoIdempotencia,
    DependenciaAgotada,
    DependenciaNoDisponible,
)
from cotizacion.domain.perfilamiento.modelos import EstadoFuente, EstadoPerfil, Fuente
from cotizacion.main import Puertos, crear_app
from fastapi.testclient import TestClient

from ...conftest import perfil_con_fallas, reglas_fallback_semilla
from .test_api import ASESOR, C01, SOCIO_1, clave
from .test_calcular_prima import PERFIL_C01
from .test_productos_catalogo import AHORA, Catalogo, Reloj

pytestmark = pytest.mark.anyio
INGRESOS = Fuente.FA_INGRESOS_AGREGADOS
REGISTROS = Fuente.DA_REGISTROS_PUBLICOS
HISTORIAL = Fuente.FA_HISTORIAL_PAGOS_12M
PRODUCTOS = Fuente.FA_PRODUCTOS_VIGENTES
# Valores de C01 que nunca pueden aparecer en una respuesta, un log o un evento degradados.
CENTINELAS = ("6500000", "1800000", "1990-04-18", "180000000")


def _falla(
    fuente: Fuente,
    motivo: MotivoFalla = MotivoFalla.NO_DISPONIBLE,
    obligatoria: bool = False,
    datos: tuple[str, ...] = ("dato",),
) -> FuenteFallida:
    return FuenteFallida(fuente.value, motivo, obligatoria, datos)


def _sin(reglas: Any, *ids: str) -> Any:
    return replace(reglas, reglas=tuple(r for r in reglas.reglas if r.id not in ids))


def _con(reglas: Any, id_: str, **cambios: Any) -> Any:
    return replace(
        reglas, reglas=tuple(replace(r, **cambios) if r.id == id_ else r for r in reglas.reglas)
    )


# --- 1. Tabla de decisión -------------------------------------------------------------------


def test_sin_fuentes_fallidas_no_hay_decision() -> None:
    assert decidir_fallback((), reglas_fallback_semilla(), AHORA) is None


SEMILLA = reglas_fallback_semilla()


@pytest.mark.parametrize(
    ("fallas", "reglas", "tipo", "reglas_aplicadas", "version"),
    [
        ((_falla(INGRESOS),), SEMILLA, TipoDecision.DEGRADADA, ["FB-003"], "2026.10.1"),
        (
            (_falla(REGISTROS),),
            _con(SEMILLA, "FB-004", accion=AccionFallback.PENDIENTE),
            TipoDecision.PENDIENTE,
            ["FB-004"],
            "2026.10.1",
        ),
        ((_falla(INGRESOS),), _sin(SEMILLA, "FB-003"), TipoDecision.PENDIENTE, [None], "2026.10.1"),
        (
            (_falla(HISTORIAL, obligatoria=True),),
            SEMILLA,
            TipoDecision.PENDIENTE,
            ["FB-002"],
            "2026.10.1",
        ),
        # El perfil dice obligatoria aunque la regla diga lo contrario: manda el modelo.
        (
            (_falla(HISTORIAL, obligatoria=True),),
            _con(SEMILLA, "FB-002", obligatoria=False, accion=AccionFallback.DEGRADAR),
            TipoDecision.PENDIENTE,
            ["FB-002"],
            "2026.10.1",
        ),
        # Una regla que se declara obligatoria tampoco degrada.
        (
            (_falla(INGRESOS),),
            _con(SEMILLA, "FB-003", obligatoria=True),
            TipoDecision.PENDIENTE,
            ["FB-003"],
            "2026.10.1",
        ),
        ((_falla(INGRESOS),), None, TipoDecision.PENDIENTE, [None], None),
        (
            (_falla(INGRESOS),),
            replace(SEMILLA, vigente_hasta=AHORA),
            TipoDecision.PENDIENTE,
            [None],
            None,
        ),
        (
            (_falla(INGRESOS),),
            replace(SEMILLA, vigente_desde=AHORA + timedelta(microseconds=1)),
            TipoDecision.PENDIENTE,
            [None],
            None,
        ),
        (
            (_falla(INGRESOS),),
            replace(SEMILLA, vigente_desde=AHORA, vigente_hasta=AHORA + timedelta(seconds=1)),
            TipoDecision.DEGRADADA,
            ["FB-003"],
            "2026.10.1",
        ),
        # Varias fuentes: basta una sin fallback permitido; todas se informan.
        (
            (_falla(REGISTROS), _falla(INGRESOS)),
            _con(SEMILLA, "FB-004", accion=AccionFallback.PENDIENTE),
            TipoDecision.PENDIENTE,
            ["FB-004", "FB-003"],
            "2026.10.1",
        ),
        (
            (_falla(REGISTROS), _falla(INGRESOS)),
            SEMILLA,
            TipoDecision.DEGRADADA,
            ["FB-004", "FB-003"],
            "2026.10.1",
        ),
        (
            (_falla(INGRESOS), _falla(HISTORIAL, MotivoFalla.TIEMPO_AGOTADO, obligatoria=True)),
            SEMILLA,
            TipoDecision.PENDIENTE,
            ["FB-002", "FB-003"],
            "2026.10.1",
        ),
        # Malformado: es una falla de la fuente como cualquier otra.
        (
            (_falla(INGRESOS, MotivoFalla.RESPUESTA_INVALIDA),),
            SEMILLA,
            TipoDecision.DEGRADADA,
            ["FB-003"],
            "2026.10.1",
        ),
        (
            (_falla(PRODUCTOS, MotivoFalla.RESPUESTA_INVALIDA, obligatoria=True),),
            SEMILLA,
            TipoDecision.PENDIENTE,
            ["FB-001"],
            "2026.10.1",
        ),
    ],
    ids=[
        "opcional con regla DEGRADAR",
        "opcional con regla PENDIENTE",
        "opcional sin regla",
        "obligatoria",
        "obligatoria con regla mal declarada",
        "regla que se declara obligatoria",
        "sin reglas vigentes",
        "reglas vencidas (vigenteHasta = ahora)",
        "reglas futuras (1 µs)",
        "vigentes desde ahora mismo",
        "dos opcionales, una PENDIENTE",
        "dos opcionales degradables",
        "opcional y obligatoria",
        "opcional malformada",
        "obligatoria malformada",
    ],
)
def test_tabla_de_fallback(
    fallas: tuple[FuenteFallida, ...],
    reglas: Any,
    tipo: TipoDecision,
    reglas_aplicadas: list[str | None],
    version: str | None,
) -> None:
    decision = decidir_fallback(fallas, reglas, AHORA)
    assert decision is not None
    assert decision.tipo is tipo
    assert [f.regla.id if f.regla else None for f in decision.faltantes] == reglas_aplicadas
    assert decision.version_reglas == version
    assert {f.regla.version for f in decision.faltantes if f.regla} <= {version}
    assert decision.fuentes == tuple(sorted(f.fuente for f in fallas))
    assert {(f.fuente, f.motivo) for f in decision.faltantes} == {
        (f.fuente, f.motivo) for f in fallas
    }
    sugerida = AccionSugerida.RECALCULAR_PERFIL if tipo is TipoDecision.PENDIENTE else None
    assert decision.accion_sugerida is sugerida
    if tipo is TipoDecision.PENDIENTE:
        assert decision.ajustes == ()


def test_cada_dato_faltante_se_nombra_sin_valor() -> None:
    """Nunca se rellena: la decisión solo tiene nombres (fuente, dato, motivo y regla)."""
    falla = _falla(PRODUCTOS, datos=("antiguedad_productos_anios", "entidades_con_deuda"))
    regla = replace(SEMILLA.reglas[0], fuente=PRODUCTOS.value, obligatoria=False)
    regla = replace(regla, accion=AccionFallback.DEGRADAR)
    decision = decidir_fallback((falla,), replace(SEMILLA, reglas=(regla,)), AHORA)
    assert decision is not None
    assert [(f.fuente, f.dato) for f in decision.faltantes] == [
        ("FA_PRODUCTOS_VIGENTES", "antiguedad_productos_anios"),
        ("FA_PRODUCTOS_VIGENTES", "entidades_con_deuda"),
    ]
    assert not hasattr(decision.faltantes[0], "valor")


def test_el_ajuste_de_la_regla_es_un_factor_explicito() -> None:
    reglas = _con(SEMILLA, "FB-003", ajuste=Decimal("1.05"))
    decision = decidir_fallback((_falla(INGRESOS),), reglas, AHORA)
    assert decision is not None and decision.tipo is TipoDecision.DEGRADADA
    assert decision.ajustes == (
        FactorAplicado(FACTOR_FALLBACK, "FA_INGRESOS_AGREGADOS", Decimal("1.05")),
    )
    # Con una PENDIENTE al lado no hay oferta, y por tanto ningún ajuste.
    obligatoria = _falla(HISTORIAL, obligatoria=True)
    pendiente = decidir_fallback((_falla(INGRESOS), obligatoria), reglas, AHORA)
    assert pendiente is not None and pendiente.ajustes == ()


def test_el_mensaje_sale_de_la_regla_que_decide() -> None:
    degradada = decidir_fallback((_falla(INGRESOS),), SEMILLA, AHORA)
    pendiente = decidir_fallback((_falla(INGRESOS),), None, AHORA)
    assert degradada is not None and pendiente is not None
    assert degradada.mensaje_codigo is MensajeFallback.FUENTE_OPCIONAL_NO_DISPONIBLE
    assert pendiente.mensaje_codigo is MensajeFallback.FUENTE_REQUERIDA_NO_DISPONIBLE


# --- 2. Las fuentes fallidas salen del linaje del perfil ------------------------------------


@pytest.mark.parametrize(
    ("fallas", "estado_perfil", "esperadas"),
    [
        (
            {INGRESOS: EstadoFuente.NO_DISPONIBLE},
            EstadoPerfil.DEGRADADO,
            [("FA_INGRESOS_AGREGADOS", MotivoFalla.NO_DISPONIBLE, False)],
        ),
        (
            {REGISTROS: EstadoFuente.TIEMPO_AGOTADO},
            EstadoPerfil.DEGRADADO,
            [("DA_REGISTROS_PUBLICOS", MotivoFalla.TIEMPO_AGOTADO, False)],
        ),
        (
            {HISTORIAL: EstadoFuente.TIEMPO_AGOTADO},
            EstadoPerfil.INFORMACION_INSUFICIENTE,
            [("FA_HISTORIAL_PAGOS_12M", MotivoFalla.TIEMPO_AGOTADO, True)],
        ),
        (
            {PRODUCTOS: EstadoFuente.RESPUESTA_INVALIDA},
            EstadoPerfil.INFORMACION_INSUFICIENTE,
            [("FA_PRODUCTOS_VIGENTES", MotivoFalla.RESPUESTA_INVALIDA, True)],
        ),
        # Sin permiso no es una falla de la fuente: no entra en la decisión.
        ({INGRESOS: EstadoFuente.NO_AUTORIZADA}, EstadoPerfil.CALCULADO, []),
    ],
    ids=["C07 caída", "C06 lenta", "C13 obligatoria lenta", "C05 malformada", "no autorizada"],
)
def test_fuentes_fallidas_del_linaje(
    perfil_c01: Any,
    modelo: Any,
    fallas: dict[Fuente, EstadoFuente],
    estado_perfil: EstadoPerfil,
    esperadas: list[tuple[str, MotivoFalla, bool]],
) -> None:
    perfil = perfil_con_fallas(perfil_c01, modelo, fallas)
    assert perfil.resultado.estado is estado_perfil
    fallidas = fuentes_fallidas(perfil)
    assert [(f.fuente, f.motivo, f.obligatoria) for f in fallidas] == esperadas
    for falla in fallidas:
        assert (
            falla.datos
            == next(r for r in perfil.fuentes if r.fuente == falla.fuente).campos_solicitados
        )


# --- 3. Caso de uso ------------------------------------------------------------------------


def _con_perfil(escenario: Any, perfil_c01: Any, modelo: Any, fallas: dict) -> Any:
    perfil = perfil_con_fallas(perfil_c01, modelo, fallas)
    escenario.perfiles.perfiles[perfil.id] = perfil
    return perfil


def _resultado(telemetria: Any) -> str:
    cierre = [
        r for r in telemetria.logs() if r.get("operation") == "cotizacion" and r.get("result")
    ]
    return str(cierre[-1]["result"])


async def test_ac1_opcional_caida_con_regla_da_oferta_degradada(
    escenario_cotizacion: Any, nueva_cotizacion: Any, perfil_c01: Any, modelo: Any, telemetria: Any
) -> None:
    perfil = _con_perfil(
        escenario_cotizacion, perfil_c01, modelo, {INGRESOS: EstadoFuente.NO_DISPONIBLE}
    )
    cotizacion, pasos, creada = await escenario_cotizacion.caso().ejecutar(
        nueva_cotizacion(perfil_id=PERFIL_C01)
    )
    assert creada and cotizacion.degradada and cotizacion.oferta is not None
    registro = cotizacion.decision
    assert registro.cotizacion_id == cotizacion.id and registro.perfil_id == perfil.id
    assert registro.decision.tipo is TipoDecision.DEGRADADA
    [faltante] = registro.decision.faltantes
    assert (faltante.fuente, faltante.dato, faltante.motivo) == (
        "FA_INGRESOS_AGREGADOS",
        "ingreso_mensual_estimado",
        MotivoFalla.NO_DISPONIBLE,
    )
    assert (faltante.regla.id, faltante.regla.version) == ("FB-003", "2026.10.1")
    # Sin ajuste en la semilla: el desglose solo tiene los factores de las reglas de prima.
    assert FACTOR_FALLBACK not in {f.codigo for f in cotizacion.desglose.factores}
    # El nivel es el que calculó el motor con las fuentes que respondieron, no uno inventado.
    assert cotizacion.entradas.nivel_riesgo is perfil.resultado.nivel
    assert "REGLAS_FALLBACK" in [p.codigo for p in pasos]
    assert _resultado(telemetria) == "degradado"
    assert escenario_cotizacion.decisiones.pendientes == []
    cierre = next(r for r in telemetria.logs() if r["message"] == "cotizacion calculada")
    assert cierre["fallback"]["decision"] == "DEGRADADA"
    texto = telemetria.texto_logs() + telemetria.texto_spans()
    for sensible in CENTINELAS:
        assert sensible not in texto


async def test_ac1_el_ajuste_de_la_regla_queda_en_el_desglose(
    escenario_cotizacion: Any, nueva_cotizacion: Any, perfil_c01: Any, modelo: Any
) -> None:
    _con_perfil(escenario_cotizacion, perfil_c01, modelo, {INGRESOS: EstadoFuente.NO_DISPONIBLE})
    sin_ajuste, _, _ = await escenario_cotizacion.caso().ejecutar(
        nueva_cotizacion(perfil_id=PERFIL_C01)
    )
    escenario_cotizacion.fallback.reglas = _con(SEMILLA, "FB-003", ajuste=Decimal("1.05"))
    con_ajuste, _, _ = await escenario_cotizacion.caso().ejecutar(
        nueva_cotizacion(perfil_id=PERFIL_C01, idempotency_key=clave_uuid(2))
    )
    assert con_ajuste.desglose.factores[-1] == FactorAplicado(
        FACTOR_FALLBACK, "FA_INGRESOS_AGREGADOS", Decimal("1.05")
    )
    assert con_ajuste.desglose.prima_total > sin_ajuste.desglose.prima_total
    assert con_ajuste.oferta.precio == con_ajuste.desglose.prima_total
    assert con_ajuste.huella_calculo != sin_ajuste.huella_calculo


def clave_uuid(n: int) -> Any:
    from uuid import UUID

    return UUID(f"00000000-0000-4000-c000-{n:012d}")


@pytest.mark.parametrize(
    "fallas",
    [
        {HISTORIAL: EstadoFuente.TIEMPO_AGOTADO},
        {PRODUCTOS: EstadoFuente.RESPUESTA_INVALIDA},
        {REGISTROS: EstadoFuente.NO_DISPONIBLE},
        {INGRESOS: EstadoFuente.NO_DISPONIBLE, HISTORIAL: EstadoFuente.TIEMPO_AGOTADO},
    ],
    ids=[
        "obligatoria lenta",
        "obligatoria malformada",
        "opcional con regla PENDIENTE",
        "opcional y obligatoria",
    ],
)
async def test_ac2_sin_fallback_permitido_no_hay_oferta_y_queda_pendiente(
    escenario_cotizacion: Any,
    nueva_cotizacion: Any,
    perfil_c01: Any,
    modelo: Any,
    telemetria: Any,
    fallas: dict,
) -> None:
    _con_perfil(escenario_cotizacion, perfil_c01, modelo, fallas)
    # La semilla degrada las dos opcionales: una versión que no lo permite para DA.
    escenario_cotizacion.fallback.reglas = _con(SEMILLA, "FB-004", accion=AccionFallback.PENDIENTE)
    with pytest.raises(CotizacionPendiente) as error:
        await escenario_cotizacion.caso().ejecutar(nueva_cotizacion(perfil_id=PERFIL_C01))
    registro = error.value.registro
    assert isinstance(error.value, DependenciaNoDisponible)  # 503 recuperable
    assert registro.decision.tipo is TipoDecision.PENDIENTE and registro.cotizacion_id is None
    assert {f.fuente for f in registro.decision.faltantes} == {f.value for f in fallas}
    assert escenario_cotizacion.repo.cotizaciones == {}
    assert escenario_cotizacion.decisiones.pendientes == [registro]
    assert _resultado(telemetria) == "pendiente"
    aviso = next(r for r in telemetria.logs() if r["message"] == "cotizacion pendiente")
    assert aviso["decision_id"] == str(registro.id)
    texto = telemetria.texto_logs() + telemetria.texto_spans()
    for sensible in CENTINELAS:
        assert sensible not in texto


async def test_ac2_repetir_la_clave_devuelve_la_misma_decision(
    escenario_cotizacion: Any, nueva_cotizacion: Any, perfil_c01: Any, modelo: Any
) -> None:
    _con_perfil(escenario_cotizacion, perfil_c01, modelo, {HISTORIAL: EstadoFuente.TIEMPO_AGOTADO})
    caso = escenario_cotizacion.caso()
    with pytest.raises(CotizacionPendiente) as primera:
        await caso.ejecutar(nueva_cotizacion(perfil_id=PERFIL_C01))
    with pytest.raises(CotizacionPendiente) as segunda:
        await caso.ejecutar(nueva_cotizacion(perfil_id=PERFIL_C01))
    assert segunda.value.registro.id == primera.value.registro.id
    assert len(escenario_cotizacion.decisiones.pendientes) == 1
    with pytest.raises(ConflictoIdempotencia):
        await caso.ejecutar(
            nueva_cotizacion(perfil_id=PERFIL_C01, coberturas_opcionales=("AUXILIO_FUNERARIO",))
        )


async def test_sin_reglas_vigentes_ninguna_fuente_es_degradable(
    escenario_cotizacion: Any, nueva_cotizacion: Any, perfil_c01: Any, modelo: Any
) -> None:
    _con_perfil(escenario_cotizacion, perfil_c01, modelo, {INGRESOS: EstadoFuente.NO_DISPONIBLE})
    escenario_cotizacion.fallback.reglas = None
    with pytest.raises(CotizacionPendiente) as error:
        await escenario_cotizacion.caso().ejecutar(nueva_cotizacion(perfil_id=PERFIL_C01))
    assert error.value.registro.decision.version_reglas is None


@pytest.mark.parametrize(
    "falla",
    [DependenciaNoDisponible("catalogo.productos"), DependenciaAgotada("catalogo.productos")],
    ids=["no disponible", "tiempo agotado"],
)
async def test_si_catalogo_no_entrega_las_reglas_falla_cerrado(
    escenario_cotizacion: Any,
    nueva_cotizacion: Any,
    perfil_c01: Any,
    modelo: Any,
    falla: Exception,
) -> None:
    """Las reglas de fallback son una dependencia del recorrido: sin ellas, 503 como W05."""
    _con_perfil(escenario_cotizacion, perfil_c01, modelo, {INGRESOS: EstadoFuente.NO_DISPONIBLE})
    escenario_cotizacion.fallback.error = falla
    with pytest.raises(DependenciaNoDisponible) as error:
        await escenario_cotizacion.caso().ejecutar(nueva_cotizacion(perfil_id=PERFIL_C01))
    assert not isinstance(error.value, CotizacionPendiente)
    assert error.value.dependencia == DEPENDENCIA_FALLBACK
    assert escenario_cotizacion.decisiones.pendientes == []


async def test_el_recorrido_nominal_no_pide_reglas_de_fallback(
    escenario_cotizacion: Any, nueva_cotizacion: Any, telemetria: Any
) -> None:
    con_perfil, _, _ = await escenario_cotizacion.caso().ejecutar(
        nueva_cotizacion(perfil_id=PERFIL_C01)
    )
    sin_perfil, _, _ = await escenario_cotizacion.caso().ejecutar(
        nueva_cotizacion(idempotency_key=clave_uuid(9))
    )
    assert not con_perfil.degradada and not sin_perfil.degradada
    assert escenario_cotizacion.fallback.llamadas == []
    assert _resultado(telemetria) == "ok"


async def test_sin_puertos_de_fallback_falla_cerrado_sin_guardar(
    escenario_cotizacion: Any, nueva_cotizacion: Any, perfil_c01: Any, modelo: Any
) -> None:
    _con_perfil(escenario_cotizacion, perfil_c01, modelo, {INGRESOS: EstadoFuente.NO_DISPONIBLE})
    caso = escenario_cotizacion.caso()
    caso._fallback = None
    caso._decisiones = None
    with pytest.raises(CotizacionPendiente):
        await caso.ejecutar(nueva_cotizacion(perfil_id=PERFIL_C01))


async def test_repetir_la_clave_de_una_degradada_devuelve_la_misma(
    escenario_cotizacion: Any, nueva_cotizacion: Any, perfil_c01: Any, modelo: Any, telemetria: Any
) -> None:
    _con_perfil(escenario_cotizacion, perfil_c01, modelo, {INGRESOS: EstadoFuente.NO_DISPONIBLE})
    caso = escenario_cotizacion.caso()
    primera, _, _ = await caso.ejecutar(nueva_cotizacion(perfil_id=PERFIL_C01))
    repetida, pasos, creada = await caso.ejecutar(nueva_cotizacion(perfil_id=PERFIL_C01))
    assert (repetida.id, repetida.decision, creada, pasos) == (
        primera.id,
        primera.decision,
        False,
        (),
    )
    assert _resultado(telemetria) == "degradado"


# --- 4. API ----------------------------------------------------------------------------------


@pytest.fixture
def api_fallback(escenario: Any, escenario_cotizacion: Any) -> Iterator[TestClient]:
    e = escenario_cotizacion
    puertos = Puertos(
        identidad=e.identidad,
        consentimiento=escenario.consentimiento,
        modelos=escenario.modelos,
        ejecutor=escenario.ejecutor,
        perfiles=e.perfiles,
        reloj=e.reloj,
        productos=e.productos,
        cotizaciones=e.repo,
        reglas_fallback=e.fallback,
        decisiones=e.decisiones,
    )
    with TestClient(crear_app(Settings(), puertos=puertos)) as cliente:
        yield cliente


def _cotizar_con_perfil(api: TestClient, n: int = 1) -> Any:
    cuerpo = {**C01, "perfilId": str(PERFIL_C01)}
    return api.post("/v1/cotizaciones", json=cuerpo, headers={**ASESOR, **clave(n)})


def test_api_oferta_degradada_identificada_y_su_decision(
    api_fallback: TestClient, escenario_cotizacion: Any, perfil_c01: Any, modelo: Any
) -> None:
    _con_perfil(escenario_cotizacion, perfil_c01, modelo, {INGRESOS: EstadoFuente.NO_DISPONIBLE})
    respuesta = _cotizar_con_perfil(api_fallback)
    assert respuesta.status_code == 201, respuesta.text
    cuerpo = respuesta.json()
    faltante = {
        "fuente": "FA_INGRESOS_AGREGADOS",
        "dato": "ingreso_mensual_estimado",
        "motivo": "NO_DISPONIBLE",
        "regla": {"id": "FB-003", "version": "2026.10.1"},
    }
    assert (cuerpo["degradada"], cuerpo["faltantes"]) == (True, [faltante])
    assert cuerpo["versionReglasFallback"] == "2026.10.1"
    assert cuerpo["oferta"]["estado"] == "VIGENTE"
    # El dato faltante no aparece con ningún valor; ni el hash de las reglas.
    assert "ingresoMensualEstimado" not in json.dumps(cuerpo) and "hashReglas" not in cuerpo
    # Ni el valor que el aliado no entregó (6.500.000 en C01) ni los de las demás fuentes.
    assert "6500000" not in json.dumps(cuerpo) and "1800000." not in json.dumps(cuerpo)
    leida = api_fallback.get(f"/v1/cotizaciones/{cuerpo['id']}", headers=ASESOR).json()
    assert (leida["degradada"], leida["faltantes"]) == (True, [faltante])
    decision = api_fallback.get(f"/v1/cotizaciones/{cuerpo['id']}/decision", headers=ASESOR)
    assert decision.status_code == 200
    assert decision.json() | {"decisionId": None, "decididoEn": None} == {
        "cotizacionId": cuerpo["id"],
        "decision": "DEGRADADA",
        "decisionId": None,
        "perfilId": str(PERFIL_C01),
        "faltantes": [faltante],
        "versionReglasFallback": "2026.10.1",
        "mensajeCodigo": "FUENTE_OPCIONAL_NO_DISPONIBLE",
        "decididoEn": None,
    }


def test_api_recorrido_nominal(api_fallback: TestClient) -> None:
    cuerpo = _cotizar_con_perfil(api_fallback).json()
    assert (cuerpo["degradada"], cuerpo["faltantes"], cuerpo["versionReglasFallback"]) == (
        False,
        [],
        None,
    )
    decision = api_fallback.get(f"/v1/cotizaciones/{cuerpo['id']}/decision", headers=ASESOR)
    assert decision.json()["decision"] == "NOMINAL" and decision.json()["faltantes"] == []


def test_api_pendiente_es_503_recuperable_con_la_decision(
    api_fallback: TestClient, escenario_cotizacion: Any, perfil_c01: Any, modelo: Any
) -> None:
    _con_perfil(escenario_cotizacion, perfil_c01, modelo, {HISTORIAL: EstadoFuente.TIEMPO_AGOTADO})
    respuesta = _cotizar_con_perfil(api_fallback)
    assert respuesta.status_code == 503
    cuerpo = respuesta.json()
    assert cuerpo["codigo"] == "DEPENDENCIA_NO_DISPONIBLE"
    detalles = cuerpo["detalles"]
    [registro] = escenario_cotizacion.decisiones.pendientes
    assert detalles == {
        "dependencia": "perfil.fuentes",
        "decisionId": str(registro.id),
        "accionSugerida": "RECALCULAR_PERFIL",
        "mensajeCodigo": "FUENTE_REQUERIDA_NO_DISPONIBLE",
        "versionReglasFallback": "2026.10.1",
        "faltantes": [
            {
                "fuente": "FA_HISTORIAL_PAGOS_12M",
                "dato": "moras_12m",
                "motivo": "TIEMPO_AGOTADO",
                "regla": {"id": "FB-002", "version": "2026.10.1"},
            }
        ],
    }


def test_api_la_decision_es_del_asesor_o_del_socio_dueno(api_fallback: TestClient) -> None:
    cuerpo = api_fallback.post("/v1/cotizaciones", json=C01, headers={**SOCIO_1, **clave(5)}).json()
    ruta = f"/v1/cotizaciones/{cuerpo['id']}/decision"
    assert api_fallback.get(ruta, headers=SOCIO_1).json()["decision"] == "NOMINAL"
    assert api_fallback.get(ruta, headers=ASESOR).status_code == 200
    otro = api_fallback.get(ruta, headers={"X-Actor-Id": "SOC-000002", "X-Actor-Rol": "SOCIO"})
    assert (otro.status_code, otro.json()["codigo"]) == (404, "COTIZACION_NO_ENCONTRADA")
    inexistente = api_fallback.get(
        "/v1/cotizaciones/00000000-0000-4000-c000-999999999999/decision", headers=ASESOR
    )
    assert inexistente.status_code == 404


# --- 5. ProductosCatalogo: reglas de fallback ------------------------------------------------


def _reglas_fallback_json(**cambios: Any) -> dict[str, Any]:
    from ...conftest import SEMILLAS_CATALOGO

    definicion = json.loads(
        (SEMILLAS_CATALOGO / "reglas_fallback_2026_10_1.json").read_text(encoding="utf-8")
    )
    return {
        "productoId": "vida-hipotecario",
        "version": "2026.10.1",
        "vigenteDesde": "2026-10-01T00:00:00Z",
        "vigenteHasta": None,
        "hash": "sha256:fallback",
        "definicion": definicion,
        **cambios,
    }


def _adaptador(catalogo: Catalogo, reloj: Reloj, **opciones: Any) -> ProductosCatalogo:
    cliente = httpx.AsyncClient(transport=httpx.MockTransport(catalogo))
    return ProductosCatalogo(cliente, "http://catalogo:8080/", 150, reloj, **opciones)


RUTA = "/reglas-fallback/vigente"


async def test_catalogo_entrega_las_reglas_con_la_fecha() -> None:
    catalogo, reloj = Catalogo(), Reloj()
    catalogo.respuestas[RUTA] = httpx.Response(200, json=_reglas_fallback_json())
    reglas = await _adaptador(catalogo, reloj, ttl_s=0).reglas_fallback("vida-hipotecario", AHORA)
    assert reglas is not None and reglas.version == "2026.10.1"
    assert [(r.id, r.accion) for r in reglas.reglas][2] == ("FB-003", AccionFallback.DEGRADAR)
    [solicitud] = catalogo.solicitudes
    assert solicitud.url.path == "/v1/productos/vida-hipotecario/reglas-fallback/vigente"
    assert solicitud.url.params["fecha"] == AHORA.isoformat()


@pytest.mark.parametrize(
    ("respuesta", "esperado"),
    [
        (httpx.Response(404, json={"codigo": "REGLAS_NO_VIGENTES"}), None),
        (httpx.Response(404, json={"codigo": "PRODUCTO_NO_ENCONTRADO"}), None),
        (httpx.Response(500), DependenciaNoDisponible),
        (httpx.Response(200, json={"productoId": "x"}), DependenciaNoDisponible),
        (
            httpx.Response(
                200, json=_reglas_fallback_json(definicion={"productoId": "x", "reglas": []})
            ),
            DependenciaNoDisponible,
        ),
    ],
    ids=["sin reglas", "producto", "500", "fuera del contrato", "sin reglas en la definición"],
)
async def test_catalogo_sin_reglas_o_fuera_del_contrato(
    respuesta: httpx.Response, esperado: Any
) -> None:
    catalogo, reloj = Catalogo(), Reloj()
    catalogo.respuestas[RUTA] = respuesta
    productos = _adaptador(catalogo, reloj, ttl_s=0)
    if esperado is None:
        assert await productos.reglas_fallback("vida-hipotecario", AHORA) is None
    else:
        with pytest.raises(esperado):
            await productos.reglas_fallback("vida-hipotecario", AHORA)


async def test_las_reglas_se_guardan_en_memoria_aun_con_valkey_y_solo_mientras_rigen() -> None:
    from .test_productos_catalogo import CacheLectura

    catalogo, reloj = Catalogo(), Reloj()
    hasta = (AHORA + timedelta(seconds=30)).isoformat()
    catalogo.respuestas[RUTA] = httpx.Response(200, json=_reglas_fallback_json(vigenteHasta=hasta))
    cache = CacheLectura()
    productos = _adaptador(catalogo, reloj, ttl_s=60, cache=cache)
    await productos.reglas_fallback("vida-hipotecario", AHORA)
    await productos.reglas_fallback("vida-hipotecario", AHORA + timedelta(seconds=10))
    assert len(catalogo.solicitudes) == 1 and cache.escrituras == 0
    # Vencidas en la memoria: se piden de nuevo (catalogo dirá si hay otras).
    catalogo.respuestas[RUTA] = httpx.Response(404, json={"codigo": "REGLAS_NO_VIGENTES"})
    assert (
        await productos.reglas_fallback("vida-hipotecario", AHORA + timedelta(seconds=31)) is None
    )
    assert len(catalogo.solicitudes) == 2
