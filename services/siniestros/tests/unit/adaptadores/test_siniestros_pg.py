"""SiniestrosPg con conexiones falsas (sin base): lo que consulta y con qué parámetros. La base real
(y la equivalencia con el dominio) la prueban las de módulo."""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Self
from uuid import UUID, uuid4

import psycopg
import pytest
from siniestros.adapters.outbound import siniestros_pg
from siniestros.adapters.outbound.mapeo import copia_a_json, ubicacion_a_json
from siniestros.adapters.outbound.siniestros_pg import (
    SiniestrosPg,
    codificar_cursor,
    decodificar_cursor,
)
from siniestros.application.siniestro.errores import SolicitudInvalida
from siniestros.application.siniestro.puertos import DecisionFinalDuplicada
from siniestros.application.siniestro.vistas import (
    AccesoDenegado,
    DecisionRegistrada,
    FiltroBandeja,
    LiquidacionAprobada,
    MotivoDenegacion,
    OrigenDenegacion,
    Plazos,
    RecursoSiniestro,
    TipoBandeja,
)
from siniestros.domain.siniestro.modelos import (
    CanalSoporte,
    CausalRechazo,
    EntradaHistorial,
    EstadoSiniestro,
    Evidencia,
    OrigenSiniestro,
    PrioridadSiniestro,
    Siniestro,
    SoporteSolicitable,
    TipoEvidencia,
    TipoSoporte,
)

pytestmark = pytest.mark.anyio
ANALISTA = UUID("a0a0a0a0-0000-4000-8000-000000000001")
PLAZOS = Plazos(48, 480, 1440)


class CursorFalso:
    def __init__(self, conexion: "ConexionFalsa", row_factory: Any) -> None:
        self._conexion = conexion
        self.row_factory = row_factory
        self.filas: list[Any] = []
        self.rowcount = 0

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def execute(self, sql: str, parametros: Any = None) -> Self:
        self._conexion.sentencias.append((sql, parametros))
        if self._conexion.falla is not None:
            raise self._conexion.falla
        self.filas = self._conexion.resultados.pop(0) if self._conexion.resultados else []
        self.rowcount = self._conexion.rowcount
        return self

    async def fetchone(self) -> Any:
        return self.filas[0] if self.filas else None

    async def fetchall(self) -> list[Any]:
        return list(self.filas)


class ConexionFalsa:
    def __init__(self, *resultados: list[Any], rowcount: int = 1) -> None:
        self.resultados = list(resultados)
        self.rowcount = rowcount
        self.sentencias: list[tuple[str, Any]] = []
        self.transacciones = 0
        self.falla: Exception | None = None

    def cursor(self, row_factory: Any = None) -> CursorFalso:
        return CursorFalso(self, row_factory)

    async def execute(self, sql: str, parametros: Any = None) -> CursorFalso:
        return await self.cursor().execute(sql, parametros)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        self.transacciones += 1
        yield


class PoolFalso:
    def __init__(self, conexion: ConexionFalsa) -> None:
        self.conexion = conexion
        self.prestadas = 0

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[ConexionFalsa]:
        self.prestadas += 1
        yield self.conexion


def fila_siniestro(s: Siniestro, **extra: Any) -> dict[str, Any]:
    return {
        "id": s.id,
        "codigo": s.codigo,
        "origen": s.origen.value,
        "estado": s.estado.value,
        "poliza_id": s.poliza_id,
        "cliente_id": s.cliente_id,
        "copia_poliza": copia_a_json(s.copia_poliza),
        "causa": s.causa,
        "fecha_evento": s.fecha_evento,
        "ubicacion_declarada": ubicacion_a_json(s.ubicacion_declarada),
        "reportado_en": s.reportado_en,
        "analista_id": s.analista_id,
        "asignado_en": s.asignado_en,
        "pausado_desde": s.pausado_desde,
        "pausa_acumulada_s": s.pausa_acumulada_s,
        "plazo_soporte_vence_en": s.plazo_soporte_vence_en,
        "decidido_en": s.decidido_en,
        "version": s.version,
        "correlation_id": s.correlation_id,
        "valor_reclamado": s.valor_reclamado,
        "moneda": s.moneda,
        "deducible": s.deducible,
        "valor_indemnizar": s.valor_indemnizar,
        **extra,
    }


def fila_evidencia(e: Evidencia) -> dict[str, Any]:
    return {
        "id": e.id,
        "siniestro_id": e.siniestro_id,
        "nombre_archivo": e.nombre_archivo,
        "tipo": e.tipo.value,
        "soporte_tipo": e.soporte_tipo.value if e.soporte_tipo else None,
        "clave_objeto": e.clave_objeto,
        "sha256": e.sha256,
        "capturada_en": e.capturada_en,
        "ubicacion": ubicacion_a_json(e.ubicacion),
        "tamano_bytes": e.tamano_bytes,
        "tipo_contenido": e.tipo_contenido,
    }


def filtro(ahora: datetime, **cambios: Any) -> FiltroBandeja:
    datos: dict[str, Any] = {
        "bandeja": TipoBandeja.MIA,
        "actor_id": ANALISTA,
        "corte": ahora,
        "plazos": PLAZOS,
    }
    datos.update(cambios)
    return FiltroBandeja(**datos)


# --- Cursor ----------------------------------------------------------------------------------------
def test_el_cursor_ida_y_vuelta(ahora: datetime) -> None:
    identificador = uuid4()
    for clave in (-35, 2040, "2026-11-05T01:00:00+00:00"):
        assert decodificar_cursor(codificar_cursor(clave, identificador, ahora)) == (
            clave,
            identificador,
            ahora,
        )


@pytest.mark.parametrize(
    "cursor",
    [
        "",
        "###",
        "bm8tZXMtanNvbg",  # "no-es-json"
        "e30",  # {}
        "eyJrIjogMSwgImkiOiAibm8iLCAidCI6ICJ4In0",  # id y fecha inválidos
    ],
)
def test_un_cursor_malformado_es_solicitud_invalida(cursor: str) -> None:
    with pytest.raises(SolicitudInvalida) as error:
        decodificar_cursor(cursor)
    assert error.value.detalles == {"campos": ["cursor"]}


def test_un_cursor_con_clave_booleana_o_fecha_sin_zona_es_invalido(ahora: datetime) -> None:
    import base64
    import json

    def cursor(**campos: Any) -> str:
        cuerpo = {"k": 1, "i": str(uuid4()), "t": ahora.isoformat(), **campos}
        return base64.urlsafe_b64encode(json.dumps(cuerpo).encode()).decode()

    for malo in (cursor(k=True), cursor(k=[1]), cursor(t="2026-11-05T01:00:00")):
        with pytest.raises(SolicitudInvalida):
            decodificar_cursor(malo)


# --- Bandeja ---------------------------------------------------------------------------------------
async def test_mi_bandeja_ordena_por_tiempo_restante_y_filtra_por_analista(
    siniestro: Callable[..., Siniestro], ahora: datetime
) -> None:
    caso = siniestro()
    conexion = ConexionFalsa([fila_siniestro(caso, clave_orden=2040)], [{"total": 1}])
    pagina = await SiniestrosPg(PoolFalso(conexion)).bandeja(filtro(ahora))  # type: ignore[arg-type]
    consulta, parametros = conexion.sentencias[0]
    assert "analista_id = %(actor)s" in consulta
    assert "ORDER BY floor(extract(epoch" in consulta
    assert "LIMIT %(limite)s" in consulta
    assert parametros["actor"] == ANALISTA
    assert (parametros["limite"], parametros["compromiso_h"], parametros["corte"]) == (
        21,
        48,
        ahora,
    )
    assert pagina.items == (caso,)
    assert (pagina.total, pagina.siguiente_cursor) == (1, None)
    # El total no lleva paginación.
    assert "LIMIT" not in conexion.sentencias[1][0]


async def test_la_cola_general_es_el_mas_antiguo_primero(
    siniestro: Callable[..., Siniestro], ahora: datetime
) -> None:
    caso = siniestro()
    conexion = ConexionFalsa([fila_siniestro(caso, clave_orden=caso.reportado_en)], [{"total": 1}])
    await SiniestrosPg(PoolFalso(conexion)).bandeja(  # type: ignore[arg-type]
        filtro(ahora, bandeja=TipoBandeja.SIN_ASIGNAR)
    )
    consulta, parametros = conexion.sentencias[0]
    assert "estado = 'REPORTADO' AND analista_id IS NULL" in consulta
    assert "ORDER BY reportado_en, id" in consulta
    assert "actor" not in parametros


async def test_hay_otra_pagina_si_llegan_mas_filas_que_el_limite(
    siniestro: Callable[..., Siniestro], ahora: datetime
) -> None:
    casos = [siniestro(codigo=f"88-2026-04{n:02d}") for n in range(3)]
    filas = [fila_siniestro(c, clave_orden=100 + i) for i, c in enumerate(casos)]
    conexion = ConexionFalsa(filas, [{"total": 9}])
    pagina = await SiniestrosPg(PoolFalso(conexion)).bandeja(filtro(ahora, limite=2))  # type: ignore[arg-type]
    assert [s.codigo for s in pagina.items] == ["88-2026-0400", "88-2026-0401"]
    assert pagina.total == 9
    assert pagina.siguiente_cursor is not None
    assert decodificar_cursor(pagina.siguiente_cursor) == (101, casos[1].id, ahora)


async def test_la_segunda_pagina_sigue_despues_del_cursor_con_el_corte_de_la_primera(
    siniestro: Callable[..., Siniestro], ahora: datetime
) -> None:
    corte_inicial = ahora - timedelta(minutes=3)
    ultimo = uuid4()
    cursor = codificar_cursor(101, ultimo, corte_inicial)
    conexion = ConexionFalsa([], [{"total": 0}])
    await SiniestrosPg(PoolFalso(conexion)).bandeja(filtro(ahora, cursor=cursor))  # type: ignore[arg-type]
    consulta, parametros = conexion.sentencias[0]
    assert "(floor(extract(epoch" in consulta
    assert ") > (%(clave)s, %(id)s)" in consulta
    assert (parametros["clave"], parametros["id"], parametros["corte"]) == (
        101,
        ultimo,
        corte_inicial,
    )


async def test_la_cola_general_pagina_por_fecha_de_reporte(
    siniestro: Callable[..., Siniestro], ahora: datetime
) -> None:
    cursor = codificar_cursor("2026-11-05T01:00:00+00:00", uuid4(), ahora)
    conexion = ConexionFalsa([], [{"total": 0}])
    await SiniestrosPg(PoolFalso(conexion)).bandeja(  # type: ignore[arg-type]
        filtro(ahora, bandeja=TipoBandeja.SIN_ASIGNAR, cursor=cursor)
    )
    consulta, parametros = conexion.sentencias[0]
    assert "(reportado_en, id) > (%(clave)s, %(id)s)" in consulta
    assert parametros["clave"] == datetime(2026, 11, 5, 1, 0, tzinfo=UTC)


async def test_un_cursor_invalido_no_llega_a_la_base(ahora: datetime) -> None:
    conexion = ConexionFalsa()
    with pytest.raises(SolicitudInvalida):
        await SiniestrosPg(PoolFalso(conexion)).bandeja(filtro(ahora, cursor="basura"))  # type: ignore[arg-type]
    assert conexion.sentencias == []


@pytest.mark.parametrize(
    ("prioridad", "fragmento"),
    [
        (PrioridadSiniestro.ALTA, "::int < %(alta)s"),
        (PrioridadSiniestro.MEDIA, "::int >= %(alta)s AND"),
        (PrioridadSiniestro.BAJA, "::int >= %(media)s"),
    ],
)
async def test_el_filtro_de_prioridad_usa_los_umbrales(
    ahora: datetime, prioridad: PrioridadSiniestro, fragmento: str
) -> None:
    conexion = ConexionFalsa([], [{"total": 0}])
    await SiniestrosPg(PoolFalso(conexion)).bandeja(filtro(ahora, prioridad=prioridad))  # type: ignore[arg-type]
    consulta, parametros = conexion.sentencias[0]
    assert fragmento in consulta
    assert (parametros["alta"], parametros["media"]) == (480, 1440)


async def test_filtra_por_estado_origen_y_texto_escapando_el_patron(ahora: datetime) -> None:
    conexion = ConexionFalsa([], [{"total": 0}])
    await SiniestrosPg(PoolFalso(conexion)).bandeja(  # type: ignore[arg-type]
        filtro(
            ahora,
            estado=EstadoSiniestro.EN_EVALUACION,
            origen=OrigenSiniestro.PORTAL,
            texto="50%_\\x",
        )
    )
    consulta, parametros = conexion.sentencias[0]
    assert "estado = %(estado)s" in consulta
    assert "origen = %(origen)s" in consulta
    assert "codigo ILIKE %(texto)s OR copia_poliza->>'numero' ILIKE %(texto)s" in consulta
    assert (parametros["estado"], parametros["origen"]) == ("EN_EVALUACION", "PORTAL")
    assert parametros["texto"] == "%50\\%\\_\\\\x%"


async def test_la_conexion_del_pool_conserva_sus_filas_en_tupla(ahora: datetime) -> None:
    # Las lecturas piden filas en diccionario con un cursor propio; no tocan la fábrica de la
    # conexión, que comparte el relay del Outbox (lee tuplas).
    conexion = ConexionFalsa([], [{"total": 0}])
    await SiniestrosPg(PoolFalso(conexion)).bandeja(filtro(ahora))  # type: ignore[arg-type]
    assert not hasattr(conexion, "row_factory")


# --- Indicadores -----------------------------------------------------------------------------------
async def test_indicadores(ahora: datetime) -> None:
    fila = {
        "pendientes": 5,
        "vencen_hoy": 1,
        "fuera_de_compromiso": 1,
        "resueltos_semana": 4,
        "resueltos_previa": 2,
        "tiempo_medio_h": 21.456,
        "asignados": 6,
        "sin_asignar": 3,
    }
    conexion = ConexionFalsa([fila])
    desde, hasta = ahora, ahora + timedelta(days=1)
    resultado = await SiniestrosPg(PoolFalso(conexion)).indicadores(
        ANALISTA, desde, hasta, ahora, 48
    )  # type: ignore[arg-type]
    assert (resultado.pendientes, resultado.vencen_hoy, resultado.fuera_de_compromiso) == (5, 1, 1)
    assert (resultado.resueltos_semana, resultado.resueltos_semana_previa) == (4, 2)
    assert resultado.tiempo_medio_decision_horas == 21.46
    assert (resultado.asignados_al_actor, resultado.sin_asignar, resultado.compromiso_horas) == (
        6,
        3,
        48,
    )
    assert conexion.sentencias[0][1] == {
        "actor": ANALISTA,
        "desde": desde,
        "hasta": hasta,
        "corte": ahora,
        "compromiso_h": 48,
    }


async def test_sin_resueltos_el_tiempo_medio_es_nulo(ahora: datetime) -> None:
    fila = dict.fromkeys(
        (
            "pendientes",
            "vencen_hoy",
            "fuera_de_compromiso",
            "resueltos_semana",
            "resueltos_previa",
            "asignados",
            "sin_asignar",
        ),
        0,
    )
    conexion = ConexionFalsa([{**fila, "tiempo_medio_h": None}])
    resultado = await SiniestrosPg(PoolFalso(conexion)).indicadores(
        ANALISTA, ahora, ahora, ahora, 48
    )  # type: ignore[arg-type]
    assert resultado.tiempo_medio_decision_horas is None


# --- Detalle y evidencia ---------------------------------------------------------------------------
async def test_detalle_inexistente_es_none() -> None:
    conexion = ConexionFalsa([])
    assert await SiniestrosPg(PoolFalso(conexion)).detalle(uuid4()) is None  # type: ignore[arg-type]
    assert len(conexion.sentencias) == 1


async def test_detalle_lee_caso_evidencias_e_historial_en_una_transaccion(
    siniestro: Callable[..., Siniestro], evidencia: Callable[..., Evidencia], ahora: datetime
) -> None:
    caso = siniestro()
    archivo = evidencia(siniestro_id=caso.id)
    historial = {
        "id": uuid4(),
        "siniestro_id": caso.id,
        "accion": "ASIGNADO",
        "estado_anterior": "REPORTADO",
        "estado_nuevo": "EN_EVALUACION",
        "actor_id": str(ANALISTA),
        "actor_rol": "ANALISTA_SINIESTROS",
        "actor_nombre": "Luis Mora",
        "justificacion": None,
        "detalles": None,
        "ocurrido_en": ahora,
    }
    sin_estados = {
        **historial,
        "id": uuid4(),
        "accion": "REPORTADO",
        "estado_anterior": None,
        "estado_nuevo": None,
    }
    conexion = ConexionFalsa(
        [fila_siniestro(caso)], [fila_evidencia(archivo)], [sin_estados, historial]
    )
    pool = PoolFalso(conexion)
    leido = await SiniestrosPg(pool).detalle(caso.id)  # type: ignore[arg-type]
    assert leido is not None
    assert leido.siniestro == caso
    assert leido.evidencias == (archivo,)
    assert [h.accion for h in leido.historial] == ["REPORTADO", "ASIGNADO"]
    assert leido.historial[1].estado_nuevo is EstadoSiniestro.EN_EVALUACION
    assert leido.historial[0].estado_nuevo is None
    assert (pool.prestadas, conexion.transacciones) == (1, 1)
    assert "ORDER BY capturada_en NULLS LAST" in conexion.sentencias[1][0]


async def test_evidencia_distingue_siniestro_inexistente_de_evidencia_inexistente(
    evidencia: Callable[..., Evidencia],
) -> None:
    sin_siniestro = ConexionFalsa([])
    assert await SiniestrosPg(PoolFalso(sin_siniestro)).evidencia(uuid4(), uuid4()) == (False, None)  # type: ignore[arg-type]
    sin_evidencia = ConexionFalsa([{"existe": 1}], [])
    assert await SiniestrosPg(PoolFalso(sin_evidencia)).evidencia(uuid4(), uuid4()) == (True, None)  # type: ignore[arg-type]
    archivo = evidencia(tipo=TipoEvidencia.SOPORTE, soporte_tipo=TipoSoporte.PERITAJE)
    con = ConexionFalsa([{"existe": 1}], [fila_evidencia(archivo)])
    assert await SiniestrosPg(PoolFalso(con)).evidencia(archivo.siniestro_id, archivo.id) == (
        True,
        archivo,
    )  # type: ignore[arg-type]
    assert "siniestro_id = %s" in con.sentencias[1][0]


# --- Transacción -----------------------------------------------------------------------------------
async def test_la_transaccion_abre_una_conexion_y_una_transaccion() -> None:
    conexion = ConexionFalsa()
    pool = PoolFalso(conexion)
    async with SiniestrosPg(pool).transaccion() as tx:  # type: ignore[arg-type]
        assert tx is not None
    assert (pool.prestadas, conexion.transacciones) == (1, 1)


async def test_idempotencia_serializa_por_clave_y_devuelve_lo_guardado() -> None:
    conexion = ConexionFalsa([], [])
    async with SiniestrosPg(PoolFalso(conexion)).transaccion() as tx:  # type: ignore[arg-type]
        assert await tx.adquirir_idempotencia("siguiente:k") is None  # type: ignore[attr-defined]
    assert "pg_advisory_xact_lock(hashtext(%s))" in conexion.sentencias[0][0]
    conexion = ConexionFalsa([], [{"huella_solicitud": "h", "respuesta": {"siniestroId": "x"}}])
    async with SiniestrosPg(PoolFalso(conexion)).transaccion() as tx:  # type: ignore[arg-type]
        previo = await tx.adquirir_idempotencia("siguiente:k")  # type: ignore[attr-defined]
    assert previo is not None
    assert (previo.huella, previo.respuesta) == ("h", {"siniestroId": "x"})


async def test_reserva_bloqueando_sin_esperar_y_guarda_la_asignacion(
    siniestro: Callable[..., Siniestro], ahora: datetime
) -> None:
    from siniestros.domain.siniestro.transiciones import asignar

    libre = siniestro()
    conexion = ConexionFalsa([], [fila_siniestro(libre)])
    async with SiniestrosPg(PoolFalso(conexion)).transaccion() as tx:  # type: ignore[arg-type]
        assert await tx.reservar_siguiente() is None  # type: ignore[attr-defined]
        reservado = await tx.reservar_siguiente()  # type: ignore[attr-defined]
        assert reservado == libre
        asignado = asignar(libre, ANALISTA, ahora)
        await tx.guardar_asignacion(asignado, libre.version)  # type: ignore[attr-defined]
    consulta = conexion.sentencias[0][0]
    assert "FOR UPDATE SKIP LOCKED" in consulta
    assert "ORDER BY reportado_en, id" in consulta
    actualizacion, parametros = conexion.sentencias[2]
    assert actualizacion.startswith("UPDATE siniestros SET")
    assert "WHERE id = %s AND version = %s" in actualizacion
    assert parametros[0] == "EN_EVALUACION"
    assert parametros[-1] == libre.version


async def test_una_asignacion_sobre_una_version_vieja_falla(
    siniestro: Callable[..., Siniestro], ahora: datetime
) -> None:
    from siniestros.domain.siniestro.transiciones import asignar

    libre = siniestro()
    conexion = ConexionFalsa(rowcount=0)
    with pytest.raises(RuntimeError, match="versión"):
        async with SiniestrosPg(PoolFalso(conexion)).transaccion() as tx:  # type: ignore[arg-type]
            await tx.guardar_asignacion(asignar(libre, ANALISTA, ahora), libre.version)  # type: ignore[attr-defined]


async def test_siniestro_por_id_idempotencia_e_historial(
    siniestro: Callable[..., Siniestro], ahora: datetime
) -> None:
    caso = siniestro()
    conexion = ConexionFalsa([], [fila_siniestro(caso)])
    entrada = EntradaHistorial(
        uuid4(),
        caso.id,
        "ASIGNADO",
        ahora,
        EstadoSiniestro.REPORTADO,
        EstadoSiniestro.EN_EVALUACION,
        "a",
        "ANALISTA_SINIESTROS",
        "Luis Mora",
    )
    async with SiniestrosPg(PoolFalso(conexion)).transaccion() as tx:  # type: ignore[arg-type]
        assert await tx.siniestro(caso.id) is None  # type: ignore[attr-defined]
        assert await tx.siniestro(caso.id) == caso  # type: ignore[attr-defined]
        await tx.guardar_idempotencia("k", "h", {"siniestroId": "x"}, ahora)  # type: ignore[attr-defined]
        await tx.agregar_historial(entrada)  # type: ignore[attr-defined]
        await tx.agregar_historial(EntradaHistorial(uuid4(), caso.id, "REPORTADO", ahora))  # type: ignore[attr-defined]
    insercion_idem, parametros = conexion.sentencias[2]
    assert insercion_idem.startswith("INSERT INTO idempotencia")
    assert parametros[0] == "k"
    historial = conexion.sentencias[3][1]
    assert (historial[2], historial[3], historial[4], historial[7]) == (
        "ASIGNADO",
        "REPORTADO",
        "EN_EVALUACION",
        "Luis Mora",
    )
    assert conexion.sentencias[4][1][3:5] == (None, None)


async def test_los_cuatro_eventos_se_insertan_en_el_outbox_de_la_misma_conexion(
    siniestro: Callable[..., Siniestro], evidencia: Callable[..., Evidencia], ahora: datetime
) -> None:
    from siniestros.domain.siniestro.transiciones import asignar

    caso = asignar(siniestro(), ANALISTA, ahora)
    archivo = evidencia(siniestro_id=caso.id)
    conexion = ConexionFalsa()
    async with SiniestrosPg(PoolFalso(conexion)).transaccion() as tx:  # type: ignore[arg-type]
        await tx.evento_en_evaluacion(caso, ahora)  # type: ignore[attr-defined]
        await tx.evento_consultado(caso, str(ANALISTA), ahora)  # type: ignore[attr-defined]
        await tx.evento_evidencia_consultada(
            archivo, str(ANALISTA), ahora + timedelta(minutes=5), ahora
        )  # type: ignore[attr-defined]
        await tx.evento_acceso_denegado(  # type: ignore[attr-defined]
            AccesoDenegado(
                "a",
                None,
                RecursoSiniestro.BANDEJA,
                None,
                MotivoDenegacion.SIN_GRUPO,
                OrigenDenegacion.BFF_WEB,
            ),
            ahora,
        )
    assert all(s.startswith("INSERT INTO outbox") for s, _ in conexion.sentencias)
    assert [p[2] for _, p in conexion.sentencias] == [
        "SiniestroEnEvaluacion",
        "SiniestroConsultado",
        "EvidenciaConsultada",
        "AccesoSiniestroDenegado",
    ]
    cargas = repr([p[6].obj for _, p in conexion.sentencias])
    assert archivo.clave_objeto not in cargas
    assert "http" not in cargas
    assert "Luis" not in cargas


def test_el_mapeo_de_filas_no_pierde_nada(siniestro: Callable[..., Siniestro]) -> None:
    caso = siniestro()
    assert siniestros_pg.siniestro_de(fila_siniestro(caso)) == caso


def test_el_mapeo_de_un_caso_aprobado_conserva_la_liquidacion(
    siniestro: Callable[..., Siniestro], ahora: datetime
) -> None:
    caso = siniestro(
        estado=EstadoSiniestro.APROBADO,
        decidido_en=ahora,
        valor_reclamado=Decimal("2850000.00"),
        moneda="COP",
        deducible=Decimal("350000.00"),
        valor_indemnizar=Decimal("2500000.00"),
    )
    assert siniestros_pg.siniestro_de(fila_siniestro(caso)) == caso


# --- HU-W20: decisión, plazos y liquidación ---------------------------------------------------------


class UnicaFalsa(psycopg.errors.UniqueViolation):
    """Una violación de unicidad que nombra su restricción, como la entrega PostgreSQL."""

    def __init__(self, restriccion: str) -> None:
        super().__init__("duplicada")
        self._restriccion = restriccion

    @property
    def diag(self) -> Any:  # type: ignore[override]
        return SimpleNamespace(constraint_name=self._restriccion)


def en_evaluacion(siniestro: Callable[..., Siniestro], ahora: datetime) -> Siniestro:
    return siniestro(estado=EstadoSiniestro.EN_EVALUACION, analista_id=ANALISTA, asignado_en=ahora)


async def test_bloquear_el_siniestro_usa_for_update_y_distingue_el_inexistente(
    siniestro: Callable[..., Siniestro], ahora: datetime
) -> None:
    caso = en_evaluacion(siniestro, ahora)
    conexion = ConexionFalsa([], [fila_siniestro(caso)])
    async with SiniestrosPg(PoolFalso(conexion)).transaccion() as tx:  # type: ignore[arg-type]
        assert await tx.bloquear_siniestro(caso.id) is None  # type: ignore[attr-defined]
        assert await tx.bloquear_siniestro(caso.id) == caso  # type: ignore[attr-defined]
    assert conexion.sentencias[0][0].rstrip().endswith("WHERE id = %s FOR UPDATE")


async def test_los_ids_de_las_evidencias_del_caso() -> None:
    uno, dos = uuid4(), uuid4()
    conexion = ConexionFalsa([{"id": uno}, {"id": dos}])
    async with SiniestrosPg(PoolFalso(conexion)).transaccion() as tx:  # type: ignore[arg-type]
        assert await tx.ids_evidencias(uuid4()) == frozenset({uno, dos})  # type: ignore[attr-defined]


async def test_los_plazos_vencidos_se_reservan_con_skip_locked_en_orden(
    siniestro: Callable[..., Siniestro], ahora: datetime
) -> None:
    vencido = siniestro(
        estado=EstadoSiniestro.FALTA_SOPORTE,
        analista_id=ANALISTA,
        asignado_en=ahora,
        pausado_desde=ahora,
        plazo_soporte_vence_en=ahora - timedelta(hours=1),
    )
    conexion = ConexionFalsa([fila_siniestro(vencido)])
    async with SiniestrosPg(PoolFalso(conexion)).transaccion() as tx:  # type: ignore[arg-type]
        assert await tx.reservar_plazos_vencidos(ahora, 25) == [vencido]  # type: ignore[attr-defined]
    consulta, parametros = conexion.sentencias[0]
    assert "estado = 'FALTA_SOPORTE' AND plazo_soporte_vence_en < %s" in consulta
    assert "ORDER BY plazo_soporte_vence_en, id FOR UPDATE SKIP LOCKED LIMIT %s" in consulta
    assert parametros == (ahora, 25)


async def test_guardar_la_transicion_escribe_el_reloj_el_plazo_y_la_liquidacion(
    siniestro: Callable[..., Siniestro], ahora: datetime
) -> None:
    caso = en_evaluacion(siniestro, ahora)
    aprobado = siniestro(
        estado=EstadoSiniestro.APROBADO,
        analista_id=ANALISTA,
        asignado_en=ahora,
        decidido_en=ahora,
        deducible=Decimal("350000.00"),
        valor_indemnizar=Decimal("2500000.00"),
        version=2,
    )
    conexion = ConexionFalsa()
    async with SiniestrosPg(PoolFalso(conexion)).transaccion() as tx:  # type: ignore[arg-type]
        await tx.guardar_transicion(aprobado, caso.version)  # type: ignore[attr-defined]
    consulta, parametros = conexion.sentencias[0]
    assert consulta.startswith("UPDATE siniestros SET estado = %s, pausado_desde = %s")
    assert "WHERE id = %s AND version = %s" in consulta
    assert parametros == (
        "APROBADO",
        None,
        0,
        None,
        ahora,
        Decimal("350000.00"),
        Decimal("2500000.00"),
        2,
        aprobado.id,
        caso.version,
    )


async def test_una_transicion_sobre_una_version_vieja_falla(
    siniestro: Callable[..., Siniestro], ahora: datetime
) -> None:
    caso = en_evaluacion(siniestro, ahora)
    with pytest.raises(RuntimeError, match="versión"):
        async with SiniestrosPg(PoolFalso(ConexionFalsa(rowcount=0))).transaccion() as tx:  # type: ignore[arg-type]
            await tx.guardar_transicion(caso, caso.version)  # type: ignore[attr-defined]


def decision(siniestro_id: UUID, ahora: datetime, tipo: str = "APROBAR") -> DecisionRegistrada:
    return DecisionRegistrada(
        id=uuid4(),
        siniestro_id=siniestro_id,
        tipo=tipo,
        idempotency_key=uuid4(),
        huella="h" * 64,
        detalle={"explicacionCliente": "texto"},
        actor_id=str(ANALISTA),
        registrada_en=ahora,
    )


async def test_la_decision_se_inserta_con_su_detalle_sensible(ahora: datetime) -> None:
    registrada = decision(uuid4(), ahora)
    conexion = ConexionFalsa()
    async with SiniestrosPg(PoolFalso(conexion)).transaccion() as tx:  # type: ignore[arg-type]
        await tx.registrar_decision(registrada)  # type: ignore[attr-defined]
    consulta, parametros = conexion.sentencias[0]
    assert consulta.startswith("INSERT INTO decisiones_siniestro")
    assert parametros[:5] == (
        registrada.id,
        registrada.siniestro_id,
        "APROBAR",
        registrada.idempotency_key,
        "h" * 64,
    )
    assert parametros[5].obj == {"explicacionCliente": "texto"}


async def test_una_segunda_decision_final_se_traduce_a_decision_duplicada(
    ahora: datetime,
) -> None:
    conexion = ConexionFalsa()
    conexion.falla = UnicaFalsa("decisiones_una_final")
    with pytest.raises(DecisionFinalDuplicada):
        async with SiniestrosPg(PoolFalso(conexion)).transaccion() as tx:  # type: ignore[arg-type]
            await tx.registrar_decision(decision(uuid4(), ahora))  # type: ignore[attr-defined]


async def test_otra_violacion_de_unicidad_no_se_disfraza(ahora: datetime) -> None:
    conexion = ConexionFalsa()
    conexion.falla = UnicaFalsa("decisiones_siniestro_idempotency_key_key")
    with pytest.raises(psycopg.errors.UniqueViolation):
        async with SiniestrosPg(PoolFalso(conexion)).transaccion() as tx:  # type: ignore[arg-type]
            await tx.registrar_decision(decision(uuid4(), ahora))  # type: ignore[attr-defined]


async def test_el_historial_guarda_las_evidencias_y_los_detalles_y_solo_cuando_hay(
    siniestro: Callable[..., Siniestro], ahora: datetime
) -> None:
    caso = en_evaluacion(siniestro, ahora)
    evidencia_id = uuid4()
    con = EntradaHistorial(
        uuid4(),
        caso.id,
        "APROBADO",
        ahora,
        EstadoSiniestro.EN_EVALUACION,
        EstadoSiniestro.APROBADO,
        "a",
        "ANALISTA_SINIESTROS",
        "Luis Mora",
        "Daño verificado",
        (evidencia_id,),
        {"referenciaPago": "IND-88-2026-0442-01"},
    )
    sin = EntradaHistorial(uuid4(), caso.id, "REPORTADO", ahora)
    conexion = ConexionFalsa()
    async with SiniestrosPg(PoolFalso(conexion)).transaccion() as tx:  # type: ignore[arg-type]
        await tx.agregar_historial(con)  # type: ignore[attr-defined]
        await tx.agregar_historial(sin)  # type: ignore[attr-defined]
    escrito = conexion.sentencias[0][1]
    assert escrito[9].obj == [str(evidencia_id)]
    assert escrito[10].obj == {"referenciaPago": "IND-88-2026-0442-01"}
    assert conexion.sentencias[1][1][9:11] == (None, None)


async def test_los_eventos_de_la_decision_van_al_outbox_sin_los_textos_al_cliente(
    siniestro: Callable[..., Siniestro], ahora: datetime
) -> None:
    caso = en_evaluacion(siniestro, ahora)
    aprobado = siniestro(
        estado=EstadoSiniestro.APROBADO,
        analista_id=ANALISTA,
        asignado_en=ahora,
        decidido_en=ahora,
        valor_indemnizar=Decimal("2500000.00"),
    )
    pedido = siniestro(
        estado=EstadoSiniestro.FALTA_SOPORTE,
        analista_id=ANALISTA,
        asignado_en=ahora,
        pausado_desde=ahora,
        plazo_soporte_vence_en=ahora + timedelta(days=14),
    )
    conexion = ConexionFalsa()
    async with SiniestrosPg(PoolFalso(conexion)).transaccion() as tx:  # type: ignore[arg-type]
        await tx.evento_aprobado(aprobado, "IND-88-2026-0442-01", ANALISTA, ahora)  # type: ignore[attr-defined]
        await tx.evento_rechazado(caso, CausalRechazo.PRIMAS_EN_MORA, ANALISTA, ahora)  # type: ignore[attr-defined]
        await tx.evento_soporte_solicitado(  # type: ignore[attr-defined]
            pedido,
            (SoporteSolicitable.PERITAJE,),
            CanalSoporte.CORREO,
            ANALISTA,
            ahora,
        )
        await tx.evento_en_evaluacion(caso, ahora, "PLAZO_SOPORTE_VENCIDO")  # type: ignore[attr-defined]
    assert [p[2] for _, p in conexion.sentencias] == [
        "SiniestroAprobado",
        "SiniestroRechazado",
        "SiniestroSoporteSolicitado",
        "SiniestroEnEvaluacion",
    ]
    cargas = [p[6].obj for _, p in conexion.sentencias]
    assert cargas[0]["valorIndemnizar"] == "2500000.00"
    assert cargas[0]["beneficiarioRef"] == "ben-sint-004"
    assert cargas[0]["decididoPor"] == str(ANALISTA)
    assert cargas[1]["causal"] == "PRIMAS_EN_MORA"
    assert cargas[2]["documentos"] == ["PERITAJE"] and cargas[2]["canal"] == "CORREO"
    assert cargas[3]["motivo"] == "PLAZO_SOPORTE_VENCIDO"
    assert "****1004" not in repr(cargas)  # nunca la cuenta, ni siquiera enmascarada


async def test_el_detalle_de_un_caso_aprobado_lee_la_decision_para_la_liquidacion(
    siniestro: Callable[..., Siniestro], ahora: datetime
) -> None:
    aprobado = siniestro(
        estado=EstadoSiniestro.APROBADO,
        analista_id=ANALISTA,
        asignado_en=ahora,
        decidido_en=ahora,
        deducible=Decimal("350000.00"),
        valor_indemnizar=Decimal("2500000.00"),
    )
    decision_fila = {
        "detalle": {
            "liquidacion": {
                "valorReclamado": "2850000.00",
                "deducible": "350000.00",
                "valorIndemnizar": "2500000.00",
                "moneda": "COP",
            }
        },
        "registrada_en": ahora + timedelta(minutes=1),
    }
    sin_pago = {
        "pago_estado": None,
        "pago_referencia": None,
        "pago_transaccion_id": None,
        "pago_motivo": None,
        "pago_intentos": 0,
        "pago_duplicados_descartados": 0,
    }
    conexion = ConexionFalsa([fila_siniestro(aprobado)], [], [], [decision_fila], [sin_pago])
    leido = await SiniestrosPg(PoolFalso(conexion)).detalle(aprobado.id)  # type: ignore[arg-type]
    assert leido is not None
    assert leido.pago is None  # `dinero` aún no informó: el detalle deriva PENDIENTE
    assert "pago_estado" in conexion.sentencias[4][0]
    assert leido.liquidacion == LiquidacionAprobada(
        "COP",
        Decimal("2850000.00"),
        Decimal("350000.00"),
        Decimal("2500000.00"),
        "IND-88-2026-0442-01",
        ahora + timedelta(minutes=1),
    )
    assert "decisiones_siniestro" in conexion.sentencias[3][0]


def test_la_liquidacion_de_un_paramétrico_no_tiene_decision(
    siniestro: Callable[..., Siniestro], ahora: datetime
) -> None:
    parametrico = siniestro(
        origen=OrigenSiniestro.PARAMETRICO,
        estado=EstadoSiniestro.APROBADO,
        decidido_en=ahora,
        valor_indemnizar=Decimal("500000.00"),
        valor_reclamado=Decimal("500000.00"),
        moneda="COP",
    )
    liquidacion = siniestros_pg.liquidacion_de(parametrico, None)
    assert liquidacion is not None
    assert (liquidacion.valor_reclamado, liquidacion.deducible) == (Decimal("500000.00"), None)
    assert (liquidacion.aprobada_en, liquidacion.moneda) == (ahora, "COP")


def test_un_caso_no_aprobado_no_tiene_liquidacion(
    siniestro: Callable[..., Siniestro], ahora: datetime
) -> None:
    assert siniestros_pg.liquidacion_de(en_evaluacion(siniestro, ahora), None) is None


def test_el_mapeo_del_historial_lee_los_detalles(ahora: datetime) -> None:
    fila = {
        "id": uuid4(),
        "siniestro_id": uuid4(),
        "accion": "RECHAZADO",
        "estado_anterior": "EN_EVALUACION",
        "estado_nuevo": "RECHAZADO",
        "actor_id": "a",
        "actor_rol": "ANALISTA_SINIESTROS",
        "actor_nombre": "Luis",
        "justificacion": "x",
        "detalles": {"causal": "PRIMAS_EN_MORA"},
        "ocurrido_en": ahora,
    }
    assert siniestros_pg.entrada_de(fila).detalles == {"causal": "PRIMAS_EN_MORA"}


def test_una_decision_sin_liquidacion_en_el_detalle_usa_las_columnas_del_caso(
    siniestro: Callable[..., Siniestro], ahora: datetime
) -> None:
    aprobado = siniestro(
        estado=EstadoSiniestro.APROBADO,
        decidido_en=ahora,
        valor_reclamado=Decimal("900000.00"),
        valor_indemnizar=Decimal("800000.00"),
    )
    decision_fila = {"detalle": {}, "registrada_en": ahora + timedelta(minutes=2)}
    liquidacion = siniestros_pg.liquidacion_de(aprobado, decision_fila)
    assert liquidacion is not None
    assert liquidacion.valor_reclamado == Decimal("900000.00")
    assert liquidacion.moneda == "COP"
    assert liquidacion.aprobada_en == ahora + timedelta(minutes=2)


# --- HU-W23: el pago que informa `dinero` ---------------------------------------------------------
def _fila_pago(**cambios: Any) -> dict[str, Any]:
    fila: dict[str, Any] = {
        "pago_estado": "CONFIRMADO",
        "pago_referencia": "IND-88-2026-0442-01",
        "pago_transaccion_id": "TRX-1",
        "pago_motivo": None,
        "pago_intentos": 1,
        "pago_duplicados_descartados": 2,
    }
    fila.update(cambios)
    return fila


def test_la_fila_de_pago_se_lee_o_es_nula() -> None:
    from siniestros.adapters.outbound.siniestros_pg import pago_de_fila
    from siniestros.domain.siniestro.pago import EstadoPago, PagoSiniestro

    assert pago_de_fila(_fila_pago()) == PagoSiniestro(
        EstadoPago.CONFIRMADO, "IND-88-2026-0442-01", "TRX-1", None, 1, 2
    )
    rechazado = pago_de_fila(
        _fila_pago(pago_estado="RECHAZADO", pago_transaccion_id=None, pago_motivo="MOTIVO")
    )
    assert rechazado is not None and (rechazado.estado, rechazado.motivo) == (
        EstadoPago.RECHAZADO,
        "MOTIVO",
    )
    assert pago_de_fila(_fila_pago(pago_estado=None)) is None


async def test_el_detalle_de_un_caso_aprobado_lee_el_pago_que_informo_dinero(
    siniestro: Callable[..., Siniestro], ahora: datetime
) -> None:
    aprobado = siniestro(
        estado=EstadoSiniestro.APROBADO,
        analista_id=ANALISTA,
        asignado_en=ahora,
        decidido_en=ahora,
        valor_indemnizar=Decimal("2500000.00"),
    )
    conexion = ConexionFalsa([fila_siniestro(aprobado)], [], [], [], [_fila_pago()])
    leido = await SiniestrosPg(PoolFalso(conexion)).detalle(aprobado.id)  # type: ignore[arg-type]
    assert leido is not None and leido.pago is not None
    assert (leido.pago.estado.value, leido.pago.transaccion_id) == ("CONFIRMADO", "TRX-1")
    assert (
        "pago_estado" in conexion.sentencias[4][0]
        and "FROM siniestros" in conexion.sentencias[4][0]
    )


async def test_un_caso_no_aprobado_no_lee_el_pago(siniestro: Callable[..., Siniestro]) -> None:
    caso = siniestro()
    conexion = ConexionFalsa([fila_siniestro(caso)], [], [])
    leido = await SiniestrosPg(PoolFalso(conexion)).detalle(caso.id)  # type: ignore[arg-type]
    assert leido is not None and leido.pago is None
    assert len(conexion.sentencias) == 3  # siniestro, evidencias e historial: nada del pago


async def test_estado_pago_lee_la_columna_o_none() -> None:
    from siniestros.domain.siniestro.pago import EstadoPago

    for fila, esperado in (
        ({"pago_estado": "RECHAZADO"}, EstadoPago.RECHAZADO),
        ({"pago_estado": None}, None),
    ):
        conexion = ConexionFalsa([fila])
        async with SiniestrosPg(PoolFalso(conexion)).transaccion() as tx:  # type: ignore[arg-type]
            assert await tx.estado_pago(uuid4()) == esperado
    conexion = ConexionFalsa([])
    async with SiniestrosPg(PoolFalso(conexion)).transaccion() as tx:  # type: ignore[arg-type]
        assert await tx.estado_pago(uuid4()) is None


@pytest.mark.parametrize("filas", [1, 0])
async def test_guardar_pago_solo_escribe_si_no_habia_resultado(filas: int) -> None:
    from siniestros.domain.siniestro.pago import EstadoPago, ResultadoPago

    resultado = ResultadoPago(
        uuid4(),
        EstadoPago.CONFIRMADO,
        "IND-88-2026-0442-01",
        "TRX-1",
        None,
        1,
        2,
        datetime.now(UTC),
    )
    conexion = ConexionFalsa(rowcount=filas)
    async with SiniestrosPg(PoolFalso(conexion)).transaccion() as tx:  # type: ignore[arg-type]
        if filas:
            await tx.guardar_pago(resultado)
        else:
            with pytest.raises(RuntimeError, match="guardar_pago"):
                await tx.guardar_pago(resultado)
    [(sql, parametros)] = conexion.sentencias
    assert sql.startswith("UPDATE siniestros SET pago_estado = %s")
    assert sql.endswith("WHERE id = %s AND pago_estado IS NULL")
    assert parametros == (
        "CONFIRMADO",
        "IND-88-2026-0442-01",
        "TRX-1",
        None,
        1,
        2,
        resultado.siniestro_id,
    )
