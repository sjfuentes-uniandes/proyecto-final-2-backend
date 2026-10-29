"""ConciliarRecaudos (HU-W16, 16.11; plan §d; ADR 0013, decisión 5), con dobles de los puertos.

- Un ciclo toma hasta `lote` movimientos PENDIENTE cuya última actualización es anterior a
  `ahora - antiguedad_min_s`, los más antiguos primero, y consulta cada uno por su `claveRecaudo`.
- APROBADO o RECHAZADO: aplica el resultado con la guarda de PENDIENTE y escribe el evento en la
  misma transacción, como la continuación de la 16.10. Si la guarda no cambia la fila, sin evento.
- `COBRO_NO_ENCONTRADO`: reintenta el cobro con la misma `claveRecaudo` (P16-3) y aplica su
  resultado igual. Nunca se toma como rechazo.
- Un incierto registra su código: renueva `actualizado_en`, y el próximo intento se espacia.
- Un movimiento que falla no detiene a los demás. Logs y medición sin el pagador.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from dinero.application.recaudo import conciliar
from dinero.application.recaudo.conciliar import ConciliarRecaudos, Resultado
from dinero.application.recaudo.puertos import (
    CobroAprobado,
    CobroIncierto,
    CobroNoEncontrado,
    CobroRechazado,
    ConsultaCobro,
    ResultadoCobro,
    ResultadoConsulta,
    SolicitudCobro,
)
from dinero.domain.recaudo.modelos import EstadoRecaudo, Recaudo

pytestmark = pytest.mark.anyio
CREADO = datetime(2026, 10, 28, 15, 0, tzinfo=UTC)
AHORA = CREADO + timedelta(minutes=30)
PAGADOR = UUID("00000000-0000-4000-8000-000000000006")
ANTIGUEDAD_S = 300
LOTE = 10
# Los movimientos se crean 30 min antes de AHORA: por debajo de la alerta por defecto (1 h).
ALERTA_S = 3600


def pendiente(**cambios: Any) -> Recaudo:
    datos: dict[str, Any] = {
        "clave_recaudo": uuid4(),
        "cotizacion_id": uuid4(),
        "pagador_ref": PAGADOR,
        "monto": Decimal("64638.07"),
        "moneda": "COP",
        "periodicidad": "MENSUAL",
        "correlation_id": "corr-w16-conciliacion",
        "ahora": CREADO,
    }
    datos.update(cambios)
    return Recaudo.pendiente(**datos).registrar_incierto("ADAPTADOR_TIEMPO_AGOTADO", CREADO)


# --- Dobles --------------------------------------------------------------------------------------
@dataclass
class Tx:
    activa: bool = True


@dataclass
class Repo:
    movimientos: list[Recaudo] = field(default_factory=list)
    filas_resultado: int = 1
    falla_pendientes: Exception | None = None
    consultas: list[tuple[datetime, int]] = field(default_factory=list)
    resultados: list[tuple[Recaudo, bool]] = field(default_factory=list)

    async def pendientes(self, antes_de: datetime, lote: int) -> list[Recaudo]:
        self.consultas.append((antes_de, lote))
        if self.falla_pendientes is not None:
            raise self.falla_pendientes
        return list(self.movimientos)

    async def registrar_resultado(self, recaudo: Recaudo, tx: Tx | None = None) -> int:
        self.resultados.append((recaudo, tx is not None and tx.activa))
        return self.filas_resultado


@dataclass
class Transacciones:
    abiertas: int = 0

    @asynccontextmanager
    async def transaccion(self) -> AsyncIterator[Tx]:
        self.abiertas += 1
        tx = Tx()
        try:
            yield tx
        finally:
            tx.activa = False


@dataclass
class Eventos:
    agregados: list[tuple[Recaudo, bool]] = field(default_factory=list)

    async def agregar(self, recaudo: Recaudo, tx: Tx) -> None:
        self.agregados.append((recaudo, tx.activa))


Respuesta = ResultadoConsulta | Exception


@dataclass
class Pagos:
    """Responde por `claveRecaudo`; por defecto, la consulta sale APROBADO."""

    consultas_por_clave: dict[UUID, Respuesta] = field(default_factory=dict)
    cobros_por_clave: dict[UUID, ResultadoCobro | Exception] = field(default_factory=dict)
    consultas: list[ConsultaCobro] = field(default_factory=list)
    cobros: list[SolicitudCobro] = field(default_factory=list)

    async def consultar(self, consulta: ConsultaCobro) -> ResultadoConsulta:
        self.consultas.append(consulta)
        respuesta = self.consultas_por_clave.get(
            consulta.clave_recaudo, CobroAprobado("TX-SINT-C06")
        )
        if isinstance(respuesta, Exception):
            raise respuesta
        return respuesta

    async def cobrar(self, solicitud: SolicitudCobro) -> ResultadoCobro:
        self.cobros.append(solicitud)
        respuesta = self.cobros_por_clave.get(solicitud.clave_recaudo, CobroAprobado("TX-R"))
        if isinstance(respuesta, Exception):
            raise respuesta
        return respuesta


class Reloj:
    def ahora(self) -> datetime:
        return AHORA


@dataclass
class Escenario:
    repo: Repo = field(default_factory=Repo)
    transacciones: Transacciones = field(default_factory=Transacciones)
    eventos: Eventos = field(default_factory=Eventos)
    pagos: Pagos = field(default_factory=Pagos)

    def caso(self, **opciones: Any) -> ConciliarRecaudos[Tx]:
        parametros: dict[str, Any] = {
            "antiguedad_min_s": ANTIGUEDAD_S,
            "lote": LOTE,
            "intervalo_s": 60,
            "alerta_tras_s": ALERTA_S,
        }
        parametros.update(opciones)
        return ConciliarRecaudos(
            recaudos=self.repo,
            transacciones=self.transacciones,
            eventos=self.eventos,
            pagos=self.pagos,
            reloj=Reloj(),
            **parametros,
        )

    def con(self, *movimientos: Recaudo) -> "Escenario":
        self.repo.movimientos = list(movimientos)
        return self


def _registros(logs: Any) -> list[dict[str, Any]]:
    return [r for r in logs.registros() if r["message"] == conciliar.MENSAJE_CONCILIADO]


# --- Selección -----------------------------------------------------------------------------------
async def test_toma_los_pendientes_mas_antiguos_que_la_antiguedad_minima_hasta_el_lote() -> None:
    escenario = Escenario()
    await escenario.caso(antiguedad_min_s=300, lote=7).conciliar_una_vez()
    assert escenario.repo.consultas == [(AHORA - timedelta(seconds=300), 7)]


async def test_sin_pendientes_no_llama_al_adaptador() -> None:
    escenario = Escenario()
    assert await escenario.caso().conciliar_una_vez() == []
    assert escenario.pagos.consultas == [] and escenario.pagos.cobros == []
    assert escenario.transacciones.abiertas == 0


# --- Ramas por movimiento ------------------------------------------------------------------------
async def test_la_consulta_lleva_la_clave_el_pagador_y_la_correlacion_del_movimiento() -> None:
    movimiento = pendiente()
    escenario = Escenario().con(movimiento)
    await escenario.caso().conciliar_una_vez()
    assert escenario.pagos.consultas == [
        ConsultaCobro(
            clave_recaudo=movimiento.clave_recaudo,
            pagador_ref=PAGADOR,
            correlation_id="corr-w16-conciliacion",
        )
    ]


async def test_consulta_aprobada_confirma_con_su_evento_en_la_misma_transaccion(
    logs: Any,
) -> None:
    movimiento = pendiente()
    escenario = Escenario().con(movimiento)
    [conciliado] = await escenario.caso().conciliar_una_vez()

    assert conciliado.resultado is Resultado.CONFIRMADO and not conciliado.reintento
    [(registrado, en_tx)] = escenario.repo.resultados
    assert en_tx and registrado.estado is EstadoRecaudo.CONFIRMADO
    assert (registrado.transaccion_id, registrado.resuelto_en) == ("TX-SINT-C06", AHORA)
    # El código del incierto anterior se conserva al resolver.
    assert registrado.ultimo_incierto == "ADAPTADOR_TIEMPO_AGOTADO"
    assert escenario.eventos.agregados == [(registrado, True)]
    assert escenario.pagos.cobros == []
    [registro] = _registros(logs)
    assert registro["resultado"] == "confirmado" and registro["codigo"] is None


async def test_consulta_rechazada_rechaza_con_su_motivo_y_su_evento() -> None:
    movimiento = pendiente()
    escenario = Escenario().con(movimiento)
    escenario.pagos.consultas_por_clave[movimiento.clave_recaudo] = CobroRechazado(
        motivo="FONDOS_INSUFICIENTES", transaccion_id=None
    )
    [conciliado] = await escenario.caso().conciliar_una_vez()

    assert (conciliado.resultado, conciliado.codigo) == (
        Resultado.RECHAZADO,
        "FONDOS_INSUFICIENTES",
    )
    [(registrado, _)] = escenario.repo.resultados
    assert registrado.estado is EstadoRecaudo.RECHAZADO
    assert [r.estado for r, _ in escenario.eventos.agregados] == [EstadoRecaudo.RECHAZADO]


@pytest.mark.parametrize(
    "codigo",
    ["TIEMPO_AGOTADO", "FUENTE_NO_DISPONIBLE", "RESPUESTA_INVALIDA", "ADAPTADOR_NO_DISPONIBLE"],
)
async def test_consulta_incierta_renueva_la_antiguedad_con_su_codigo_y_sin_evento(
    codigo: str,
) -> None:
    movimiento = pendiente()
    escenario = Escenario().con(movimiento)
    escenario.pagos.consultas_por_clave[movimiento.clave_recaudo] = CobroIncierto(codigo)
    [conciliado] = await escenario.caso().conciliar_una_vez()

    assert (conciliado.resultado, conciliado.codigo) == (Resultado.INCIERTO, codigo)
    [(registrado, en_tx)] = escenario.repo.resultados
    assert en_tx and registrado.estado is EstadoRecaudo.PENDIENTE
    assert (registrado.ultimo_incierto, registrado.actualizado_en) == (codigo, AHORA)
    assert escenario.eventos.agregados == []
    assert escenario.pagos.cobros == []  # un incierto de la consulta no reintenta el cobro


@pytest.mark.parametrize(
    ("reintento", "resultado", "estado", "evento"),
    [
        (CobroAprobado("TX-R"), Resultado.CONFIRMADO, EstadoRecaudo.CONFIRMADO, True),
        (
            CobroRechazado(motivo="FONDOS_INSUFICIENTES", transaccion_id=None),
            Resultado.RECHAZADO,
            EstadoRecaudo.RECHAZADO,
            True,
        ),
        (CobroIncierto("FUENTE_NO_DISPONIBLE"), Resultado.INCIERTO, EstadoRecaudo.PENDIENTE, False),
    ],
    ids=["aprobado", "rechazado", "incierto"],
)
async def test_no_encontrado_reintenta_el_cobro_con_la_misma_clave_y_aplica_su_resultado(
    reintento: ResultadoCobro,
    resultado: Resultado,
    estado: EstadoRecaudo,
    evento: bool,
    logs: Any,
) -> None:
    movimiento = pendiente()
    escenario = Escenario().con(movimiento)
    escenario.pagos.consultas_por_clave[movimiento.clave_recaudo] = CobroNoEncontrado()
    escenario.pagos.cobros_por_clave[movimiento.clave_recaudo] = reintento
    [conciliado] = await escenario.caso().conciliar_una_vez()

    assert (conciliado.resultado, conciliado.reintento) == (resultado, True)
    # P16-3: la misma claveRecaudo (idempotente en el proveedor), el monto exacto y su correlación.
    assert escenario.pagos.cobros == [
        SolicitudCobro(
            clave_recaudo=movimiento.clave_recaudo,
            monto=Decimal("64638.07"),
            moneda="COP",
            pagador_ref=PAGADOR,
            correlation_id="corr-w16-conciliacion",
        )
    ]
    [(registrado, _)] = escenario.repo.resultados
    assert registrado.estado is estado  # nunca RECHAZADO por el «no existe»
    assert bool(escenario.eventos.agregados) is evento
    [registro] = _registros(logs)
    assert registro["reintento"] is True


@pytest.mark.parametrize(
    "respuesta",
    [CobroAprobado("TX-SINT-C06"), CobroRechazado(motivo=None, transaccion_id=None)],
    ids=["aprobado", "rechazado"],
)
async def test_si_la_guarda_no_cambia_ninguna_fila_no_hay_evento(
    respuesta: ResultadoConsulta, logs: Any
) -> None:
    """Otra tarea (la continuación u otra conciliación) ya lo resolvió."""
    movimiento = pendiente()
    escenario = Escenario().con(movimiento)
    escenario.repo.filas_resultado = 0
    escenario.pagos.consultas_por_clave[movimiento.clave_recaudo] = respuesta
    [conciliado] = await escenario.caso().conciliar_una_vez()

    assert conciliado.resultado is Resultado.YA_RESUELTO
    assert len(escenario.repo.resultados) == 1
    assert escenario.eventos.agregados == []
    [registro] = _registros(logs)
    assert registro["resultado"] == "ya_resuelto"


# --- Aislamiento de fallos -----------------------------------------------------------------------
async def test_un_movimiento_que_falla_no_detiene_a_los_demas(logs: Any) -> None:
    primero, roto, ultimo = pendiente(), pendiente(), pendiente()
    escenario = Escenario().con(primero, roto, ultimo)
    escenario.pagos.consultas_por_clave[roto.clave_recaudo] = RuntimeError(f"con {PAGADOR}")
    conciliados = await escenario.caso().conciliar_una_vez()

    assert [c.resultado for c in conciliados] == [
        Resultado.CONFIRMADO,
        Resultado.ERROR,
        Resultado.CONFIRMADO,
    ]
    assert [r.clave_recaudo for r, _ in escenario.repo.resultados] == [
        primero.clave_recaudo,
        ultimo.clave_recaudo,
    ]
    assert len(escenario.eventos.agregados) == 2
    [error] = [r for r in logs.de_nivel("ERROR") if r["message"] == conciliar.MENSAJE_FALLIDO]
    assert (error["clave_recaudo"], error["error_type"]) == (
        str(roto.clave_recaudo),
        "RuntimeError",
    )
    assert str(PAGADOR) not in logs.texto()


async def test_una_falla_al_registrar_tampoco_detiene_a_los_demas() -> None:
    primero, segundo = pendiente(), pendiente()
    escenario = Escenario().con(primero, segundo)
    escenario.pagos.cobros_por_clave[primero.clave_recaudo] = RuntimeError("caído")
    escenario.pagos.consultas_por_clave[primero.clave_recaudo] = CobroNoEncontrado()
    conciliados = await escenario.caso().conciliar_una_vez()
    assert [c.resultado for c in conciliados] == [Resultado.ERROR, Resultado.CONFIRMADO]


async def test_la_parada_se_atiende_entre_movimientos() -> None:
    movimientos = [pendiente() for _ in range(3)]
    escenario = Escenario().con(*movimientos)
    parada = asyncio.Event()

    class PagosQueParan(Pagos):
        async def consultar(self, consulta: ConsultaCobro) -> ResultadoConsulta:
            parada.set()  # llega SIGTERM durante la primera consulta
            return await super().consultar(consulta)

    escenario.pagos = PagosQueParan()
    conciliados = await escenario.caso().conciliar_una_vez(parada)
    # El movimiento en curso termina (y registra su resultado); los demás quedan para otra tarea.
    assert [c.clave_recaudo for c in conciliados] == [movimientos[0].clave_recaudo]
    assert len(escenario.pagos.consultas) == 1


# --- Trazas --------------------------------------------------------------------------------------
async def test_cada_movimiento_deja_un_log_y_una_medicion_sin_el_pagador(logs: Any) -> None:
    aprobado, incierto = pendiente(), pendiente()
    escenario = Escenario().con(aprobado, incierto)
    escenario.pagos.consultas_por_clave[incierto.clave_recaudo] = CobroIncierto("TIEMPO_AGOTADO")
    await escenario.caso().conciliar_una_vez()

    registros = _registros(logs)
    assert [
        (r["recaudo_id"], r["clave_recaudo"], r["resultado"], r["codigo"], r["reintento"])
        for r in registros
    ] == [
        (str(aprobado.recaudo_id), str(aprobado.clave_recaudo), "confirmado", None, False),
        (
            str(incierto.recaudo_id),
            str(incierto.clave_recaudo),
            "incierto",
            "TIEMPO_AGOTADO",
            False,
        ),
    ]
    # La medición (operación `recaudo.conciliar`, contracts/operaciones.md) con el resultado.
    mediciones = [
        r
        for r in logs.registros()
        if r["operation"] == conciliar.OPERACION and r["duration_ms"] is not None
    ]
    assert [m["result"] for m in mediciones] == ["confirmado", "incierto"]
    assert conciliar.OPERACION == "recaudo.conciliar"
    # Cada traza lleva la correlación del movimiento (la de su OfertaAceptada).
    assert {r["correlation_id"] for r in registros + mediciones} == {"corr-w16-conciliacion"}
    texto = logs.texto()
    assert str(PAGADOR) not in texto and "pagador" not in texto


# --- Bucle ---------------------------------------------------------------------------------------
async def test_el_bucle_espera_el_intervalo_concilia_y_para_con_la_senal() -> None:
    escenario = Escenario().con(pendiente())
    caso = escenario.caso(intervalo_s=0.01)
    parada = asyncio.Event()
    tarea = asyncio.create_task(caso.ejecutar(parada))
    while len(escenario.repo.consultas) < 2:
        await asyncio.sleep(0.005)
    parada.set()
    await asyncio.wait_for(tarea, 1)
    assert len(escenario.pagos.consultas) >= 2


async def test_el_bucle_espera_antes_del_primer_ciclo() -> None:
    """Al arrancar no consulta: las tareas que arrancan juntas no llaman a la vez al proveedor."""
    escenario = Escenario().con(pendiente())
    parada = asyncio.Event()
    tarea = asyncio.create_task(escenario.caso(intervalo_s=60).ejecutar(parada))
    await asyncio.sleep(0.05)
    parada.set()
    await asyncio.wait_for(tarea, 1)
    assert escenario.repo.consultas == []


async def test_un_ciclo_que_falla_se_registra_y_el_bucle_sigue(logs: Any) -> None:
    escenario = Escenario()
    escenario.repo.falla_pendientes = OSError(f"la base no responde ({PAGADOR})")
    caso = escenario.caso(intervalo_s=0.01)
    parada = asyncio.Event()
    tarea = asyncio.create_task(caso.ejecutar(parada))
    while len(escenario.repo.consultas) < 2:
        await asyncio.sleep(0.005)
    parada.set()
    await asyncio.wait_for(tarea, 1)
    errores = [r for r in logs.de_nivel("ERROR") if r["message"] == conciliar.MENSAJE_CICLO_FALLIDO]
    assert errores and errores[0]["error_type"] == "OSError"
    assert str(PAGADOR) not in logs.texto()


# --- PENDIENTE vencido (16.11, ajuste) -----------------------------------------------------------
def _mediciones(logs: Any) -> list[str]:
    return [
        r["result"]
        for r in logs.registros()
        if r["operation"] == conciliar.OPERACION and r["duration_ms"] is not None
    ]


@pytest.mark.parametrize("alerta_s", [ALERTA_S, 1800], ids=["muy-por-debajo", "en-el-umbral"])
async def test_un_incierto_por_debajo_del_umbral_sigue_incierto(alerta_s: int, logs: Any) -> None:
    """Creado hace 30 min (1800 s): «más de `alerta_tras_s`» no se cumple en el umbral exacto."""
    movimiento = pendiente()
    escenario = Escenario().con(movimiento)
    escenario.pagos.consultas_por_clave[movimiento.clave_recaudo] = CobroIncierto("TIEMPO_AGOTADO")
    [conciliado] = await escenario.caso(alerta_tras_s=alerta_s).conciliar_una_vez()

    assert (conciliado.resultado, conciliado.vencido) == (Resultado.INCIERTO, False)
    assert _mediciones(logs) == ["incierto"]
    assert [r["message"] for r in logs.de_nivel("ERROR")] == []


@pytest.mark.parametrize(
    ("consulta", "reintento"),
    [
        (CobroIncierto("TIEMPO_AGOTADO"), None),
        (CobroNoEncontrado(), CobroIncierto("TIEMPO_AGOTADO")),
    ],
    ids=["consulta-incierta", "reintento-incierto"],
)
async def test_un_incierto_vencido_se_mide_como_error_y_sigue_pendiente(
    consulta: ResultadoConsulta, reintento: ResultadoCobro | None, logs: Any
) -> None:
    """Más de `alerta_tras_s` desde `creado_en`: el resultado medido es `error`, el único (con
    `timeout`) que suma en `OperationErrors`. Sin tope: sigue PENDIENTE y se sigue conciliando."""
    movimiento = pendiente()
    escenario = Escenario().con(movimiento)
    escenario.pagos.consultas_por_clave[movimiento.clave_recaudo] = consulta
    if reintento is not None:
        escenario.pagos.cobros_por_clave[movimiento.clave_recaudo] = reintento
    [conciliado] = await escenario.caso(alerta_tras_s=1799).conciliar_una_vez()

    assert (conciliado.resultado, conciliado.vencido) == (Resultado.ERROR, True)
    assert conciliado.codigo == "TIEMPO_AGOTADO"
    [(registrado, _)] = escenario.repo.resultados
    assert registrado.estado is EstadoRecaudo.PENDIENTE
    assert registrado.ultimo_incierto == "TIEMPO_AGOTADO"
    assert escenario.eventos.agregados == []
    assert _mediciones(logs) == ["error"]
    [error] = [r for r in logs.de_nivel("ERROR") if r["message"] == conciliar.MENSAJE_VENCIDO]
    assert (error["recaudo_id"], error["clave_recaudo"]) == (
        str(movimiento.recaudo_id),
        str(movimiento.clave_recaudo),
    )
    assert (error["codigo"], error["antiguedad_s"]) == ("TIEMPO_AGOTADO", 1800)
    assert error["correlation_id"] == "corr-w16-conciliacion"
    # Un solo log del movimiento: el ERROR reemplaza al INFO de «conciliado».
    assert _registros(logs) == []
    texto = logs.texto()
    assert str(PAGADOR) not in texto and "pagador" not in texto


async def test_un_movimiento_antiguo_que_se_resuelve_no_es_un_error(logs: Any) -> None:
    movimiento = pendiente()
    escenario = Escenario().con(movimiento)
    [conciliado] = await escenario.caso(alerta_tras_s=60).conciliar_una_vez()
    assert (conciliado.resultado, conciliado.vencido) == (Resultado.CONFIRMADO, False)
    assert _mediciones(logs) == ["confirmado"]
    assert logs.de_nivel("ERROR") == []


def test_el_caso_de_uso_no_usa_float() -> None:
    import inspect

    assert "float(" not in inspect.getsource(conciliar)
