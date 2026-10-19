"""HU-W31 (PR 3.4): comprobación final del consentimiento tras la fase B (ADR 0011 §3).

- Vigente: el perfil guarda la comprobación y no lleva la marca.
- No vigente (cada causa): lo obtenido se conserva (ADR 0004) y el perfil queda marcado como
  "revocado durante el perfilamiento"; ni la respuesta ni los eventos llevan valores.
- Fallo técnico de `clientes`: falla cerrado (`DEPENDENCIA_NO_DISPONIBLE`), sin perfil.
"""

import contextlib
import json
from dataclasses import replace
from typing import Any

import pytest
from cotizacion.adapters.inbound.http.perfilamiento import respuesta_de
from cotizacion.adapters.outbound.outbox_pg import eventos_de
from cotizacion.application.perfilamiento.crear_perfil import ConfigPerfilamiento, CrearPerfil
from cotizacion.domain.perfilamiento.errores import ClienteNoEncontrado, DependenciaNoDisponible
from cotizacion.domain.perfilamiento.modelos import (
    PROPOSITO_PERFILAMIENTO,
    DecisionConsentimiento,
    EstadoFuente,
    EstadoPerfil,
    Fuente,
    NivelRiesgo,
)
from cotizacion.domain.perfilamiento.plan_fuentes import plan_fuentes

pytestmark = pytest.mark.anyio

P, H, I, D = (
    Fuente.FA_PRODUCTOS_VIGENTES,
    Fuente.FA_HISTORIAL_PAGOS_12M,
    Fuente.FA_INGRESOS_AGREGADOS,
    Fuente.DA_REGISTROS_PUBLICOS,
)
CAUSAS_NO_VIGENTE = ("REVOCADO", "VENCIDO", "SIN_CONSENTIMIENTO", "PROPOSITO_NO_AUTORIZADO")
# Valores sintéticos de C01 que nunca salen del servicio (insumos y declarados).
CENTINELAS = ("1800000", "6500000", "180000000", "1990-04-18")


def _no_vigente(verificacion_de: Any, causa: str) -> Any:
    return verificacion_de(DecisionConsentimiento.RECHAZADO, frozenset(), causa)


# --- Vigente -------------------------------------------------------------------------------


async def test_vigente_guarda_la_comprobacion_sin_marca(escenario: Any, nuevo_comando: Any) -> None:
    perfil, creado = await escenario.caso().ejecutar(nuevo_comando())
    assert creado is True
    assert perfil.comprobacion_final == escenario.comprobacion.respuesta
    assert perfil.revocado_durante_perfilamiento is False
    assert escenario.repo.perfiles == {perfil.id: perfil}
    # Una verificación en la fase A y otra al terminar la fase B (ADR 0011: 2 por perfil).
    assert len(escenario.consentimiento.llamadas) == 1
    assert escenario.comprobacion.llamadas == [
        (perfil.cliente_id, PROPOSITO_PERFILAMIENTO, [P, H, I, D])
    ]


async def test_la_comprobacion_pide_solo_los_alcances_consultados(
    escenario: Any, nuevo_comando: Any, verificacion_de: Any
) -> None:
    escenario.consentimiento.respuesta = verificacion_de(
        DecisionConsentimiento.PERMITIDO_PARCIAL, frozenset({P, H, D})
    )
    perfil, _ = await escenario.caso().ejecutar(nuevo_comando())
    ((_, _, alcances),) = escenario.comprobacion.llamadas
    assert alcances == [P, H, D]
    assert perfil.revocado_durante_perfilamiento is False


async def test_la_comprobacion_ocurre_despues_de_la_fase_b(
    escenario: Any, nuevo_comando: Any
) -> None:
    orden: list[str] = []
    ejecutar, verificar = escenario.ejecutor.ejecutar, escenario.comprobacion.verificar

    async def fuentes(*argumentos: Any) -> Any:
        orden.append("fase_b")
        return await ejecutar(*argumentos)

    async def comprobar(*argumentos: Any) -> Any:
        orden.append("comprobacion_final")
        return await verificar(*argumentos)

    escenario.ejecutor.ejecutar, escenario.comprobacion.verificar = fuentes, comprobar
    await escenario.caso().ejecutar(nuevo_comando())
    assert orden == ["fase_b", "comprobacion_final"]


@pytest.mark.parametrize(
    ("decision", "permitidas"),
    [
        (DecisionConsentimiento.RECHAZADO, frozenset()),
        # C16: falta un obligatorio, así que no se consulta ninguna fuente.
        (DecisionConsentimiento.PERMITIDO_PARCIAL, frozenset({P, I, D})),
    ],
    ids=["rechazado-en-fase-a", "sin-fuentes-a-consultar"],
)
async def test_sin_fuentes_consultadas_no_hay_comprobacion(
    escenario: Any,
    nuevo_comando: Any,
    verificacion_de: Any,
    decision: DecisionConsentimiento,
    permitidas: frozenset[Fuente],
) -> None:
    escenario.consentimiento.respuesta = verificacion_de(decision, permitidas, "REVOCADO")
    perfil, _ = await escenario.caso().ejecutar(nuevo_comando())
    assert escenario.ejecutor.llamadas == [] and escenario.comprobacion.llamadas == []
    assert perfil.comprobacion_final is None
    assert perfil.revocado_durante_perfilamiento is False


async def test_por_omision_comprueba_con_el_puerto_de_la_fase_a(
    escenario: Any, nuevo_comando: Any
) -> None:
    caso = CrearPerfil(
        identidad=escenario.identidad,
        consentimiento=escenario.consentimiento,
        modelos=escenario.modelos,
        plan_fuentes=plan_fuentes,
        ejecutor=escenario.ejecutor,
        repo=escenario.repo,
        reloj=escenario.reloj,
        config=escenario.config,
    )
    perfil, _ = await caso.ejecutar(nuevo_comando())
    assert [a for _, _, a in escenario.consentimiento.llamadas] == [list(Fuente), [P, H, I, D]]
    assert perfil.comprobacion_final == escenario.consentimiento.respuesta


# --- No vigente: se conserva lo obtenido y se marca (ADR 0004) ----------------------------


@pytest.mark.parametrize("causa", CAUSAS_NO_VIGENTE)
async def test_no_vigente_conserva_lo_obtenido_y_marca_el_perfil(
    escenario: Any, nuevo_comando: Any, verificacion_de: Any, causa: str
) -> None:
    nominal, _ = await escenario.caso().ejecutar(nuevo_comando())
    escenario.repo.perfiles.clear()
    escenario.comprobacion.respuesta = _no_vigente(verificacion_de, causa)

    perfil, creado = await escenario.caso().ejecutar(nuevo_comando())

    assert creado is True and escenario.repo.perfiles == {perfil.id: perfil}
    assert perfil.revocado_durante_perfilamiento is True
    assert perfil.comprobacion_final is not None
    assert (perfil.comprobacion_final.decision, perfil.comprobacion_final.causa) == (
        DecisionConsentimiento.RECHAZADO,
        causa,
    )
    # La fase A sigue siendo la que autorizó las consultas.
    assert perfil.consentimiento == nominal.consentimiento
    # Lo obtenido antes de la revocación se conserva: mismas fuentes, insumos y resultado.
    assert [f.estado for f in perfil.fuentes] == [EstadoFuente.CONSULTADA] * 4
    assert perfil.insumos == nominal.insumos
    assert perfil.resultado == nominal.resultado
    assert (perfil.resultado.estado, perfil.resultado.nivel) == (
        EstadoPerfil.CALCULADO,
        NivelRiesgo.BAJO,
    )
    # No se lanza ninguna consulta nueva.
    assert len(escenario.ejecutor.llamadas) == 2


@pytest.mark.parametrize("causa", CAUSAS_NO_VIGENTE)
async def test_no_vigente_no_expone_valores_en_la_respuesta_ni_en_los_eventos(
    escenario: Any, nuevo_comando: Any, verificacion_de: Any, causa: str
) -> None:
    escenario.comprobacion.respuesta = _no_vigente(verificacion_de, causa)
    perfil, _ = await escenario.caso().ejecutar(nuevo_comando())

    cuerpo = respuesta_de(perfil, 400).model_dump(mode="json", by_alias=True)
    assert cuerpo["revocadoDurantePerfilamiento"] is True
    assert cuerpo["comprobacionFinal"] == {
        "decision": "RECHAZADO",
        "causa": causa,
        "vigenteHasta": None,
    }
    assert not {"insumos", "datosDeclarados"} & set(cuerpo)
    sobres = [s.model_dump(mode="json", by_alias=True) for s in eventos_de(perfil, "corr-ut")]
    consultados = next(s for s in sobres if s["type"] == "DatosConsentidosConsultados")
    assert consultados["payload"]["revocadoDurantePerfilamiento"] is True
    assert consultados["payload"]["comprobacionFinal"] == {"decision": "RECHAZADO", "causa": causa}
    texto = json.dumps([cuerpo, sobres])
    for centinela in CENTINELAS:
        assert centinela not in texto, centinela


async def test_la_repeticion_devuelve_el_perfil_marcado_sin_volver_a_comprobar(
    escenario: Any, nuevo_comando: Any, verificacion_de: Any
) -> None:
    escenario.comprobacion.respuesta = _no_vigente(verificacion_de, "REVOCADO")
    primero, _ = await escenario.caso().ejecutar(nuevo_comando())
    repetido, creado = await escenario.caso().ejecutar(nuevo_comando())
    assert (repetido, creado) == (primero, False)
    assert len(escenario.comprobacion.llamadas) == 1


# --- Fallo técnico: falla cerrado ---------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        DependenciaNoDisponible("clientes.consentimiento"),  # 5xx o fuera de contrato
        DependenciaNoDisponible("otra"),
    ],
    ids=["5xx-o-fuera-de-contrato", "nombre-normalizado"],
)
async def test_fallo_tecnico_falla_cerrado_sin_perfil(
    escenario: Any, nuevo_comando: Any, error: Exception
) -> None:
    escenario.comprobacion.respuesta = error
    with pytest.raises(DependenciaNoDisponible) as capturado:
        await escenario.caso().ejecutar(nuevo_comando())
    assert capturado.value.dependencia == "clientes.consentimiento"
    assert escenario.repo.perfiles == {}
    assert len(escenario.ejecutor.llamadas) == 1  # las fuentes sí se consultaron


async def test_timeout_de_la_comprobacion_falla_cerrado(escenario: Any, nuevo_comando: Any) -> None:
    escenario.config = ConfigPerfilamiento(dependencia_timeout_ms=20)
    escenario.comprobacion.demora_s = 0.2
    with pytest.raises(DependenciaNoDisponible) as capturado:
        await escenario.caso().ejecutar(nuevo_comando())
    assert capturado.value.dependencia == "clientes.consentimiento"
    assert isinstance(capturado.value.__cause__, TimeoutError)
    assert escenario.repo.perfiles == {}


async def test_cliente_inexistente_en_la_comprobacion_no_guarda_el_perfil(
    escenario: Any, nuevo_comando: Any
) -> None:
    escenario.comprobacion.respuesta = ClienteNoEncontrado()
    with pytest.raises(ClienteNoEncontrado):
        await escenario.caso().ejecutar(nuevo_comando())
    assert escenario.repo.perfiles == {}


async def test_un_error_inesperado_de_la_comprobacion_se_propaga(
    escenario: Any, nuevo_comando: Any
) -> None:
    escenario.comprobacion.respuesta = RuntimeError("defecto")
    with pytest.raises(RuntimeError):
        await escenario.caso().ejecutar(nuevo_comando())
    assert escenario.repo.perfiles == {}


# --- Telemetría y log ---------------------------------------------------------------------


def _resultados(telemetria: Any, operacion: str) -> list[str]:
    return [s.attributes["result"] for s in telemetria.lista_spans() if s.name == operacion]


async def test_el_log_de_cierre_registra_la_comprobacion_sin_valores(
    telemetria: Any, escenario: Any, nuevo_comando: Any, verificacion_de: Any
) -> None:
    escenario.comprobacion.respuesta = _no_vigente(verificacion_de, "REVOCADO")
    perfil, _ = await escenario.caso().ejecutar(nuevo_comando())
    (cierre,) = [l for l in telemetria.logs() if l["message"] == "perfil calculado"]
    assert cierre["revocado_durante_perfilamiento"] is True
    comprobacion = cierre["comprobacion_final"]
    assert set(comprobacion) == {"nombre", "ms", "resultado"}
    assert (comprobacion["nombre"], comprobacion["resultado"]) == (
        "clientes.comprobacion_final",
        "ok",
    )
    # La comprobación no se mezcla con las dependencias de las fases A y B.
    assert "clientes.comprobacion_final" not in {d["nombre"] for d in cierre["dependencias"]}
    assert _resultados(telemetria, "perfilamiento.comprobacion_final") == ["ok"]
    assert _resultados(telemetria, "perfilamiento") == ["ok"]
    texto = telemetria.texto_logs() + telemetria.texto_spans()
    for centinela in CENTINELAS:
        assert centinela not in texto
    assert str(perfil.cliente_id) not in json.dumps(comprobacion)


async def test_sin_comprobacion_el_log_lo_dice(
    telemetria: Any, escenario: Any, nuevo_comando: Any, verificacion_de: Any
) -> None:
    escenario.consentimiento.respuesta = _no_vigente(verificacion_de, "REVOCADO")
    await escenario.caso().ejecutar(nuevo_comando())
    (cierre,) = [l for l in telemetria.logs() if l["message"] == "perfil calculado"]
    assert (cierre["comprobacion_final"], cierre["revocado_durante_perfilamiento"]) == (None, False)


@pytest.mark.parametrize(
    ("demora_s", "error", "resultado"),
    [(0.2, None, "timeout"), (0, DependenciaNoDisponible("clientes.consentimiento"), "error")],
    ids=["timeout", "error"],
)
async def test_telemetria_del_fallo_de_la_comprobacion(
    telemetria: Any,
    escenario: Any,
    nuevo_comando: Any,
    demora_s: float,
    error: Exception | None,
    resultado: str,
) -> None:
    escenario.config = ConfigPerfilamiento(dependencia_timeout_ms=20)
    escenario.comprobacion.demora_s = demora_s
    if error is not None:
        escenario.comprobacion.respuesta = error
    with contextlib.suppress(DependenciaNoDisponible):
        await escenario.caso().ejecutar(nuevo_comando())
    operacion = "perfilamiento.comprobacion_final"
    assert _resultados(telemetria, operacion) == [resultado]
    assert _resultados(telemetria, "perfilamiento") == ["error"]


def test_la_marca_solo_la_da_un_rechazo(perfil_c01: Any, verificacion_de: Any) -> None:
    assert perfil_c01.comprobacion_final is None
    assert perfil_c01.revocado_durante_perfilamiento is False
    for decision in (DecisionConsentimiento.PERMITIDO, DecisionConsentimiento.PERMITIDO_PARCIAL):
        marcado = replace(perfil_c01, comprobacion_final=verificacion_de(decision))
        assert marcado.revocado_durante_perfilamiento is False
    revocado = replace(perfil_c01, comprobacion_final=_no_vigente(verificacion_de, "REVOCADO"))
    assert revocado.revocado_durante_perfilamiento is True


async def test_la_comprobacion_es_un_paso_hijo_del_perfilamiento(
    telemetria: Any, escenario: Any, nuevo_comando: Any
) -> None:
    await escenario.caso().ejecutar(nuevo_comando())
    spans = {s.name: s for s in telemetria.lista_spans()}
    padre, hijo = spans["perfilamiento"], spans["perfilamiento.comprobacion_final"]
    assert hijo.parent is not None and hijo.parent.span_id == padre.context.span_id
    # No se cuenta entre las dependencias de las fases A y B (perfilamiento.dependencia.*).
    assert not any(n.startswith("perfilamiento.dependencia.clientes.comprob") for n in spans)
