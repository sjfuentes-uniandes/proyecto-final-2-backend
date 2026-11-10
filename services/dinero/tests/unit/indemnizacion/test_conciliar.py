"""ConciliarIndemnizaciones (HU-W23; DS3-18), con dobles de los puertos.

Reclama un lote (transacción corta), consulta después y aplica el resultado en la misma fila. Si la
pasarela no conoce la referencia, reintenta el desembolso con la misma referencia. Un incierto la
deja PENDIENTE. Un movimiento que falla no detiene a los demás.
"""

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from dinero.application.indemnizacion import conciliar
from dinero.application.indemnizacion.conciliar import ConciliarIndemnizaciones, Resultado
from dinero.application.indemnizacion.puertos import (
    DesembolsoConfirmado,
    DesembolsoIncierto,
    DesembolsoNoEncontrado,
    DesembolsoRechazado,
)
from dinero.domain.indemnizacion.modelos import EstadoIndemnizacion

pytestmark = pytest.mark.anyio


class Escenario:
    def __init__(
        self,
        d: SimpleNamespace,
        *,
        consultas: list[Any],
        desembolsos: list[Any] | None = None,
        minutos: float = 10,
        alerta_tras_s: float = 3600,
    ) -> None:
        self.d = d
        self.repo = d.Repo()
        self.transacciones = d.Transacciones()
        self.eventos = d.Eventos()
        self.pagos = d.Pagos(consultas=consultas, desembolsos=desembolsos or [])
        self.ahora = d.AHORA + timedelta(minutes=minutos)
        self.reloj = d.Reloj(self.ahora)
        self.alerta_tras_s = alerta_tras_s

    def sembrar(self, *, hace_min: float = 10, **cambios: Any) -> Any:
        i = self.d.nueva(ahora=self.ahora - timedelta(minutes=hace_min), **cambios)
        self.repo.filas[i.siniestro_id] = i
        return i

    def caso(self, lote: int = 10) -> ConciliarIndemnizaciones[Any]:
        return ConciliarIndemnizaciones(
            indemnizaciones=self.repo,
            transacciones=self.transacciones,
            eventos=self.eventos,
            pagos=self.pagos,
            reloj=self.reloj,
            intervalo_s=0.01,
            antiguedad_min_s=300,
            lote=lote,
            reclamo_s=60,
            alerta_tras_s=self.alerta_tras_s,
        )


async def test_confirma_la_misma_fila_sin_sumar_intento(d: Any, logs: Any) -> None:
    e = Escenario(d, consultas=[DesembolsoConfirmado("TRX-L")])
    pendiente = e.sembrar()
    pendiente = e.repo.filas[pendiente.siniestro_id] = pendiente.registrar_incierto(
        "ADAPTADOR_TIEMPO_AGOTADO", e.ahora - timedelta(minutes=9)
    )
    [conciliada] = await e.caso().conciliar_una_vez()
    assert conciliada.resultado is Resultado.CONFIRMADA and not conciliada.reintento
    fila = e.repo.filas[pendiente.siniestro_id]
    assert fila.indemnizacion_id == pendiente.indemnizacion_id  # la misma fila
    assert (fila.estado, fila.transaccion_id, fila.intentos) == (
        EstadoIndemnizacion.CONFIRMADA,
        "TRX-L",
        1,
    )
    assert fila.reclamado_hasta is None
    [(evento, _)] = e.eventos.agregados
    assert evento == fila
    assert e.pagos.solicitudes == []  # solo consultó
    [(antes_de, ahora, hasta, lote)] = e.repo.reclamos
    assert (ahora - antes_de, hasta - ahora, lote) == (
        timedelta(seconds=300),
        timedelta(seconds=60),
        10,
    )
    [info] = [r for r in logs.registros() if r["message"] == conciliar.MENSAJE_CONCILIADA]
    assert (info["resultado"], info["referencia"]) == ("confirmada", "IND-88-2026-0442-01")
    assert info["correlation_id"] == "corr-w23"


async def test_un_rechazo_queda_rechazada_con_su_motivo(d: Any) -> None:
    e = Escenario(d, consultas=[DesembolsoRechazado("BENEFICIARIO_NO_HABILITADO", None)])
    i = e.sembrar()
    [conciliada] = await e.caso().conciliar_una_vez()
    assert (conciliada.resultado, conciliada.codigo) == (
        Resultado.RECHAZADA,
        "BENEFICIARIO_NO_HABILITADO",
    )
    assert e.repo.filas[i.siniestro_id].estado is EstadoIndemnizacion.RECHAZADA


async def test_si_la_pasarela_no_la_conoce_reintenta_con_la_misma_referencia(d: Any) -> None:
    e = Escenario(
        d, consultas=[DesembolsoNoEncontrado()], desembolsos=[DesembolsoConfirmado("TRX-R")]
    )
    i = e.sembrar()
    [conciliada] = await e.caso().conciliar_una_vez()
    assert conciliada.resultado is Resultado.CONFIRMADA and conciliada.reintento
    [solicitud] = e.pagos.solicitudes
    assert (solicitud.referencia, solicitud.beneficiario_ref) == (i.referencia, i.beneficiario_ref)
    assert e.repo.filas[i.siniestro_id].intentos == 1  # el reintento sí es un intento de pago


async def test_un_incierto_de_la_consulta_la_deja_pendiente_y_no_cuenta_intento(
    d: Any, telemetria: Any
) -> None:
    e = Escenario(d, consultas=[DesembolsoIncierto("ADAPTADOR_NO_DISPONIBLE")])
    i = e.sembrar()
    [conciliada] = await e.caso().conciliar_una_vez()
    assert (conciliada.resultado, conciliada.codigo) == (
        Resultado.INCIERTO,
        "ADAPTADOR_NO_DISPONIBLE",
    )
    fila = e.repo.filas[i.siniestro_id]
    assert (fila.estado, fila.intentos, fila.ultimo_incierto) == (
        EstadoIndemnizacion.PENDIENTE,
        0,
        "ADAPTADOR_NO_DISPONIBLE",
    )
    assert e.eventos.agregados == []
    spans = [s for s in telemetria.lista_spans() if s.name == "indemnizacion.conciliar"]
    assert spans[-1].attributes["result"] == "incierto"


async def test_un_incierto_del_reintento_si_cuenta_el_intento(d: Any) -> None:
    e = Escenario(
        d, consultas=[DesembolsoNoEncontrado()], desembolsos=[DesembolsoIncierto("HTTP_503")]
    )
    i = e.sembrar()
    await e.caso().conciliar_una_vez()
    assert e.repo.filas[i.siniestro_id].intentos == 1


async def test_ya_resuelta_por_otra_tarea_cuenta_un_duplicado_sin_evento(d: Any) -> None:
    e = Escenario(d, consultas=[DesembolsoConfirmado("TRX-L")])
    i = e.sembrar()
    e.repo.filas_resultado = 0  # la guarda no cambia la fila
    [conciliada] = await e.caso().conciliar_una_vez()
    assert conciliada.resultado is Resultado.YA_RESUELTO
    assert e.eventos.agregados == [] and e.repo.duplicados == [i.indemnizacion_id]


async def test_el_lote_acota_y_atiende_las_mas_antiguas_primero(d: Any) -> None:
    e = Escenario(d, consultas=[DesembolsoConfirmado("TRX")])
    antigua = e.sembrar(hace_min=60)
    media = e.sembrar(hace_min=30)
    e.sembrar(hace_min=20)
    conciliadas = await e.caso(lote=2).conciliar_una_vez()
    assert [c.indemnizacion_id for c in conciliadas] == [
        antigua.indemnizacion_id,
        media.indemnizacion_id,
    ]


async def test_no_toma_las_recientes_las_resueltas_ni_las_reclamadas(d: Any) -> None:
    e = Escenario(d, consultas=[DesembolsoConfirmado("TRX")])
    e.sembrar(hace_min=2)  # reciente
    resuelta = e.sembrar(hace_min=60)
    e.repo.filas[resuelta.siniestro_id] = resuelta.confirmar("T", e.ahora)
    reclamada = e.sembrar(hace_min=60)
    e.repo.filas[reclamada.siniestro_id] = reclamada.reclamar(e.ahora + timedelta(seconds=30))
    assert await e.caso().conciliar_una_vez() == []
    assert e.pagos.consultadas == []


async def test_un_movimiento_que_falla_no_detiene_a_los_demas(d: Any, logs: Any) -> None:
    e = Escenario(d, consultas=[DesembolsoConfirmado("TRX")])
    primera = e.sembrar(hace_min=60, beneficiario_ref="ben-sint-centinela")
    segunda = e.sembrar(hace_min=30)
    llamadas = 0
    original = e.pagos.consultar_desembolso

    async def consultar(consulta: Any) -> Any:
        nonlocal llamadas
        llamadas += 1
        if llamadas == 1:
            raise RuntimeError("falla con datos sensibles")
        return await original(consulta)

    e.pagos.consultar_desembolso = consultar  # type: ignore[method-assign]
    conciliadas = await e.caso().conciliar_una_vez()
    assert [c.resultado for c in conciliadas] == [Resultado.ERROR, Resultado.CONFIRMADA]
    assert e.repo.filas[primera.siniestro_id].estado is EstadoIndemnizacion.PENDIENTE
    assert e.repo.filas[segunda.siniestro_id].estado is EstadoIndemnizacion.CONFIRMADA
    [error] = [r for r in logs.de_nivel("ERROR") if r["message"] == conciliar.MENSAJE_FALLIDA]
    assert error["error_type"] == "RuntimeError"
    assert "datos sensibles" not in logs.texto() and "ben-sint-centinela" not in logs.texto()


async def test_un_incierto_vencido_se_mide_como_error_y_sigue_pendiente(
    d: Any, logs: Any, telemetria: Any
) -> None:
    e = Escenario(d, consultas=[DesembolsoIncierto("HTTP_503")], minutos=200, alerta_tras_s=3600)
    i = e.sembrar(hace_min=120)
    [conciliada] = await e.caso().conciliar_una_vez()
    assert conciliada.vencido and conciliada.resultado is Resultado.ERROR
    assert e.repo.filas[i.siniestro_id].estado is EstadoIndemnizacion.PENDIENTE
    [error] = [r for r in logs.de_nivel("ERROR") if r["message"] == conciliar.MENSAJE_VENCIDA]
    assert error["antiguedad_s"] == 7200 and error["codigo"] == "HTTP_503"
    spans = [s for s in telemetria.lista_spans() if s.name == "indemnizacion.conciliar"]
    assert spans[-1].attributes["result"] == "error"


async def test_la_parada_se_atiende_entre_movimientos(d: Any) -> None:
    e = Escenario(d, consultas=[DesembolsoConfirmado("TRX")])
    e.sembrar(hace_min=60)
    e.sembrar(hace_min=50)
    parada = asyncio.Event()
    parada.set()
    assert await e.caso().conciliar_una_vez(parada) == []
    assert len(e.repo.reclamos) == 1  # reclamó, pero no consultó a nadie


async def test_el_bucle_espera_el_intervalo_concilia_y_se_detiene(d: Any) -> None:
    e = Escenario(d, consultas=[DesembolsoConfirmado("TRX")])
    e.sembrar(hace_min=60)
    parada = asyncio.Event()
    tarea = asyncio.create_task(e.caso().ejecutar(parada))
    for _ in range(200):
        if e.eventos.agregados:
            break
        await asyncio.sleep(0.01)
    parada.set()
    await asyncio.wait_for(tarea, 2)
    assert len(e.eventos.agregados) == 1


async def test_un_ciclo_que_falla_se_registra_y_el_bucle_sigue(d: Any, logs: Any) -> None:
    e = Escenario(d, consultas=[DesembolsoConfirmado("TRX")])
    e.repo.falla_al_reclamar = RuntimeError("base caída con datos")
    parada = asyncio.Event()
    tarea = asyncio.create_task(e.caso().ejecutar(parada))
    for _ in range(200):
        if logs.de_nivel("ERROR"):
            break
        await asyncio.sleep(0.01)
    parada.set()
    await asyncio.wait_for(tarea, 2)
    [error] = [
        r for r in logs.de_nivel("ERROR") if r["message"] == conciliar.MENSAJE_CICLO_FALLIDO
    ][:1]
    assert error["error_type"] == "RuntimeError" and "datos" not in error["error_type"]
