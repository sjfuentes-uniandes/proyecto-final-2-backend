"""Caso de uso crear_perfil (UT-W11-17 a 24) con fakes de todos los puertos y reloj fijo."""

import contextlib
import json
from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from cotizacion.application.perfilamiento.crear_perfil import (
    ConfigPerfilamiento,
    edad_cumplida,
    huella_solicitud,
)
from cotizacion.domain.perfilamiento.errores import (
    ClienteNoEncontrado,
    ConflictoIdempotencia,
    DatosDeclaradosInvalidos,
    DependenciaNoDisponible,
    IdentidadNoVerificada,
    IdentidadPendiente,
    ModeloNoDisponible,
)
from cotizacion.domain.perfilamiento.modelos import (
    PROPOSITO_PERFILAMIENTO,
    AccionSugerida,
    DecisionConsentimiento,
    EstadoCampo,
    EstadoFuente,
    EstadoIdentidad,
    EstadoPerfil,
    Fuente,
    NivelRiesgo,
    ValorCampo,
)

pytestmark = pytest.mark.anyio

P, H, I, D = (
    Fuente.FA_PRODUCTOS_VIGENTES,
    Fuente.FA_HISTORIAL_PAGOS_12M,
    Fuente.FA_INGRESOS_AGREGADOS,
    Fuente.DA_REGISTROS_PUBLICOS,
)
CENTINELAS = ("1800000", "6500000", "180000000", "1990-04-18")


def _llamadas(escenario: Any) -> int:
    return sum(
        len(f.llamadas)
        for f in (
            escenario.identidad,
            escenario.consentimiento,
            escenario.modelos,
            escenario.ejecutor,
        )
    )


async def test_c01_calcula_persiste_y_devuelve_el_perfil(
    escenario: Any, nuevo_comando: Any
) -> None:
    perfil, creado = await escenario.caso().ejecutar(nuevo_comando())
    assert creado is True
    assert escenario.repo.perfiles == {perfil.id: perfil}
    r = perfil.resultado
    assert (r.estado, r.nivel, r.puntaje, r.confianza) == (
        EstadoPerfil.CALCULADO,
        NivelRiesgo.BAJO,
        Decimal("75.90"),
        Decimal("0.92"),
    )
    assert (perfil.version_modelo, perfil.calculado_en) == ("2026.10.1", escenario.reloj.instante)
    assert (perfil.correlation_id, perfil.actor_id) == ("corr-ut", "asesor-sintetico")
    assert perfil.huella_solicitud == huella_solicitud(nuevo_comando())
    assert perfil.consentimiento.decision is DecisionConsentimiento.PERMITIDO
    assert isinstance(perfil.duracion_ms, int) and perfil.duracion_ms >= 0
    assert escenario.modelos.llamadas == [("vida-hipotecario", escenario.reloj.instante)]
    assert escenario.consentimiento.llamadas == [
        (perfil.cliente_id, PROPOSITO_PERFILAMIENTO, list(Fuente))
    ]


# --- UT-W11-17 y 18 · idempotencia -------------------------------------------------------


async def test_ut_w11_17_misma_clave_y_misma_huella_no_llama_a_los_puertos(
    escenario: Any, nuevo_comando: Any
) -> None:
    primero, _ = await escenario.caso().ejecutar(nuevo_comando())
    antes = _llamadas(escenario)
    repetido, creado = await escenario.caso().ejecutar(nuevo_comando())
    assert (repetido, creado) == (primero, False)
    assert _llamadas(escenario) == antes


async def test_ut_w11_17_misma_clave_con_otra_huella_es_conflicto(
    escenario: Any, nuevo_comando: Any
) -> None:
    await escenario.caso().ejecutar(nuevo_comando())
    otro = nuevo_comando(producto_id="otro-producto")
    with pytest.raises(ConflictoIdempotencia):
        await escenario.caso().ejecutar(otro)


@pytest.mark.parametrize(
    "cambio",
    [
        {"producto_id": "otro"},
        {"cliente_id": UUID(int=9)},
        {"perfil_anterior_id": UUID(int=3)},
    ],
)
def test_la_huella_cambia_con_cada_campo_de_la_solicitud(
    nuevo_comando: Any, cambio: dict[str, Any]
) -> None:
    assert huella_solicitud(nuevo_comando(**cambio)) != huella_solicitud(nuevo_comando())


def test_la_huella_cambia_con_los_datos_declarados(nuevo_comando: Any) -> None:
    base = nuevo_comando()
    otro = replace(base.datos_declarados, plazo_credito_meses=240)
    assert huella_solicitud(replace(base, datos_declarados=otro)) != huella_solicitud(base)
    assert huella_solicitud(base).startswith("sha256:")


async def test_ut_w11_18_carrera_al_guardar_devuelve_el_ganador(
    escenario: Any, nuevo_comando: Any, perfil_c01: Any
) -> None:
    ganador = replace(perfil_c01, huella_solicitud=huella_solicitud(nuevo_comando()))
    ganador = replace(ganador, idempotency_key=nuevo_comando().idempotency_key)

    repo = escenario.repo
    original = repo.guardar

    async def guardar_con_carrera(perfil: Any) -> None:
        # Otra réplica guardó la misma clave justo antes.
        repo.perfiles[ganador.id] = ganador
        await original(perfil)

    repo.guardar = guardar_con_carrera
    perfil, creado = await escenario.caso().ejecutar(nuevo_comando())
    assert (perfil, creado) == (ganador, False)
    assert repo.lecturas_idempotencia >= 2


async def test_ut_w11_18_conflicto_sin_ganador_se_propaga(
    escenario: Any, nuevo_comando: Any
) -> None:
    escenario.repo.errores_al_guardar.append(ConflictoIdempotencia())
    with pytest.raises(ConflictoIdempotencia):
        await escenario.caso().ejecutar(nuevo_comando())


async def test_un_error_de_base_al_guardar_se_propaga(escenario: Any, nuevo_comando: Any) -> None:
    escenario.repo.errores_al_guardar.append(OSError("base caída"))
    with pytest.raises(OSError):
        await escenario.caso().ejecutar(nuevo_comando())


# --- UT-W11-19 y 24 · fase A -------------------------------------------------------------


@pytest.mark.parametrize(
    ("identidad", "consentimiento", "modelo", "esperado", "dependencia"),
    [
        (
            DependenciaNoDisponible("x"),
            ClienteNoEncontrado(),
            ModeloNoDisponible(),
            ClienteNoEncontrado,
            None,
        ),
        (EstadoIdentidad.RECHAZADO, None, ClienteNoEncontrado(), ClienteNoEncontrado, None),
        (
            EstadoIdentidad.RECHAZADO,
            None,
            DependenciaNoDisponible("x"),
            DependenciaNoDisponible,
            "catalogo.modelo",
        ),
        (
            EstadoIdentidad.VERIFICADO,
            DependenciaNoDisponible("x"),
            None,
            DependenciaNoDisponible,
            "clientes.consentimiento",
        ),
        (DependenciaNoDisponible("x"), None, None, DependenciaNoDisponible, "clientes.identidad"),
        (EstadoIdentidad.RECHAZADO, None, ModeloNoDisponible(), IdentidadNoVerificada, None),
        (EstadoIdentidad.PENDIENTE, None, ModeloNoDisponible(), IdentidadPendiente, None),
        (EstadoIdentidad.VERIFICADO, None, ModeloNoDisponible(), ModeloNoDisponible, None),
    ],
    ids=[
        "404-antes-que-todo",
        "404-del-modelo",
        "falla-tecnica-antes-que-identidad",
        "consentimiento-caido",
        "identidad-caida",
        "identidad-antes-que-modelo",
        "pendiente",
        "sin-modelo",
    ],
)
async def test_ut_w11_19_orden_de_evaluacion_de_la_fase_a(
    escenario: Any,
    nuevo_comando: Any,
    identidad: Any,
    consentimiento: Any,
    modelo: Any,
    esperado: type[Exception],
    dependencia: str | None,
) -> None:
    escenario.identidad.respuesta = identidad
    if consentimiento is not None:
        escenario.consentimiento.respuesta = consentimiento
    if modelo is not None:
        escenario.modelos.respuesta = modelo
    with pytest.raises(esperado) as error:
        await escenario.caso().ejecutar(nuevo_comando())
    if dependencia:
        assert error.value.dependencia == dependencia
    assert escenario.ejecutor.llamadas == [] and escenario.repo.perfiles == {}


@pytest.mark.parametrize("puerto", ["identidad", "consentimiento", "modelos"])
async def test_un_error_inesperado_de_un_puerto_se_propaga(
    escenario: Any, nuevo_comando: Any, puerto: str
) -> None:
    getattr(escenario, puerto).respuesta = RuntimeError("defecto")
    with pytest.raises(RuntimeError):
        await escenario.caso().ejecutar(nuevo_comando())


@pytest.mark.parametrize("puerto", ["identidad", "consentimiento", "modelos"])
async def test_ut_w11_24_timeout_de_la_fase_a_es_dependencia_no_disponible(
    escenario: Any, nuevo_comando: Any, puerto: str
) -> None:
    escenario.config = ConfigPerfilamiento(dependencia_timeout_ms=20)
    getattr(escenario, puerto).demora_s = 0.2
    with pytest.raises(DependenciaNoDisponible) as error:
        await escenario.caso().ejecutar(nuevo_comando())
    assert (
        error.value.dependencia
        == {
            "identidad": "clientes.identidad",
            "consentimiento": "clientes.consentimiento",
            "modelos": "catalogo.modelo",
        }[puerto]
    )


# --- UT-W11-20 · edad --------------------------------------------------------------------


async def test_ut_w11_20_edad_a_la_fecha_del_reloj(escenario: Any, nuevo_comando: Any) -> None:
    perfil, _ = await escenario.caso().ejecutar(nuevo_comando())
    assert perfil.insumos["edad"] == Decimal(36)


@pytest.mark.parametrize(
    ("nacimiento", "edad"),
    [(date(1990, 10, 15), 36), (date(1990, 10, 16), 35), (date(2000, 2, 29), 26)],
)
def test_edad_en_anios_cumplidos(nacimiento: date, edad: int) -> None:
    assert edad_cumplida(nacimiento, date(2026, 10, 15)) == edad


@pytest.mark.parametrize("nacimiento", [date(2026, 10, 16), date(1905, 10, 15)])
async def test_ut_w11_20_fecha_futura_o_edad_mayor_a_120(
    escenario: Any, nuevo_comando: Any, nacimiento: date
) -> None:
    base = nuevo_comando()
    declarados = replace(base.datos_declarados, fecha_nacimiento=nacimiento)
    with pytest.raises(DatosDeclaradosInvalidos) as error:
        await escenario.caso().ejecutar(replace(base, datos_declarados=declarados))
    assert error.value.campos == ("datosDeclarados.fechaNacimiento",)
    assert _llamadas(escenario) == 0


async def test_edad_120_es_valida(escenario: Any, nuevo_comando: Any) -> None:
    base = nuevo_comando()
    declarados = replace(base.datos_declarados, fecha_nacimiento=date(1906, 10, 15))
    perfil, _ = await escenario.caso().ejecutar(replace(base, datos_declarados=declarados))
    assert perfil.insumos["edad"] == Decimal(120)


# --- UT-W11-21 · fuentes no consultadas --------------------------------------------------


async def test_ut_w11_21_c02_ingresos_no_autorizada_y_contexto_de_consulta(
    escenario: Any, nuevo_comando: Any, verificacion_de: Any
) -> None:
    escenario.consentimiento.respuesta = verificacion_de(
        DecisionConsentimiento.PERMITIDO_PARCIAL, frozenset({P, H, D})
    )
    perfil, _ = await escenario.caso().ejecutar(nuevo_comando())
    assert [(f.fuente, f.estado) for f in perfil.fuentes] == [
        (P, EstadoFuente.CONSULTADA),
        (H, EstadoFuente.CONSULTADA),
        (I, EstadoFuente.NO_AUTORIZADA),
        (D, EstadoFuente.CONSULTADA),
    ]
    sintetica = perfil.fuentes[2]
    assert (sintetica.campos_solicitados, sintetica.campos, sintetica.confianza) == ((), {}, None)
    ((solicitudes, contexto),) = escenario.ejecutor.llamadas
    assert [s.fuente for s in solicitudes] == [P, H, D]
    assert (contexto.consentimiento_id, contexto.proposito, contexto.deadline_ms) == (
        UUID("00000000-0000-4000-9000-000000000001"),
        PROPOSITO_PERFILAMIENTO,
        290,
    )
    assert "ingreso_mensual_estimado" not in perfil.insumos
    assert (perfil.resultado.puntaje, perfil.resultado.confianza) == (
        Decimal("75.00"),
        Decimal("0.87"),
    )


@pytest.mark.parametrize(
    ("decision", "permitidas", "esperados"),
    [
        (
            DecisionConsentimiento.PERMITIDO_PARCIAL,
            frozenset({P, I, D}),
            [
                EstadoFuente.NO_CONSULTADA,
                EstadoFuente.NO_AUTORIZADA,
                EstadoFuente.NO_CONSULTADA,
                EstadoFuente.NO_CONSULTADA,
            ],
        ),
        (
            DecisionConsentimiento.RECHAZADO,
            frozenset(),
            [EstadoFuente.NO_AUTORIZADA] * 4,
        ),
    ],
    ids=["C16", "C08"],
)
async def test_ut_w11_21_sin_consultas_las_fuentes_se_completan_en_orden(
    escenario: Any,
    nuevo_comando: Any,
    verificacion_de: Any,
    decision: DecisionConsentimiento,
    permitidas: frozenset[Fuente],
    esperados: list[EstadoFuente],
) -> None:
    escenario.consentimiento.respuesta = verificacion_de(decision, permitidas, "REVOCADO")
    perfil, _ = await escenario.caso().ejecutar(nuevo_comando())
    assert escenario.ejecutor.llamadas == []
    assert [f.estado for f in perfil.fuentes] == esperados
    assert perfil.resultado.estado is EstadoPerfil.INFORMACION_INSUFICIENTE
    assert perfil.resultado.accion is AccionSugerida.COMPLETAR_CONSENTIMIENTO
    assert set(perfil.insumos) == {"edad", "plazo_credito_meses", "suma_asegurada"}


async def test_solo_los_campos_disponibles_son_insumos(escenario: Any, nuevo_comando: Any) -> None:
    sin_moras = {"campos": {"moras_12m": ValorCampo(EstadoCampo.NO_DISPONIBLE, None)}}
    escenario.ejecutor.cambios = {H: sin_moras}
    perfil, _ = await escenario.caso().ejecutar(nuevo_comando())
    assert "moras_12m" not in perfil.insumos
    assert perfil.resultado.estado is EstadoPerfil.INFORMACION_INSUFICIENTE


# --- UT-W11-22 y 23 · telemetría y log ---------------------------------------------------


def _resultado_de(telemetria: Any, operacion: str) -> list[str]:
    return [s.attributes["result"] for s in telemetria.lista_spans() if s.name == operacion]


@pytest.mark.parametrize(
    ("preparar", "resultado"),
    [
        (lambda e, v: None, "ok"),
        (
            lambda e, v: setattr(
                e.ejecutor,
                "cambios",
                {D: {"estado": EstadoFuente.TIEMPO_AGOTADO, "campos": {}, "confianza": None}},
            ),
            "degradado",
        ),
        (
            lambda e, v: setattr(
                e.consentimiento, "respuesta", v(DecisionConsentimiento.RECHAZADO)
            ),
            "insuficiente",
        ),
        (lambda e, v: setattr(e.identidad, "respuesta", EstadoIdentidad.RECHAZADO), "rechazado"),
        (lambda e, v: setattr(e.identidad, "respuesta", ClienteNoEncontrado()), "rechazado"),
        (
            lambda e, v: setattr(
                e.modelos, "respuesta", DependenciaNoDisponible("catalogo.modelo")
            ),
            "error",
        ),
    ],
    ids=["calculado", "degradado", "insuficiente", "identidad", "cliente-404", "dependencia"],
)
async def test_ut_w11_22_resultado_de_la_operacion(
    telemetria: Any,
    escenario: Any,
    nuevo_comando: Any,
    verificacion_de: Any,
    preparar: Any,
    resultado: str,
) -> None:
    preparar(escenario, verificacion_de)
    with contextlib.suppress(Exception):  # aquí solo interesa la telemetría
        await escenario.caso().ejecutar(nuevo_comando())
    assert _resultado_de(telemetria, "perfilamiento") == [resultado]
    puntos = [
        p
        for p in telemetria.puntos("OperationDuration")
        if p.attributes["Operation"] == "perfilamiento"
    ]
    assert len(puntos) == 1 and puntos[0].count == 1


async def test_ut_w11_22_spans_hijos_por_dependencia(
    telemetria: Any, escenario: Any, nuevo_comando: Any
) -> None:
    escenario.modelos.respuesta = ModeloNoDisponible()
    with pytest.raises(ModeloNoDisponible):
        await escenario.caso().ejecutar(nuevo_comando())
    spans = {s.name: s for s in telemetria.lista_spans()}
    padre = spans["perfilamiento"]
    for nombre in ("clientes.identidad", "clientes.consentimiento", "catalogo.modelo"):
        hijo = spans[f"perfilamiento.dependencia.{nombre}"]
        assert hijo.parent is not None and hijo.parent.span_id == padre.context.span_id
        assert hijo.attributes["result"] == "ok"


async def test_ut_w11_22_dependencia_con_timeout(
    telemetria: Any, escenario: Any, nuevo_comando: Any
) -> None:
    escenario.config = ConfigPerfilamiento(dependencia_timeout_ms=20)
    escenario.identidad.demora_s = 0.2
    with pytest.raises(DependenciaNoDisponible):
        await escenario.caso().ejecutar(nuevo_comando())
    assert _resultado_de(telemetria, "perfilamiento.dependencia.clientes.identidad") == ["timeout"]
    assert _resultado_de(telemetria, "perfilamiento") == ["error"]


async def test_ut_w11_23_log_de_cierre_sin_valores(
    telemetria: Any, escenario: Any, nuevo_comando: Any
) -> None:
    escenario.ejecutor.cambios = {
        I: {"estado": EstadoFuente.NO_DISPONIBLE, "campos": {}, "confianza": None}
    }
    perfil, _ = await escenario.caso().ejecutar(nuevo_comando())
    (cierre,) = [l for l in telemetria.logs() if l["message"] == "perfil calculado"]
    assert (cierre["perfil_id"], cierre["estado"]) == (str(perfil.id), "DEGRADADO")
    assert cierre["operation"] == "perfilamiento"
    dependencias = cierre["dependencias"]
    assert isinstance(dependencias, list)
    assert [d["nombre"] for d in dependencias][3:] == [f"fuente.{f.value}" for f in Fuente]
    assert {d["nombre"] for d in dependencias[:3]} == {
        "clientes.identidad",
        "clientes.consentimiento",
        "catalogo.modelo",
    }
    assert {d["nombre"]: d["resultado"] for d in dependencias}[
        "fuente.FA_INGRESOS_AGREGADOS"
    ] == "error"
    assert all(set(d) == {"nombre", "ms", "resultado"} for d in dependencias)
    texto = telemetria.texto_logs() + telemetria.texto_spans()
    for centinela in CENTINELAS:
        assert centinela not in texto
    assert json.dumps(dependencias)


async def test_fuente_con_timeout_queda_como_timeout_en_el_log(
    telemetria: Any, escenario: Any, nuevo_comando: Any
) -> None:
    escenario.ejecutor.cambios = {
        D: {
            "estado": EstadoFuente.TIEMPO_AGOTADO,
            "campos": {},
            "confianza": None,
            "duracion_ms": None,
        }
    }
    await escenario.caso().ejecutar(nuevo_comando())
    (cierre,) = [l for l in telemetria.logs() if l["message"] == "perfil calculado"]
    da = cierre["dependencias"][-1]
    assert (da["nombre"], da["resultado"], da["ms"]) == (
        "fuente.DA_REGISTROS_PUBLICOS",
        "timeout",
        0,
    )
