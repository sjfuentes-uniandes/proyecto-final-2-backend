"""Caso de uso comunicar_decision (HU-W14, W14-B02) con dobles de los puertos: alternativas de la
versión de reglas de la decisión, reloj inyectado y falla abierta solo para las alternativas."""

from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from polizas.application.suscripcion.comunicar import OPERACION_ALTERNATIVAS, ComunicarDecision
from polizas.application.suscripcion.solicitar_suscripcion import (
    ComandoSuscripcion,
    ConfigSuscripcion,
    SolicitarSuscripcion,
)
from polizas.domain.suscripcion.errores import DependenciaNoDisponible, ReglasNoVigentes
from polizas.domain.suscripcion.modelos import (
    Actor,
    CondicionAseguramiento,
    Decision,
    RolActor,
    SiguientePaso,
)

pytestmark = pytest.mark.anyio
ASESOR = Actor("sub-asesor-1", RolActor.ASESOR)


async def _decision(dobles: Any, sinteticos: Any, caso_id: str) -> Decision:
    solicitar = SolicitarSuscripcion(
        dobles.insumos,
        dobles.reglas,
        dobles.decisiones,
        dobles.codigos,
        dobles.reloj,
        ConfigSuscripcion(),
    )
    comando = ComandoSuscripcion(
        UUID(sinteticos.cotizacion_id(caso_id)), uuid4(), ASESOR, "corr-ut-w14"
    )
    decision, _ = await solicitar.ejecutar(comando)
    return decision


def _comunicar(dobles: Any, timeout_ms: int = 300) -> ComunicarDecision:
    return ComunicarDecision(dobles.reglas, dobles.reloj, 8, timeout_ms)


class ReglasContadas:
    """Envuelve el doble de reglas y cuenta las versiones pedidas."""

    def __init__(self, reglas: Any) -> None:
        self._reglas = reglas
        self.versiones: list[tuple[str, str]] = []

    async def vigentes(self, producto_id: str) -> Any:
        return await self._reglas.vigentes(producto_id)

    async def por_version(self, producto_id: str, version: str) -> Any:
        self.versiones.append((producto_id, version))
        return await self._reglas.por_version(producto_id, version)


async def test_rechazo_con_las_alternativas_de_su_version(dobles: Any, sinteticos: Any) -> None:
    decision = await _decision(dobles, sinteticos, "S02")
    contadas = ReglasContadas(dobles.reglas)
    comunicacion = await ComunicarDecision(contadas, dobles.reloj, 8, 300).ejecutar(decision)
    assert contadas.versiones == [("vida-hipotecario", "2026.10.1")]
    assert comunicacion.condicion is CondicionAseguramiento.NO_ASEGURABLE
    assert comunicacion.motivo_cliente == "EDAD_FUERA_DE_RANGO"
    assert [a.producto_id for a in comunicacion.alternativas] == [
        "vida-senior",
        "proteccion-pagos",
        "auxilio-funerario",
    ]
    assert comunicacion.alternativas == sinteticos.reglas().alternativas


async def test_las_alternativas_salen_de_la_version_guardada_no_de_la_vigente(
    dobles: Any, sinteticos: Any
) -> None:
    """Si catalogo publica otra versión, la decisión sigue comunicando las de su versión."""
    decision = await _decision(dobles, sinteticos, "S03")
    dobles.reglas.vigente = "2026.11.1"
    comunicacion = await _comunicar(dobles).ejecutar(decision)
    assert comunicacion.alternativas == sinteticos.reglas().alternativas


@pytest.mark.parametrize("caso_id", ["S01", "S06"])
async def test_aprobada_y_revision_no_piden_alternativas(
    caso_id: str, dobles: Any, sinteticos: Any
) -> None:
    decision = await _decision(dobles, sinteticos, caso_id)
    contadas = ReglasContadas(dobles.reglas)
    comunicacion = await ComunicarDecision(contadas, dobles.reloj, 8, 300).ejecutar(decision)
    assert contadas.versiones == []
    assert comunicacion.alternativas == ()


async def test_la_aprobada_vence_con_el_reloj(dobles: Any, sinteticos: Any) -> None:
    """Criterio técnico 4: la misma decisión deja de admitir contratación al vencer la oferta."""
    decision = await _decision(dobles, sinteticos, "S01")
    comunicar = _comunicar(dobles)
    vigente = await comunicar.ejecutar(decision)
    dobles.reloj.instante = decision.insumos.vigente_hasta
    vencida = await comunicar.ejecutar(decision)
    assert (vigente.admite_contratacion, vigente.siguiente_paso) == (
        True,
        SiguientePaso.ACEPTAR_OFERTA,
    )
    assert (vencida.admite_contratacion, vencida.siguiente_paso) == (
        False,
        SiguientePaso.COTIZAR_NUEVAMENTE,
    )
    assert vigente.vigente_hasta == vencida.vigente_hasta == decision.insumos.vigente_hasta


async def test_revision_con_el_compromiso_de_polizas(dobles: Any, sinteticos: Any) -> None:
    decision = await _decision(dobles, sinteticos, "S07")
    comunicacion = await ComunicarDecision(dobles.reglas, dobles.reloj, 12, 300).ejecutar(decision)
    assert comunicacion.plazo_respuesta_horas == 12
    assert comunicacion.motivo_cliente == "PERFIL_REQUIERE_REVISION"


@pytest.mark.parametrize(
    ("falla", "resultado"),
    [
        (DependenciaNoDisponible("catalogo"), "error"),
        (ReglasNoVigentes(), "error"),
    ],
)
async def test_catalogo_caido_comunica_sin_alternativas(
    falla: Exception, resultado: str, dobles: Any, sinteticos: Any, telemetria: Any
) -> None:
    """Falla abierta solo para las alternativas: la decisión ya está registrada."""
    decision = await _decision(dobles, sinteticos, "S02")
    dobles.reglas.falla = falla
    comunicacion = await _comunicar(dobles).ejecutar(decision)
    assert comunicacion.alternativas == ()
    assert comunicacion.motivo_cliente == "EDAD_FUERA_DE_RANGO"
    assert comunicacion.siguiente_paso is SiguientePaso.COTIZAR_ALTERNATIVA
    assert telemetria.resultados(OPERACION_ALTERNATIVAS)[-1] == resultado
    [aviso] = [log for log in telemetria.logs() if log["message"] == "alternativas no disponibles"]
    assert (aviso["decision_id"], aviso["dependencia"], aviso["resultado"]) == (
        str(decision.id),
        "catalogo",
        resultado,
    )


async def test_catalogo_lento_agota_su_timeout(
    dobles: Any, sinteticos: Any, telemetria: Any
) -> None:
    decision = await _decision(dobles, sinteticos, "S02")
    dobles.reglas.demora_s = 0.2
    comunicacion = await _comunicar(dobles, timeout_ms=50).ejecutar(decision)
    assert comunicacion.alternativas == ()
    assert telemetria.resultados(OPERACION_ALTERNATIVAS)[-1] == "timeout"


async def test_el_aviso_no_tiene_pii(dobles: Any, sinteticos: Any, telemetria: Any) -> None:
    decision = await _decision(dobles, sinteticos, "S02")
    dobles.reglas.falla = DependenciaNoDisponible("catalogo")
    await _comunicar(dobles).ejecutar(decision)
    texto = telemetria.texto_logs()
    assert str(decision.cliente_id) not in texto
    assert str(decision.insumos.edad) not in [
        str(v) for log in telemetria.logs() for v in log.values()
    ]


async def test_la_comunicacion_no_cambia_la_decision(dobles: Any, sinteticos: Any) -> None:
    decision = await _decision(dobles, sinteticos, "S04")
    antes = list(dobles.decisiones.decisiones)
    dobles.reloj.instante = dobles.reloj.instante + timedelta(days=30)
    await _comunicar(dobles).ejecutar(decision)
    assert dobles.decisiones.decisiones == antes
