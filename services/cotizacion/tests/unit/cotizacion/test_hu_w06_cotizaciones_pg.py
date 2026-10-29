"""CotizacionesPg (HU-W06) con un pool falso: la decisión de fallback y su evento se guardan en la
transacción de la cotización degradada, la PENDIENTE se registra sola y todo vuelve igual."""

import dataclasses
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from cotizacion.adapters.outbound.cotizaciones_pg import (
    COLUMNAS_DECISION,
    CotizacionesPg,
    cotizacion_de,
    decision_de,
    fila_decision,
)
from cotizacion.domain.cotizacion.fallback import CotizacionPendiente, TipoDecision
from cotizacion.domain.perfilamiento.errores import ConflictoIdempotencia
from cotizacion.domain.perfilamiento.modelos import EstadoFuente, Fuente

from ...conftest import perfil_con_fallas
from .test_calcular_prima import PERFIL_C01
from .test_cotizaciones_pg import Conexion, Duplicada, Pool, _decodificada, _leida

pytestmark = pytest.mark.anyio
CENTINELAS = ("6500000", "1990-04-18", "180000000", "82593")


async def _calcular(
    escenario: Any, comando: Any, perfil_c01: Any, modelo: Any, fallas: dict
) -> Any:
    perfil = perfil_con_fallas(perfil_c01, modelo, fallas)
    escenario.perfiles.perfiles[perfil.id] = perfil
    return await escenario.caso().ejecutar(comando(perfil_id=PERFIL_C01))


@pytest.fixture
async def degradada(
    escenario_cotizacion: Any, nueva_cotizacion: Any, perfil_c01: Any, modelo: Any
) -> Any:
    fallas = {Fuente.FA_INGRESOS_AGREGADOS: EstadoFuente.NO_DISPONIBLE}
    cotizacion, _, _ = await _calcular(
        escenario_cotizacion, nueva_cotizacion, perfil_c01, modelo, fallas
    )
    return cotizacion


@pytest.fixture
async def pendiente(
    escenario_cotizacion: Any, nueva_cotizacion: Any, perfil_c01: Any, modelo: Any
) -> Any:
    fallas = {Fuente.FA_HISTORIAL_PAGOS_12M: EstadoFuente.TIEMPO_AGOTADO}
    with pytest.raises(CotizacionPendiente) as error:
        await _calcular(escenario_cotizacion, nueva_cotizacion, perfil_c01, modelo, fallas)
    return error.value.registro


def _con_decision(cotizacion: Any) -> dict[str, Any]:
    columnas = _decodificada(fila_decision(cotizacion.decision))
    return _leida(cotizacion) | {f"decision_{c}": v for c, v in columnas.items()}


async def test_la_decision_vuelve_igual_con_la_cotizacion(degradada: Any) -> None:
    fila = fila_decision(degradada.decision)
    assert set(fila) == set(COLUMNAS_DECISION)
    assert fila["tipo"] == "DEGRADADA" and fila["cotizacion_id"] == degradada.id
    assert fila["faltantes"].obj == [
        {
            "fuente": "FA_INGRESOS_AGREGADOS",
            "dato": "ingreso_mensual_estimado",
            "motivo": "NO_DISPONIBLE",
            "obligatoria": False,
            "regla": {"id": "FB-003", "version": "2026.10.1", "accion": "DEGRADAR"},
        }
    ]
    leida = cotizacion_de(_con_decision(degradada))
    assert leida == degradada and leida.degradada
    # Sin decisión (nominal o anterior a la migración 005), vuelve sin ella.
    assert cotizacion_de(_leida(dataclasses.replace(degradada, decision=None))).decision is None


async def test_guardar_una_degradada_escribe_la_decision_y_el_evento_en_la_transaccion(
    degradada: Any,
) -> None:
    conexion = Conexion(_con_decision(degradada), codigo=degradada.oferta.codigo)
    assert await CotizacionesPg(Pool(conexion)).guardar(degradada) == degradada  # type: ignore[arg-type]
    tablas = [sql.split("(")[0].split()[-1] for sql, _ in conexion.sentencias]
    assert tablas[:3] == ["cotizaciones", "decisiones_fallback", "outbox"]
    assert "INSERT INTO ofertas" in conexion.sentencias[3][0]
    _, (_, agregado, tipo, version, _, correlacion, payload) = conexion.sentencias[2]
    assert (agregado, tipo, version, correlacion) == (
        degradada.id,
        "CotizacionDegradada",
        1,
        "corr-w03",
    )
    assert payload.obj["decision"] == "DEGRADADA"
    assert payload.obj["cotizacionId"] == str(degradada.id)
    assert payload.obj["faltantes"][0]["reglaId"] == "FB-003"
    # Minimización: ni cliente, ni montos, ni datos declarados, ni el valor faltante.
    assert "clienteId" not in payload.obj
    for sensible in CENTINELAS:
        assert sensible not in str(payload.obj)


async def test_registrar_pendiente_guarda_la_decision_y_su_evento(pendiente: Any) -> None:
    conexion = Conexion()
    registro = await CotizacionesPg(Pool(conexion)).registrar_pendiente(pendiente)  # type: ignore[arg-type]
    assert registro == pendiente
    (sql, parametros), (sql_evento, evento) = conexion.sentencias
    assert sql.startswith("INSERT INTO decisiones_fallback") and parametros[1] is None
    assert "INSERT INTO outbox" in sql_evento
    assert evento[1] == pendiente.id  # Sin cotización, el agregado es la decisión.
    assert evento[6].obj["decision"] == "PENDIENTE"
    assert evento[6].obj["accionSugerida"] == "RECALCULAR_PERFIL"
    assert decision_de(_decodificada(fila_decision(pendiente))) == pendiente
    assert pendiente.decision.tipo is TipoDecision.PENDIENTE


async def test_registrar_pendiente_repetida_devuelve_la_guardada(pendiente: Any) -> None:
    guardada = _decodificada(fila_decision(pendiente))
    conexion = Conexion(guardada, error=Duplicada("decisiones_fallback_pendiente_idempotencia"))
    repo = CotizacionesPg(Pool(conexion))  # type: ignore[arg-type]
    otra = dataclasses.replace(pendiente, id=uuid4())
    assert await repo.registrar_pendiente(otra) == pendiente
    [(sql, parametros)] = conexion.cursor_.sentencias
    assert "cotizacion_id IS NULL" in sql
    assert parametros == (pendiente.actor_id, pendiente.idempotency_key)
    with pytest.raises(ConflictoIdempotencia):
        await repo.registrar_pendiente(dataclasses.replace(otra, huella_solicitud="sha256:otra"))


async def test_registrar_pendiente_otra_violacion_se_propaga(pendiente: Any) -> None:
    otra = Conexion(error=Duplicada("decisiones_fallback_pkey"))
    with pytest.raises(psycopg.errors.UniqueViolation):
        await CotizacionesPg(Pool(otra)).registrar_pendiente(pendiente)  # type: ignore[arg-type]
    sin_fila = Conexion(error=Duplicada("decisiones_fallback_pendiente_idempotencia"))
    with pytest.raises(psycopg.errors.UniqueViolation):
        await CotizacionesPg(Pool(sin_fila)).registrar_pendiente(pendiente)  # type: ignore[arg-type]
