"""Crear los siniestros de las activaciones pendientes (HU-W22, W22-B08)."""

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from pagos_parametricos.application.parametrico.activar import ActivarSiniestros
from pagos_parametricos.domain.parametrico.activacion import (
    ActivacionPendiente,
    Disparador,
    SiniestroCreado,
    SolicitudSiniestro,
)
from pagos_parametricos.domain.parametrico.errores import ActivacionFallida, SiniestrosRechazo
from pagos_parametricos.domain.parametrico.modelos import EventoObservado
from pagos_parametricos.domain.parametrico.ventana import ventana_de_vigencia
from solventa_common.resiliencia import DependenciaNoDisponible, TiempoAgotado

pytestmark = pytest.mark.anyio
EVENTO = UUID("bafc2b26-7c88-5ae3-b5ac-26fdf5427a09")
DESDE, HASTA = ventana_de_vigencia(date(2026, 9, 1), date(2027, 8, 31))
OBSERVADO = EventoObservado(
    EVENTO, "IDEAM-SIM", "IDEAM-1", "EVT-2026-0913", "LLUVIA_ACUMULADA", Decimal(135), "mm",
    "11001", datetime(2026, 11, 10, 14, 0, tzinfo=UTC),
)  # fmt: skip


def _pendiente(i: int, **cambios: Any) -> ActivacionPendiente:
    datos: dict[str, Any] = {
        "evento_id": EVENTO,
        "poliza_id": UUID(int=i),
        "valor_pago": Decimal("1400000.00"),
        "moneda": "COP",
        "version_regla": "1",
        "cliente_id": uuid4(),
        "numero": f"250-2026-{i:06d}",
        "ramo": "Paramétrico",
        "umbral": Decimal("120.000"),
        "vigente_desde": DESDE,
        "vigente_hasta": HASTA,
    } | cambios
    return ActivacionPendiente(**datos)


class RepositorioMemoria:
    def __init__(self, pendientes: list[ActivacionPendiente]) -> None:
        self.por_crear = list(pendientes)
        self.creadas: dict[UUID, tuple[SiniestroCreado, Disparador]] = {}
        self.duplicados = 0
        self.consultas = 0

    async def pendientes(self, evento_id: UUID) -> list[ActivacionPendiente]:
        self.consultas += 1
        return [p for p in self.por_crear if p.poliza_id not in self.creadas]

    async def marcar_creada(
        self, evento_id: UUID, poliza_id: UUID, siniestro: SiniestroCreado, disparador: Disparador
    ) -> None:
        self.creadas[poliza_id] = (siniestro, disparador)

    async def sumar_duplicado(self, evento_id: UUID) -> None:
        self.duplicados += 1


class SiniestrosFalso:
    def __init__(self, fallos: dict[UUID, BaseException] | None = None, pausa_s: float = 0) -> None:
        self.fallos = fallos or {}
        self.pausa_s = pausa_s
        self.solicitudes: list[SolicitudSiniestro] = []
        self.activas = 0
        self.maximo = 0

    async def crear(self, solicitud: SolicitudSiniestro) -> SiniestroCreado:
        self.activas += 1
        self.maximo = max(self.maximo, self.activas)
        try:
            self.solicitudes.append(solicitud)
            await asyncio.sleep(self.pausa_s)
            if solicitud.poliza_id in self.fallos:
                raise self.fallos[solicitud.poliza_id]
            return SiniestroCreado(uuid4(), f"88-2026-{len(self.solicitudes):04d}", True)
        finally:
            self.activas -= 1


async def test_cada_activacion_pendiente_crea_su_siniestro_y_queda_creada() -> None:
    repo = RepositorioMemoria([_pendiente(1), _pendiente(2)])
    siniestros = SiniestrosFalso()

    await ActivarSiniestros(repo, siniestros).ejecutar(OBSERVADO)  # type: ignore[arg-type]

    assert {s.clave for s in siniestros.solicitudes} == {f"{EVENTO}:{UUID(int=i)}" for i in (1, 2)}
    assert set(repo.creadas) == {UUID(int=1), UUID(int=2)}
    siniestro, disparador = repo.creadas[UUID(int=1)]
    assert siniestro.codigo.startswith("88-2026-") and disparador.valor_observado == "135"


async def test_sin_pendientes_no_llama_a_siniestros() -> None:
    siniestros = SiniestrosFalso()

    await ActivarSiniestros(RepositorioMemoria([]), siniestros).ejecutar(OBSERVADO)  # type: ignore[arg-type]

    assert siniestros.solicitudes == []


async def test_el_paralelismo_esta_acotado_por_el_bulkhead() -> None:
    repo = RepositorioMemoria([_pendiente(i) for i in range(1, 26)])
    siniestros = SiniestrosFalso(pausa_s=0.01)

    await ActivarSiniestros(repo, siniestros, paralelismo=10).ejecutar(OBSERVADO)  # type: ignore[arg-type]

    assert siniestros.maximo == 10 and len(repo.creadas) == 25


@pytest.mark.parametrize(
    "error",
    [TiempoAgotado("x"), DependenciaNoDisponible("ERROR", "CERRADO"), SiniestrosRechazo(422, "X")],
)
async def test_si_una_falla_intenta_las_demas_y_lanza_para_no_borrar_el_mensaje(
    error: BaseException,
) -> None:
    repo = RepositorioMemoria([_pendiente(1), _pendiente(2), _pendiente(3)])
    siniestros = SiniestrosFalso({UUID(int=2): error})

    with pytest.raises(ActivacionFallida) as lanzada:
        await ActivarSiniestros(repo, siniestros).ejecutar(OBSERVADO)  # type: ignore[arg-type]

    assert (lanzada.value.fallidas, lanzada.value.total) == (1, 3)
    assert lanzada.value.__cause__ is error
    # Las demás sí quedaron creadas; la fallida sigue pendiente para `reanudar`.
    assert set(repo.creadas) == {UUID(int=1), UUID(int=3)}
    assert [p.poliza_id for p in await repo.pendientes(EVENTO)] == [UUID(int=2)]


async def test_una_cobertura_incompleta_falla_sin_llamar_a_siniestros() -> None:
    repo = RepositorioMemoria([_pendiente(1, cliente_id=None)])
    siniestros = SiniestrosFalso()

    with pytest.raises(ActivacionFallida):
        await ActivarSiniestros(repo, siniestros).ejecutar(OBSERVADO)  # type: ignore[arg-type]

    assert siniestros.solicitudes == [] and not repo.creadas


async def test_la_reanudacion_completa_lo_que_quedo_pendiente() -> None:
    repo = RepositorioMemoria([_pendiente(1), _pendiente(2)])
    siniestros = SiniestrosFalso({UUID(int=2): TiempoAgotado("x")})
    caso = ActivarSiniestros(repo, siniestros)  # type: ignore[arg-type]
    with pytest.raises(ActivacionFallida):
        await caso.ejecutar(OBSERVADO)

    siniestros.fallos.clear()  # siniestros de vuelta
    await caso.ejecutar(OBSERVADO)

    assert set(repo.creadas) == {UUID(int=1), UUID(int=2)}
    # La primera no se volvió a llamar: solo la pendiente.
    assert [s.poliza_id for s in siniestros.solicitudes] == [UUID(int=1), UUID(int=2), UUID(int=2)]


async def test_una_cancelacion_se_propaga_tal_cual() -> None:
    repo = RepositorioMemoria([_pendiente(1)])
    siniestros = SiniestrosFalso({UUID(int=1): asyncio.CancelledError()})

    with pytest.raises(asyncio.CancelledError):
        await ActivarSiniestros(repo, siniestros).ejecutar(OBSERVADO)  # type: ignore[arg-type]


async def test_contar_duplicado_suma_uno_al_evento() -> None:
    repo = RepositorioMemoria([])

    await ActivarSiniestros(repo, SiniestrosFalso()).contar_duplicado(EVENTO)  # type: ignore[arg-type]

    assert repo.duplicados == 1


async def test_la_medicion_distingue_ok_timeout_y_error(telemetria: Any) -> None:
    repo = RepositorioMemoria([_pendiente(1), _pendiente(2), _pendiente(3)])
    siniestros = SiniestrosFalso(
        {UUID(int=2): TiempoAgotado("x"), UUID(int=3): SiniestrosRechazo(422, None)}
    )
    with pytest.raises(ActivacionFallida):
        await ActivarSiniestros(repo, siniestros).ejecutar(OBSERVADO)  # type: ignore[arg-type]

    resultados = sorted(
        s.attributes["result"] for s in telemetria.lista_spans() if s.name == "parametrico.activar"
    )
    assert resultados == ["error", "ok", "timeout"]


async def test_los_registros_llevan_identificadores_y_no_la_medicion(
    telemetria: Any,
) -> None:
    repo = RepositorioMemoria([_pendiente(1), _pendiente(2)])
    siniestros = SiniestrosFalso({UUID(int=2): TiempoAgotado("x")})
    with pytest.raises(ActivacionFallida):
        await ActivarSiniestros(repo, siniestros).ejecutar(OBSERVADO)  # type: ignore[arg-type]

    texto = telemetria.texto_logs()
    assert (
        "parametrico: activacion procesada" in texto
        and "parametrico: activacion pendiente" in texto
    )
    for centinela in ("IDEAM-SIM", "135", "1400000", "Paramétrico", "250-2026"):
        assert centinela not in texto
