"""La continuación de `parametricos-eventos` y su `reanudar` (HU-W22, DS3-15): qué devuelve el
manejador, cuándo se reanuda y qué cobertura llega al siniestro (cliente y ramo de `PolizaEmitida`)."""

import copy
from collections.abc import Callable
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from pagos_parametricos.adapters.inbound.sqs.eventos import manejador_eventos, reanudador_eventos
from pagos_parametricos.adapters.inbound.sqs.polizas import manejador_polizas
from pagos_parametricos.application.parametrico.activar import ActivarSiniestros
from pagos_parametricos.application.parametrico.aplicar_poliza import AplicarEventoPoliza
from pagos_parametricos.application.parametrico.evaluar_evento import EvaluarEvento
from pagos_parametricos.domain.parametrico.activacion import (
    ActivacionPendiente,
    Disparador,
    SiniestroCreado,
    SolicitudSiniestro,
)
from pagos_parametricos.domain.parametrico.orden import PolizaEmitida
from solventa_common.inbox import EventoInvalido

pytestmark = pytest.mark.anyio


class Activaciones:
    """Las activaciones pendientes de un evento, con el repositorio en memoria."""

    def __init__(self, pendientes: int = 0) -> None:
        self.pendientes_: list[ActivacionPendiente] = []
        self.duplicados = 0
        self._cuantas = pendientes

    async def pendientes(self, evento_id: UUID) -> list[ActivacionPendiente]:
        return self.pendientes_

    async def marcar_creada(
        self, evento_id: UUID, poliza_id: UUID, siniestro: SiniestroCreado, disparador: Disparador
    ) -> None:
        self.pendientes_ = [p for p in self.pendientes_ if p.poliza_id != poliza_id]

    async def sumar_duplicado(self, evento_id: UUID) -> None:
        self.duplicados += 1


class Siniestros:
    def __init__(self) -> None:
        self.solicitudes: list[SolicitudSiniestro] = []

    async def crear(self, solicitud: SolicitudSiniestro) -> SiniestroCreado:
        self.solicitudes.append(solicitud)
        return SiniestroCreado(UUID(int=len(self.solicitudes)), "88-2026-1001", True)


@pytest.fixture
def activaciones() -> Activaciones:
    return Activaciones()


@pytest.fixture
def siniestros() -> Siniestros:
    return Siniestros()


@pytest.fixture
def activar(activaciones: Activaciones, siniestros: Siniestros) -> ActivarSiniestros:
    return ActivarSiniestros(activaciones, siniestros)  # type: ignore[arg-type]


@pytest.fixture
async def manejar(
    coberturas: Any, evaluaciones: Any, reloj: Any, activar: ActivarSiniestros
) -> Callable[..., Any]:
    return manejador_eventos(EvaluarEvento(coberturas, evaluaciones, reloj), activar)


@pytest.fixture
async def con_coberturas(coberturas: Any, recibido: Any, tx: Any, datos: dict[str, Any]) -> Any:
    aplicar = manejador_polizas(AplicarEventoPoliza(coberturas))
    for entrada in datos["coberturas"]:
        await aplicar(recibido(copy.deepcopy(entrada["sobre"])), tx)
    return coberturas


async def test_un_evento_que_activa_devuelve_la_continuacion_de_w22(
    manejar: Any, con_coberturas: Any, recibido: Any, observado: Any, tx: Any, casos: Any
) -> None:
    continuacion = await manejar(recibido(observado(casos["valido"])), tx)

    assert continuacion is not None


async def test_un_evento_que_no_activa_no_devuelve_continuacion(
    manejar: Any, con_coberturas: Any, recibido: Any, observado: Any, tx: Any, casos: Any
) -> None:
    assert await manejar(recibido(observado(casos["unaUnidadBajoElUmbral"])), tx) is None


async def test_un_evento_ya_evaluado_devuelve_la_continuacion_por_si_quedaron_pendientes(
    manejar: Any, con_coberturas: Any, recibido: Any, observado: Any, tx: Any, casos: Any
) -> None:
    """El inbox pudo purgarse: el evento ya tiene decisión, pero sus activaciones pueden seguir
    `PENDIENTE`; la continuación consulta y, si no hay, no hace nada."""
    sobre = observado(casos["valido"])
    await manejar(recibido(sobre), tx)

    assert await manejar(recibido(sobre), tx) is not None


async def test_sin_activar_es_el_modo_a_de_w21(
    coberturas: Any,
    evaluaciones: Any,
    reloj: Any,
    con_coberturas: Any,
    recibido: Any,
    observado: Any,
    tx: Any,
    casos: Any,
) -> None:
    manejar = manejador_eventos(EvaluarEvento(coberturas, evaluaciones, reloj))

    assert await manejar(recibido(observado(casos["valido"])), tx) is None


async def test_el_evento_de_otro_tipo_o_invalido_no_devuelve_continuacion(
    manejar: Any, recibido: Any, tx: Any
) -> None:
    sobre = {"eventId": str(UUID(int=7)), "type": "OtroEvento", "version": 1, "payload": {}}
    assert await manejar(recibido(sobre), tx) is None
    with pytest.raises(EventoInvalido):
        await manejar(recibido({**sobre, "type": "EventoParametricoObservado"}), tx)


# --- reanudar -------------------------------------------------------------------------------------
def _pendiente(evento_id: UUID, i: int = 1) -> ActivacionPendiente:
    return ActivacionPendiente(
        evento_id, UUID(int=i), Decimal("1400000.00"), "COP", "1", UUID(int=99), "250-2026-000101",
        "Paramétrico", Decimal("120.000"), None, None,
    )  # fmt: skip


async def test_reanudar_devuelve_la_continuacion_y_suma_un_duplicado_si_quedan_pendientes(
    activar: ActivarSiniestros,
    activaciones: Activaciones,
    recibido: Any,
    observado: Any,
    casos: Any,
) -> None:
    sobre = observado(casos["valido"])
    activaciones.pendientes_ = [_pendiente(UUID(sobre["eventId"]))]
    reanudar = reanudador_eventos(activar)

    continuacion = await reanudar(recibido(sobre))

    assert continuacion is not None and activaciones.duplicados == 1


async def test_reanudar_devuelve_none_si_no_queda_nada_pendiente(
    activar: ActivarSiniestros,
    activaciones: Activaciones,
    recibido: Any,
    observado: Any,
    casos: Any,
) -> None:
    sobre = observado(casos["valido"])

    assert await reanudador_eventos(activar)(recibido(sobre)) is None
    assert activaciones.duplicados == 0


@pytest.mark.parametrize(
    "sobre",
    [
        {"eventId": str(UUID(int=7)), "type": "Otro", "version": 1},
        {
            "eventId": str(UUID(int=8)),
            "type": "EventoParametricoObservado",
            "version": 1,
            "payload": {},
        },
    ],
)
async def test_reanudar_ignora_lo_que_no_es_un_evento_observado_valido(
    activar: ActivarSiniestros, activaciones: Activaciones, recibido: Any, sobre: dict[str, Any]
) -> None:
    activaciones.pendientes_ = [_pendiente(UUID(int=1))]

    assert await reanudador_eventos(activar)(recibido(sobre)) is None
    assert activaciones.duplicados == 0


# --- cliente y ramo de la póliza --------------------------------------------------------------------
async def test_la_emision_guarda_el_cliente_y_el_ramo_de_la_poliza(
    coberturas: Any, recibido: Any, tx: Any, datos: dict[str, Any]
) -> None:
    manejar = manejador_polizas(AplicarEventoPoliza(coberturas))
    sobre = copy.deepcopy(datos["coberturas"][0]["sobre"])

    await manejar(recibido(sobre), tx)

    cobertura = coberturas.filas[UUID(sobre["payload"]["polizaId"])]
    assert cobertura.cliente_id == UUID(sobre["payload"]["clienteId"])
    assert cobertura.ramo == sobre["payload"]["ramo"]["nombre"]


async def test_una_emision_sin_cliente_ni_ramo_se_aplica_igual_con_ellos_nulos(
    coberturas: Any, recibido: Any, tx: Any, datos: dict[str, Any]
) -> None:
    manejar = manejador_polizas(AplicarEventoPoliza(coberturas))
    sobre = copy.deepcopy(datos["coberturas"][0]["sobre"])
    del sobre["payload"]["clienteId"], sobre["payload"]["ramo"]

    await manejar(recibido(sobre), tx)

    cobertura = coberturas.filas[UUID(sobre["payload"]["polizaId"])]
    assert (cobertura.cliente_id, cobertura.ramo) == (None, None) and cobertura.regla is not None


async def test_la_cancelacion_conserva_el_cliente_y_el_ramo(
    coberturas: Any, recibido: Any, tx: Any, datos: dict[str, Any]
) -> None:
    manejar = manejador_polizas(AplicarEventoPoliza(coberturas))
    emitida = copy.deepcopy(datos["coberturas"][0]["sobre"])
    await manejar(recibido(emitida), tx)
    cancelacion = copy.deepcopy(datos["cancelaciones"][0]["sobre"])
    cancelacion["payload"]["polizaId"] = emitida["payload"]["polizaId"]
    cancelacion["occurredAt"] = "2026-12-01T00:00:00Z"

    await manejar(recibido(cancelacion), tx)

    cobertura = coberturas.filas[UUID(emitida["payload"]["polizaId"])]
    assert cobertura.estado.value == "CANCELADA"
    assert cobertura.cliente_id == UUID(emitida["payload"]["clienteId"])
    assert cobertura.ramo == emitida["payload"]["ramo"]["nombre"]
    assert PolizaEmitida.__name__
