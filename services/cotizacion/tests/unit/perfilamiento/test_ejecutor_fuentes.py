"""EjecutorParalelo (UT-W12-10 a 17)."""

import asyncio
import gc
import time
from typing import Any
from uuid import UUID

import pytest
from cotizacion.application.perfilamiento.ejecutor_fuentes import EjecutorParalelo
from cotizacion.domain.perfilamiento.modelos import (
    ContextoConsulta,
    EstadoFuente,
    Fuente,
    ResultadoFuente,
    SolicitudFuente,
)
from solventa_common import correlation
from solventa_common.telemetry import ATRIBUTO_OPERACION

pytestmark = pytest.mark.anyio

CONTEXTO = ContextoConsulta(UUID(int=1), UUID(int=2), "PERFILAMIENTO_PRECIO", 290)
TODAS = [SolicitudFuente(f, ("x",)) for f in Fuente]
NOMBRE = "perfilamiento.dependencia.fuente.{}"


class PuertoQueRegistra:
    """Duerme `demoras[fuente]` segundos (0,1 por defecto) y registra inicio, fin y cancelación.

    Si `errores[fuente]` existe, lanza esa excepción en lugar de responder.
    """

    def __init__(
        self,
        demoras: dict[Fuente, float] | None = None,
        errores: dict[Fuente, Exception] | None = None,
    ) -> None:
        self.demoras = demoras or {}
        self.errores = errores or {}
        self.inicios: dict[Fuente, float] = {}
        self.fines: dict[Fuente, float] = {}
        self.canceladas: list[Fuente] = []
        self.contextos: list[ContextoConsulta] = []

    async def consultar(self, solicitud: SolicitudFuente, contexto: ContextoConsulta) -> Any:
        fuente = solicitud.fuente
        self.inicios[fuente] = time.perf_counter()
        self.contextos.append(contexto)
        try:
            await asyncio.sleep(self.demoras.get(fuente, 0.1))
        except asyncio.CancelledError:
            self.canceladas.append(fuente)
            raise
        self.fines[fuente] = time.perf_counter()
        if fuente in self.errores:
            raise self.errores[fuente]
        return ResultadoFuente(
            fuente, EstadoFuente.CONSULTADA, solicitud.campos, {}, "3.1", None, None, 100, "sim"
        )


async def test_ut_w12_10_las_ramas_empiezan_juntas() -> None:
    puerto = PuertoQueRegistra()
    inicio = time.perf_counter()
    resultados = await EjecutorParalelo(puerto, 300).ejecutar(TODAS, CONTEXTO)
    total = time.perf_counter() - inicio
    assert max(puerto.inicios.values()) < min(puerto.fines.values())
    assert total < 0.2
    assert all(r.estado is EstadoFuente.CONSULTADA for r in resultados)


async def test_ut_w12_11_resultados_en_el_orden_de_las_solicitudes() -> None:
    fuentes = list(Fuente)
    # La primera solicitud es la más lenta: terminan en orden inverso.
    puerto = PuertoQueRegistra({f: 0.01 * (len(fuentes) - i) for i, f in enumerate(fuentes)})
    # Las ramas se separan por 10 ms: una recolección gen2 bajo cobertura (30–40 ms) dentro de la
    # medición invertía el orden. Se recolecta antes para que no caiga en la ventana.
    gc.collect()
    resultados = await EjecutorParalelo(puerto, 300).ejecutar(TODAS, CONTEXTO)
    assert [r.fuente for r in resultados] == fuentes
    assert sorted(puerto.fines, key=puerto.fines.__getitem__) == list(reversed(fuentes))


async def test_ut_w12_12_timeout_por_rama() -> None:
    lenta = Fuente.DA_REGISTROS_PUBLICOS
    puerto = PuertoQueRegistra({lenta: 1.0, **{f: 0.01 for f in Fuente if f is not lenta}})
    inicio = time.perf_counter()
    resultados = await EjecutorParalelo(puerto, 50).ejecutar(TODAS, CONTEXTO)
    assert time.perf_counter() - inicio < 0.15
    por_fuente = {r.fuente: r for r in resultados}
    agotada = por_fuente.pop(lenta)
    assert agotada.estado is EstadoFuente.TIEMPO_AGOTADO
    assert agotada.duracion_ms is not None and 45 <= agotada.duracion_ms < 100
    assert agotada.campos_solicitados == ("x",) and agotada.campos == {}
    assert (agotada.version_fuente, agotada.confianza, agotada.proveedor) == (None, None, None)
    assert agotada.consultada_en is None
    assert all(r.estado is EstadoFuente.CONSULTADA for r in por_fuente.values())


async def test_ut_w12_13_la_rama_lenta_se_cancela() -> None:
    lenta = Fuente.FA_INGRESOS_AGREGADOS
    puerto = PuertoQueRegistra({lenta: 1.0})
    await EjecutorParalelo(puerto, 50).ejecutar([SolicitudFuente(lenta, ("x",))], CONTEXTO)
    assert puerto.canceladas == [lenta]
    assert lenta not in puerto.fines


async def test_ut_w12_14_una_excepcion_no_afecta_a_las_demas(telemetria: Any) -> None:
    caida = Fuente.FA_HISTORIAL_PAGOS_12M
    puerto = PuertoQueRegistra(errores={caida: ValueError("cliente 1234 sin datos")})
    resultados = await EjecutorParalelo(puerto, 300).ejecutar(TODAS, CONTEXTO)
    estados = {r.fuente: r.estado for r in resultados}
    assert estados.pop(caida) is EstadoFuente.NO_DISPONIBLE
    assert set(estados.values()) == {EstadoFuente.CONSULTADA}
    # Solo el tipo del error: nunca su mensaje.
    aviso = [linea for linea in telemetria.logs() if linea.get("error_type") == "ValueError"]
    assert aviso and aviso[0]["fuente"] == caida.value
    assert "1234" not in telemetria.texto_logs() + telemetria.texto_spans()


async def test_la_cancelacion_del_recorrido_se_propaga() -> None:
    puerto = PuertoQueRegistra({f: 1.0 for f in Fuente})
    tarea = asyncio.create_task(EjecutorParalelo(puerto, 500).ejecutar(TODAS, CONTEXTO))
    await asyncio.sleep(0.02)
    tarea.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tarea
    assert sorted(puerto.canceladas) == sorted(Fuente)


async def test_ut_w12_15_un_span_y_una_metrica_por_rama(telemetria: Any) -> None:
    lenta, caida = Fuente.DA_REGISTROS_PUBLICOS, Fuente.FA_INGRESOS_AGREGADOS
    puerto = PuertoQueRegistra({lenta: 1.0}, {caida: RuntimeError("x")})
    token = correlation.establecer("corr-w12")
    try:
        await EjecutorParalelo(puerto, 150).ejecutar(TODAS, CONTEXTO)
    finally:
        correlation.restablecer(token)
    spans = {s.name: s for s in telemetria.lista_spans()}
    esperados = {
        NOMBRE.format(f.value): "timeout" if f is lenta else "error" if f is caida else "ok"
        for f in Fuente
    }
    assert set(spans) == set(esperados)
    for nombre, resultado in esperados.items():
        atributos = spans[nombre].attributes or {}
        assert atributos["result"] == resultado
        assert atributos[correlation.ATRIBUTO_CORRELACION] == "corr-w12"
    operaciones = {p.attributes[ATRIBUTO_OPERACION] for p in telemetria.puntos("OperationDuration")}
    assert operaciones == set(esperados)


async def test_ut_w12_16_el_contexto_llega_igual_a_cada_rama() -> None:
    puerto = PuertoQueRegistra({f: 0 for f in Fuente})
    await EjecutorParalelo(puerto, 300).ejecutar(TODAS, CONTEXTO)
    assert puerto.contextos == [CONTEXTO] * len(TODAS)
    assert {c.deadline_ms for c in puerto.contextos} == {290}


async def test_ut_w12_17_sin_solicitudes_no_crea_spans(telemetria: Any) -> None:
    puerto = PuertoQueRegistra()
    assert await EjecutorParalelo(puerto, 300).ejecutar([], CONTEXTO) == []
    assert puerto.inicios == {}
    assert telemetria.lista_spans() == []
