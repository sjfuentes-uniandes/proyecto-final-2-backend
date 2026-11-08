"""Outbox, mapeo, soportes requeridos, reloj y códigos de siniestros (HU-W19)."""

from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from siniestros.adapters.outbound import outbox_pg
from siniestros.adapters.outbound.codigos import PRIMER_CONSECUTIVO, CodigosPg
from siniestros.adapters.outbound.mapeo import (
    copia_a_json,
    copia_de_json,
    ubicacion_a_json,
    ubicacion_de_json,
)
from siniestros.adapters.outbound.reloj import RelojSistema
from siniestros.adapters.outbound.soportes_requeridos import SoportesRequeridosJson
from siniestros.application.siniestro.vistas import (
    AccesoDenegado,
    MotivoDenegacion,
    OrigenDenegacion,
    RecursoSiniestro,
)
from siniestros.domain.siniestro.modelos import (
    CopiaPoliza,
    Evidencia,
    Siniestro,
    TipoSoporte,
    Ubicacion,
)
from siniestros.domain.siniestro.transiciones import asignar
from solventa_common import correlation

pytestmark = pytest.mark.anyio
ANALISTA = UUID("a0a0a0a0-0000-4000-8000-000000000001")


# --- Mapeo -----------------------------------------------------------------------------------------
def test_la_copia_de_la_poliza_ida_y_vuelta(copia: Callable[..., CopiaPoliza]) -> None:
    original = copia()
    json_ = copia_a_json(original)
    assert json_["vigencia"] == {"desde": "2026-05-09", "hasta": "2027-05-09"}
    assert json_["amparos"] == [
        {"codigo": "DANO_ACCIDENTAL", "nombre": "Daño accidental", "sumaAsegurada": "3000000.00"}
    ]
    assert json_["beneficiario"] == {
        "beneficiarioRef": "ben-sint-004",
        "cuentaMascarada": "****1004",
    }
    assert (json_["carenciaDias"], json_["plazoAvisoDias"], json_["primasAlDia"]) == (30, 30, True)
    assert copia_de_json(json_) == original


def test_una_copia_incompleta_conserva_lo_que_falta_como_none() -> None:
    minima = CopiaPoliza(numero="220-2026-000001", ramo="Dispositivos", moneda="COP")
    json_ = copia_a_json(minima)
    assert "vigencia" not in json_
    assert "beneficiario" not in json_
    recuperada = copia_de_json(json_)
    assert recuperada == minima
    assert recuperada.vigencia is None
    assert recuperada.vigencia != date(2026, 1, 1)


def test_la_ubicacion_ida_y_vuelta() -> None:
    ubicacion = Ubicacion("Bogotá", 4.65, -74.05)
    assert ubicacion_de_json(ubicacion_a_json(ubicacion)) == ubicacion
    assert ubicacion_a_json(None) is None
    assert ubicacion_de_json(None) is None
    assert ubicacion_de_json({}) == Ubicacion()


# --- Soportes y reloj ------------------------------------------------------------------------------
def test_los_soportes_requeridos_se_leen_del_archivo_del_servicio() -> None:
    soportes = SoportesRequeridosJson.cargar()
    assert soportes.para("ROBO") == (
        TipoSoporte.DOCUMENTO_IDENTIDAD,
        TipoSoporte.DENUNCIA_AUTORIDAD,
    )
    assert soportes.para("DANO_ACCIDENTAL")[-1] is TipoSoporte.CERTIFICADO_REPARACION
    assert soportes.para("AMPARO_DESCONOCIDO") == ()


def test_el_reloj_del_sistema_da_utc() -> None:
    ahora = RelojSistema().ahora()
    assert ahora.utcoffset() == timedelta(0)
    assert abs(datetime.now(UTC) - ahora) < timedelta(seconds=5)


# --- Outbox ----------------------------------------------------------------------------------------
def test_el_evento_de_asignacion(siniestro: Callable[..., Siniestro], ahora: datetime) -> None:
    caso = asignar(siniestro(), ANALISTA, ahora)
    sobre = outbox_pg.en_evaluacion(caso, ahora)
    assert (sobre.type, sobre.version, sobre.aggregate_id, sobre.occurred_at) == (
        "SiniestroEnEvaluacion",
        1,
        caso.id,
        ahora,
    )
    assert sobre.payload.model_dump(mode="json", by_alias=True) == {
        "siniestroId": str(caso.id),
        "codigo": "88-2026-0442",
        "polizaId": str(caso.poliza_id),
        "analistaId": str(ANALISTA),
        "motivo": "ASIGNACION",
    }


def test_el_evento_toma_la_correlacion_del_contexto_o_crea_una(
    siniestro: Callable[..., Siniestro], ahora: datetime
) -> None:
    token = correlation.establecer("corr-w19-outbox")
    try:
        assert outbox_pg.consultado(siniestro(), "a", ahora).correlation_id == "corr-w19-outbox"
    finally:
        correlation.restablecer(token)
    sin_contexto = outbox_pg.consultado(siniestro(), "a", ahora).correlation_id
    assert UUID(sin_contexto)


def test_la_evidencia_consultada_no_lleva_url_ni_clave(
    evidencia: Callable[..., Evidencia], ahora: datetime
) -> None:
    archivo = evidencia()
    expira = ahora + timedelta(seconds=300)
    sobre = outbox_pg.evidencia_consultada(archivo, "a0a0", expira, ahora)
    assert sobre.aggregate_id == archivo.siniestro_id
    carga = sobre.payload.model_dump(mode="json", by_alias=True)
    assert carga == {
        "siniestroId": str(archivo.siniestro_id),
        "evidenciaId": str(archivo.id),
        "actorId": "a0a0",
        "expiraEn": "2026-11-05T15:05:00Z",
    }


def acceso(recurso_id: str | None, **cambios: Any) -> AccesoDenegado:
    datos: dict[str, Any] = {
        "actor_id": "a0a0",
        "rol": "CLIENTE",
        "recurso": RecursoSiniestro.SINIESTRO,
        "recurso_id": recurso_id,
        "motivo": MotivoDenegacion.ROL_INVALIDO,
        "origen": OrigenDenegacion.SINIESTROS,
    }
    datos.update(cambios)
    return AccesoDenegado(**datos)


def test_el_acceso_denegado_agrega_por_el_recurso_si_es_uuid(ahora: datetime) -> None:
    identificador = uuid4()
    sobre = outbox_pg.acceso_denegado(acceso(str(identificador)), ahora)
    assert sobre.aggregate_id == identificador
    assert sobre.payload.model_dump(mode="json", by_alias=True) == {
        "actorId": "a0a0",
        "rol": "CLIENTE",
        "recurso": "SINIESTRO",
        "recursoId": str(identificador),
        "motivo": "ROL_INVALIDO",
        "origen": "SINIESTROS",
    }


@pytest.mark.parametrize("recurso_id", [None, "", "no-es-uuid"])
def test_sin_uuid_el_acceso_denegado_agrega_por_su_propio_evento(
    ahora: datetime, recurso_id: str | None
) -> None:
    sobre = outbox_pg.acceso_denegado(acceso(recurso_id, recurso=RecursoSiniestro.BANDEJA), ahora)
    assert sobre.aggregate_id == sobre.event_id


# --- Códigos ---------------------------------------------------------------------------------------
class CursorUno:
    def __init__(self, valor: int) -> None:
        self.valor = valor

    async def fetchone(self) -> tuple[int]:
        return (self.valor,)


class ConexionCodigos:
    def __init__(self) -> None:
        self.ultimo: dict[int, int] = {}
        self.sentencias: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, sql: str, parametros: tuple[Any, ...]) -> CursorUno:
        self.sentencias.append((sql, parametros))
        anio, primero = parametros
        self.ultimo[anio] = self.ultimo[anio] + 1 if anio in self.ultimo else primero
        return CursorUno(self.ultimo[anio])


class PoolCodigos:
    def __init__(self) -> None:
        self.conexion = ConexionCodigos()

    @asynccontextmanager
    async def connection(self) -> Any:
        yield self.conexion


async def test_los_codigos_arrancan_en_1000_y_suben_de_uno_por_anio() -> None:
    pool = PoolCodigos()
    codigos = CodigosPg(pool)  # type: ignore[arg-type]
    assert PRIMER_CONSECUTIVO == 1000
    assert [await codigos.siguiente(2026) for _ in range(3)] == [
        "88-2026-1000",
        "88-2026-1001",
        "88-2026-1002",
    ]
    assert await codigos.siguiente(2027) == "88-2027-1000"
    assert "ON CONFLICT (anio) DO UPDATE" in pool.conexion.sentencias[0][0]
