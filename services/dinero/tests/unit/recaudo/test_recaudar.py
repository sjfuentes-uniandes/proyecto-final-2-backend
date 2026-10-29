"""RecaudarPrima (HU-W16, 16.10; plan §d; ADR 0013, decisiones 2, 4 y 5), con dobles de los puertos.

- `registrar` corre en la transacción del inbox: crea el movimiento PENDIENTE y, solo si es nuevo,
  devuelve la continuación que cobra (modo b). Si no es nuevo, no hay cobro.
- La continuación cobra y, en otra transacción, aplica el resultado con la guarda de PENDIENTE y
  escribe el evento en el outbox, solo si cambió la fila y el estado es final. No lanza por fallos
  del adaptador (el puerto los devuelve como `CobroIncierto`); solo si falla la base.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from dinero.application.recaudo.puertos import (
    CobroAprobado,
    CobroIncierto,
    CobroRechazado,
    Creacion,
    ResultadoCobro,
    SolicitudCobro,
)
from dinero.application.recaudo.recaudar import OfertaAceptada, OfertaInvalida, RecaudarPrima
from dinero.domain.recaudo.modelos import MOTIVO_NO_INFORMADO, EstadoRecaudo, Recaudo

pytestmark = pytest.mark.anyio
AHORA = datetime(2026, 10, 28, 15, 0, tzinfo=UTC)
DESPUES = AHORA + timedelta(seconds=2)
PAGADOR = UUID("00000000-0000-4000-8000-000000000001")


# --- Dobles --------------------------------------------------------------------------------------
@dataclass
class Tx:
    nombre: str
    activa: bool = True
    confirmada: bool = False


@dataclass
class Repo:
    existente: Recaudo | None = None
    filas_resultado: int = 1
    creados: list[tuple[Recaudo, Tx]] = field(default_factory=list)
    resultados: list[tuple[Recaudo, Tx | None, bool]] = field(default_factory=list)

    async def crear_pendiente(self, recaudo: Recaudo, tx: Tx | None = None) -> Creacion:
        assert tx is not None and tx.activa
        self.creados.append((recaudo, tx))
        if self.existente is not None:
            return Creacion(self.existente, nuevo=False)
        return Creacion(recaudo, nuevo=True)

    async def por_clave(self, clave_recaudo: UUID, tx: Tx | None = None) -> Recaudo | None:
        raise AssertionError("no se usa")

    async def registrar_resultado(self, recaudo: Recaudo, tx: Tx | None = None) -> int:
        self.resultados.append((recaudo, tx, tx is not None and tx.activa))
        return self.filas_resultado


@dataclass
class Transacciones:
    falla: Exception | None = None
    abiertas: list[Tx] = field(default_factory=list)

    @asynccontextmanager
    async def transaccion(self) -> AsyncIterator[Tx]:
        if self.falla is not None:
            raise self.falla
        tx = Tx(f"resultado-{len(self.abiertas)}")
        self.abiertas.append(tx)
        try:
            yield tx
            tx.confirmada = True
        finally:
            tx.activa = False


@dataclass
class Eventos:
    agregados: list[tuple[Recaudo, Tx, bool]] = field(default_factory=list)

    async def agregar(self, recaudo: Recaudo, tx: Tx) -> None:
        self.agregados.append((recaudo, tx, tx.activa))


@dataclass
class Pagos:
    resultado: ResultadoCobro = field(default_factory=lambda: CobroAprobado("TX-SINT-C01"))
    solicitudes: list[SolicitudCobro] = field(default_factory=list)

    async def cobrar(self, solicitud: SolicitudCobro) -> ResultadoCobro:
        self.solicitudes.append(solicitud)
        return self.resultado


class Reloj:
    def __init__(self) -> None:
        self.instantes = [AHORA, DESPUES]

    def ahora(self) -> datetime:
        return self.instantes.pop(0) if len(self.instantes) > 1 else self.instantes[0]


@dataclass
class Escenario:
    repo: Repo = field(default_factory=Repo)
    transacciones: Transacciones = field(default_factory=Transacciones)
    eventos: Eventos = field(default_factory=Eventos)
    pagos: Pagos = field(default_factory=Pagos)
    reloj: Reloj = field(default_factory=Reloj)

    def caso(self) -> RecaudarPrima[Tx]:
        return RecaudarPrima(
            recaudos=self.repo,
            transacciones=self.transacciones,
            eventos=self.eventos,
            pagos=self.pagos,
            reloj=self.reloj,
        )


def oferta(**cambios: Any) -> OfertaAceptada:
    datos: dict[str, Any] = {
        "clave_recaudo": uuid4(),
        "cotizacion_id": uuid4(),
        "pagador_ref": PAGADOR,
        "monto": Decimal("64638.07"),
        "moneda": "COP",
        "periodicidad": "MENSUAL",
        "correlation_id": "corr-w16-cobro",
    }
    datos.update(cambios)
    return OfertaAceptada(**datos)


async def _registrar_y_cobrar(escenario: Escenario, **cambios: Any) -> Recaudo:
    caso = escenario.caso()
    inbox = Tx("inbox")
    continuacion = await caso.registrar(oferta(**cambios), inbox)
    assert continuacion is not None
    inbox.activa = False  # el consumidor hace commit antes de la continuación
    await continuacion()
    [(creado, _)] = escenario.repo.creados
    return creado


# --- registrar: la parte transaccional -------------------------------------------------------------
async def test_una_oferta_nueva_crea_el_pendiente_en_la_transaccion_del_inbox_sin_cobrar() -> None:
    escenario = Escenario()
    entrada = oferta()
    inbox = Tx("inbox")
    continuacion = await escenario.caso().registrar(entrada, inbox)

    assert continuacion is not None
    [(recaudo, tx)] = escenario.repo.creados
    assert tx is inbox
    assert recaudo.estado is EstadoRecaudo.PENDIENTE
    assert (recaudo.clave_recaudo, recaudo.cotizacion_id, recaudo.pagador_ref) == (
        entrada.clave_recaudo,
        entrada.cotizacion_id,
        PAGADOR,
    )
    assert (recaudo.monto, recaudo.moneda.value, recaudo.periodicidad) == (
        Decimal("64638.07"),
        "COP",
        "MENSUAL",
    )
    assert (recaudo.creado_en, recaudo.correlation_id) == (AHORA, "corr-w16-cobro")
    # El cobro va en la continuación, después del commit: aún no se llamó al adaptador.
    assert escenario.pagos.solicitudes == []
    assert escenario.repo.resultados == [] and escenario.transacciones.abiertas == []


async def test_un_movimiento_que_ya_existe_no_se_cobra(logs: Any) -> None:
    """Otro eventId con la misma claveRecaudo: sin continuación ni llamada al adaptador. Si sigue
    PENDIENTE, lo resuelve la conciliación (16.11)."""
    existente = Recaudo.pendiente(
        clave_recaudo=uuid4(),
        cotizacion_id=uuid4(),
        pagador_ref=PAGADOR,
        monto=Decimal(64638),
        moneda="COP",
        periodicidad="MENSUAL",
        correlation_id="corr-anterior",
        ahora=AHORA,
    )
    escenario = Escenario(repo=Repo(existente=existente))
    continuacion = await escenario.caso().registrar(
        oferta(clave_recaudo=existente.clave_recaudo), Tx("inbox")
    )
    assert continuacion is None
    assert escenario.pagos.solicitudes == []
    assert escenario.repo.resultados == [] and escenario.eventos.agregados == []
    [registro] = logs.de_nivel("INFO")
    assert registro["clave_recaudo"] == str(existente.clave_recaudo)
    assert registro["recaudo_id"] == str(existente.recaudo_id)
    assert registro["estado"] == "PENDIENTE"
    assert str(PAGADOR) not in logs.texto()


@pytest.mark.parametrize(
    ("cambios", "campo"),
    [
        ({"moneda": "USD"}, "moneda"),
        ({"monto": Decimal("64638.001")}, "monto"),
        ({"monto": Decimal(0)}, "monto"),
        ({"monto": Decimal("0.00")}, "monto"),
        ({"periodicidad": ""}, "periodicidad"),
    ],
)
async def test_una_oferta_que_no_cumple_el_movimiento_es_invalida_y_no_se_guarda(
    cambios: dict[str, Any], campo: str
) -> None:
    escenario = Escenario()
    with pytest.raises(OfertaInvalida, match=campo):
        await escenario.caso().registrar(oferta(**cambios), Tx("inbox"))
    assert escenario.repo.creados == [] and escenario.pagos.solicitudes == []


# --- La continuación: cobro y resultado ------------------------------------------------------------
async def test_la_solicitud_lleva_la_clave_el_monto_exacto_y_la_correlacion() -> None:
    escenario = Escenario()
    creado = await _registrar_y_cobrar(escenario, monto=Decimal("64638.10"))
    [solicitud] = escenario.pagos.solicitudes
    assert solicitud == SolicitudCobro(
        clave_recaudo=creado.clave_recaudo,
        monto=Decimal("64638.10"),
        moneda="COP",
        pagador_ref=PAGADOR,
        correlation_id="corr-w16-cobro",
    )
    assert str(solicitud.monto) == "64638.10"  # sin cambio de escala ni float


async def test_aprobado_confirma_y_escribe_el_evento_en_la_misma_transaccion(logs: Any) -> None:
    escenario = Escenario()
    creado = await _registrar_y_cobrar(escenario)

    [tx] = escenario.transacciones.abiertas
    [(resuelto, tx_resultado, dentro)] = escenario.repo.resultados
    assert (tx_resultado, dentro) == (tx, True)
    assert resuelto.estado is EstadoRecaudo.CONFIRMADO
    assert resuelto.transaccion_id == "TX-SINT-C01"
    assert (resuelto.recaudo_id, resuelto.resuelto_en) == (creado.recaudo_id, DESPUES)
    [(evento, tx_evento, evento_dentro)] = escenario.eventos.agregados
    assert (evento, tx_evento, evento_dentro) == (resuelto, tx, True)
    assert tx.confirmada
    [registro] = [r for r in logs.registros() if r.get("estado") == "CONFIRMADO"]
    assert (registro["clave_recaudo"], registro["recaudo_id"]) == (
        str(creado.clave_recaudo),
        str(creado.recaudo_id),
    )
    assert str(PAGADOR) not in logs.texto()


async def test_rechazado_rechaza_con_su_motivo_y_escribe_el_evento() -> None:
    escenario = Escenario(pagos=Pagos(CobroRechazado("FONDOS_INSUFICIENTES", "TX-R-01")))
    await _registrar_y_cobrar(escenario)

    [tx] = escenario.transacciones.abiertas
    [(resuelto, tx_resultado, _)] = escenario.repo.resultados
    assert tx_resultado is tx
    assert resuelto.estado is EstadoRecaudo.RECHAZADO
    assert (resuelto.motivo, resuelto.transaccion_id) == ("FONDOS_INSUFICIENTES", "TX-R-01")
    assert [(e, t, d) for e, t, d in escenario.eventos.agregados] == [(resuelto, tx, True)]


@pytest.mark.parametrize("motivo", [None, "", "fondos insuficientes del cliente Juan Pérez"])
async def test_rechazado_sin_motivo_o_con_texto_libre_es_motivo_no_informado(
    motivo: str | None, logs: Any
) -> None:
    escenario = Escenario(pagos=Pagos(CobroRechazado(motivo, None)))
    await _registrar_y_cobrar(escenario)
    [(resuelto, _, _)] = escenario.repo.resultados
    assert (resuelto.estado, resuelto.motivo) == (EstadoRecaudo.RECHAZADO, MOTIVO_NO_INFORMADO)
    assert len(escenario.eventos.agregados) == 1
    assert "Juan" not in logs.texto()


@pytest.mark.parametrize(
    "codigo",
    [
        "TIEMPO_AGOTADO",  # 504 del adaptador
        "FUENTE_NO_DISPONIBLE",  # 503
        "RESPUESTA_INVALIDA",  # 502
        "SOLICITUD_INVALIDA",  # 422 inesperado
        "HTTP_500",
        "ADAPTADOR_TIEMPO_AGOTADO",  # sin respuesta a tiempo
        "ADAPTADOR_NO_DISPONIBLE",  # error de conexión
        "ADAPTADOR_RESPUESTA_INVALIDA",  # 200 fuera de contrato
    ],
)
async def test_un_incierto_deja_pendiente_con_su_codigo_y_sin_evento(
    codigo: str, logs: Any
) -> None:
    """Nunca se asume rechazo (ADR 0013, decisión 5); la continuación no lanza."""
    escenario = Escenario(pagos=Pagos(CobroIncierto(codigo)))
    creado = await _registrar_y_cobrar(escenario)

    [(resuelto, tx, dentro)] = escenario.repo.resultados
    assert dentro and tx is escenario.transacciones.abiertas[0]
    assert resuelto.estado is EstadoRecaudo.PENDIENTE
    assert (resuelto.ultimo_incierto, resuelto.actualizado_en) == (codigo, DESPUES)
    assert (resuelto.resuelto_en, resuelto.transaccion_id, resuelto.motivo) == (None, None, None)
    assert escenario.eventos.agregados == []
    [aviso] = logs.de_nivel("WARNING")
    assert (aviso["clave_recaudo"], aviso["codigo"]) == (str(creado.clave_recaudo), codigo)
    assert str(PAGADOR) not in logs.texto()


@pytest.mark.parametrize(
    "resultado", [CobroAprobado("TX-SINT-C01"), CobroRechazado("FONDOS_INSUFICIENTES", None)]
)
async def test_si_la_guarda_no_cambia_ninguna_fila_no_hay_evento(resultado: ResultadoCobro) -> None:
    """Otro proceso (la conciliación) ya lo resolvió: ni evento ni error."""
    escenario = Escenario(repo=Repo(filas_resultado=0), pagos=Pagos(resultado))
    await _registrar_y_cobrar(escenario)
    assert len(escenario.repo.resultados) == 1
    assert escenario.eventos.agregados == []
    assert escenario.transacciones.abiertas[0].confirmada


async def test_si_falla_la_base_tras_el_cobro_la_continuacion_lanza() -> None:
    """El consumidor no borra el mensaje; la reentrega es un duplicado y el movimiento queda
    PENDIENTE para la conciliación. El cobro ocurrió una sola vez."""
    escenario = Escenario(transacciones=Transacciones(falla=ConnectionError("base caída")))
    continuacion = await escenario.caso().registrar(oferta(), Tx("inbox"))
    assert continuacion is not None
    with pytest.raises(ConnectionError):
        await continuacion()
    assert len(escenario.pagos.solicitudes) == 1
    assert escenario.repo.resultados == [] and escenario.eventos.agregados == []


async def test_registrar_resultado_que_falla_tambien_se_propaga() -> None:
    class RepoQueFalla(Repo):
        async def registrar_resultado(self, recaudo: Recaudo, tx: Tx | None = None) -> int:
            raise ConnectionError("base caída")

    escenario = Escenario(repo=RepoQueFalla())
    continuacion = await escenario.caso().registrar(oferta(), Tx("inbox"))
    assert continuacion is not None
    with pytest.raises(ConnectionError):
        await continuacion()
    assert escenario.eventos.agregados == []
    assert escenario.transacciones.abiertas[0].confirmada is False  # rollback


def test_el_caso_de_uso_no_usa_float() -> None:
    import inspect

    from dinero.application.recaudo import recaudar

    assert "float" not in inspect.getsource(recaudar)
