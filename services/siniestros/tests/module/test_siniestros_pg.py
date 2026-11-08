"""SiniestrosPg contra PostgreSQL real (HU-W19): bandeja, indicadores, detalle y tomar el siguiente."""

import asyncio
import json
from collections.abc import Callable
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb
from siniestros.adapters.outbound.mapeo import copia_a_json, ubicacion_a_json
from siniestros.adapters.outbound.siniestros_pg import SiniestrosPg
from siniestros.application.siniestro.errores import (
    ColaSinCasos,
    IdempotenciaConflicto,
    SolicitudInvalida,
)
from siniestros.application.siniestro.tomar import TomarSiguiente
from siniestros.application.siniestro.vistas import (
    Actor,
    FiltroBandeja,
    Plazos,
    TipoBandeja,
)
from siniestros.domain.siniestro.modelos import (
    EstadoSiniestro,
    Evidencia,
    OrigenSiniestro,
    PrioridadSiniestro,
    Siniestro,
    TipoEvidencia,
    TipoSoporte,
)
from siniestros.domain.siniestro.plazos import prioridad, tiempo_restante

pytestmark = pytest.mark.anyio
ANALISTA = UUID("a0a0a0a0-0000-4000-8000-000000000001")
OTRO = UUID("a0a0a0a0-0000-4000-8000-000000000002")
PLAZOS = Plazos(48, 480, 1440)


def guardar(conexion: psycopg.Connection[Any], s: Siniestro, *evidencias: Evidencia) -> None:
    conexion.execute(
        "INSERT INTO siniestros (id, codigo, origen, estado, poliza_id, cliente_id, copia_poliza, "
        "causa, fecha_evento, ubicacion_declarada, reportado_en, analista_id, asignado_en, "
        "pausado_desde, pausa_acumulada_s, plazo_soporte_vence_en, decidido_en, version, "
        "correlation_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            s.id,
            s.codigo,
            s.origen.value,
            s.estado.value,
            s.poliza_id,
            s.cliente_id,
            Jsonb(copia_a_json(s.copia_poliza)),
            s.causa,
            s.fecha_evento,
            Jsonb(ubicacion_a_json(s.ubicacion_declarada)),
            s.reportado_en,
            s.analista_id,
            s.asignado_en,
            s.pausado_desde,
            s.pausa_acumulada_s,
            s.plazo_soporte_vence_en,
            s.decidido_en,
            s.version,
            s.correlation_id,
        ),
    )
    for e in evidencias:
        conexion.execute(
            "INSERT INTO evidencias (id, siniestro_id, nombre_archivo, tipo, soporte_tipo, "
            "clave_objeto, sha256, capturada_en, ubicacion, tamano_bytes, tipo_contenido) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                e.id,
                e.siniestro_id,
                e.nombre_archivo,
                e.tipo.value,
                e.soporte_tipo.value if e.soporte_tipo else None,
                e.clave_objeto,
                e.sha256,
                e.capturada_en,
                Jsonb(ubicacion_a_json(e.ubicacion)) if e.ubicacion else None,
                e.tamano_bytes,
                e.tipo_contenido,
            ),
        )


def filtro(ahora: Any, **cambios: Any) -> FiltroBandeja:
    datos: dict[str, Any] = {
        "bandeja": TipoBandeja.MIA,
        "actor_id": ANALISTA,
        "corte": ahora,
        "plazos": PLAZOS,
    }
    datos.update(cambios)
    return FiltroBandeja(**datos)


@pytest.fixture
def casos(
    conexion: psycopg.Connection[Any], siniestro: Callable[..., Siniestro], ahora: Any
) -> dict[str, Siniestro]:
    """Seis casos: tres sin asignar y tres del analista QA (uno vencido, uno medio, uno holgado)."""

    def en(horas: float) -> dict[str, Any]:
        return {
            "reportado_en": ahora - timedelta(hours=horas),
            "fecha_evento": ahora - timedelta(hours=horas + 1),
        }

    asignado = {"analista_id": ANALISTA, "asignado_en": ahora - timedelta(hours=1)}
    datos = {
        "SIN-01": siniestro(codigo="88-2026-0431", **en(20)),
        "SIN-02": siniestro(codigo="88-2026-0433", **en(5)),
        "SIN-03": siniestro(codigo="88-2026-0436", origen=OrigenSiniestro.PORTAL, **en(1)),
        "VEN-02": siniestro(
            codigo="88-2026-0439", estado=EstadoSiniestro.EN_EVALUACION, **asignado, **en(50)
        ),
        "MED-01": siniestro(
            codigo="88-2026-0440", estado=EstadoSiniestro.EN_EVALUACION, **asignado, **en(30)
        ),
        "EVA-01": siniestro(
            codigo="88-2026-0442", estado=EstadoSiniestro.EN_EVALUACION, **asignado, **en(14)
        ),
    }
    for caso in datos.values():
        guardar(conexion, caso)
    return datos


# --- Bandeja ---------------------------------------------------------------------------------------
async def test_mi_bandeja_va_del_menor_tiempo_restante_al_mayor(
    pool: Any, casos: dict[str, Siniestro], ahora: Any
) -> None:
    pagina = await SiniestrosPg(pool).bandeja(filtro(ahora))
    assert [s.codigo for s in pagina.items] == ["88-2026-0439", "88-2026-0440", "88-2026-0442"]
    assert (pagina.total, pagina.siguiente_cursor) == (3, None)


async def test_la_cola_general_va_del_mas_antiguo_al_mas_nuevo(
    pool: Any, casos: dict[str, Siniestro], ahora: Any
) -> None:
    pagina = await SiniestrosPg(pool).bandeja(filtro(ahora, bandeja=TipoBandeja.SIN_ASIGNAR))
    assert [s.codigo for s in pagina.items] == ["88-2026-0431", "88-2026-0433", "88-2026-0436"]


async def test_pagina_por_cursor_sin_repetir_ni_saltar_casos(
    pool: Any, casos: dict[str, Siniestro], ahora: Any
) -> None:
    repo = SiniestrosPg(pool)
    vistos: list[str] = []
    cursor = None
    for _ in range(5):
        pagina = await repo.bandeja(filtro(ahora, limite=2, cursor=cursor))
        vistos += [s.codigo for s in pagina.items]
        assert pagina.total == 3
        cursor = pagina.siguiente_cursor
        if cursor is None:
            break
    assert vistos == ["88-2026-0439", "88-2026-0440", "88-2026-0442"]


async def test_la_cola_general_tambien_pagina(
    pool: Any, casos: dict[str, Siniestro], ahora: Any
) -> None:
    repo = SiniestrosPg(pool)
    primera = await repo.bandeja(filtro(ahora, bandeja=TipoBandeja.SIN_ASIGNAR, limite=2))
    segunda = await repo.bandeja(
        filtro(ahora, bandeja=TipoBandeja.SIN_ASIGNAR, limite=2, cursor=primera.siguiente_cursor)
    )
    assert [s.codigo for s in primera.items + segunda.items] == [
        "88-2026-0431",
        "88-2026-0433",
        "88-2026-0436",
    ]
    assert segunda.siguiente_cursor is None


async def test_el_orden_de_las_paginas_no_se_mueve_aunque_pase_el_tiempo(
    pool: Any, casos: dict[str, Siniestro], ahora: Any
) -> None:
    repo = SiniestrosPg(pool)
    primera = await repo.bandeja(filtro(ahora, limite=1))
    # La segunda página se pide una hora después: usa el corte de la primera.
    segunda = await repo.bandeja(
        filtro(ahora + timedelta(hours=1), limite=2, cursor=primera.siguiente_cursor)
    )
    assert [s.codigo for s in primera.items + segunda.items] == [
        "88-2026-0439",
        "88-2026-0440",
        "88-2026-0442",
    ]


@pytest.mark.parametrize(
    ("prioridad_pedida", "esperados"),
    [
        (PrioridadSiniestro.ALTA, ["88-2026-0439"]),
        (PrioridadSiniestro.MEDIA, ["88-2026-0440"]),
        (PrioridadSiniestro.BAJA, ["88-2026-0442"]),
    ],
)
async def test_filtra_por_prioridad(
    pool: Any,
    casos: dict[str, Siniestro],
    ahora: Any,
    prioridad_pedida: PrioridadSiniestro,
    esperados: list[str],
) -> None:
    pagina = await SiniestrosPg(pool).bandeja(filtro(ahora, prioridad=prioridad_pedida))
    assert [s.codigo for s in pagina.items] == esperados
    assert pagina.total == len(esperados)


async def test_filtra_por_estado_origen_y_texto(
    pool: Any, casos: dict[str, Siniestro], ahora: Any
) -> None:
    repo = SiniestrosPg(pool)
    portal = await repo.bandeja(
        filtro(ahora, bandeja=TipoBandeja.SIN_ASIGNAR, origen=OrigenSiniestro.PORTAL)
    )
    assert [s.codigo for s in portal.items] == ["88-2026-0436"]
    estado = await repo.bandeja(filtro(ahora, estado=EstadoSiniestro.REPORTADO))
    assert estado.items == ()
    por_codigo = await repo.bandeja(filtro(ahora, texto="0442"))
    assert [s.codigo for s in por_codigo.items] == ["88-2026-0442"]
    por_poliza = await repo.bandeja(filtro(ahora, texto="220-2026-011294"))
    assert por_poliza.total == 3
    assert (await repo.bandeja(filtro(ahora, texto="no-existe"))).total == 0
    # Los comodines del texto no se interpretan.
    assert (await repo.bandeja(filtro(ahora, texto="%"))).total == 0
    assert (await repo.bandeja(filtro(ahora, texto="_"))).total == 0


async def test_no_muestra_los_casos_de_otro_analista(
    pool: Any, casos: dict[str, Siniestro], ahora: Any
) -> None:
    pagina = await SiniestrosPg(pool).bandeja(filtro(ahora, actor_id=OTRO))
    assert pagina.items == ()


async def test_el_tiempo_restante_de_sql_coincide_con_el_del_dominio(
    pool: Any,
    conexion: psycopg.Connection[Any],
    siniestro: Callable[..., Siniestro],
    ahora: Any,
) -> None:
    """Pausado, con pausa acumulada, decidido y vencido: la prioridad del filtro en SQL es la que
    calcula el dominio con `tiempo_restante` y `prioridad`."""
    base = {"analista_id": ANALISTA, "asignado_en": ahora - timedelta(hours=40)}
    reportado = ahora - timedelta(hours=44)
    variantes = [
        siniestro(
            codigo="88-2026-0501",
            reportado_en=reportado,
            fecha_evento=reportado - timedelta(hours=1),
            estado=EstadoSiniestro.EN_EVALUACION,
            **base,
        ),
        siniestro(
            codigo="88-2026-0502",
            reportado_en=reportado,
            fecha_evento=reportado - timedelta(hours=1),
            estado=EstadoSiniestro.EN_EVALUACION,
            pausa_acumulada_s=3600 * 30,
            **base,
        ),
        siniestro(
            codigo="88-2026-0503",
            reportado_en=reportado,
            fecha_evento=reportado - timedelta(hours=1),
            estado=EstadoSiniestro.FALTA_SOPORTE,
            pausado_desde=ahora - timedelta(hours=10),
            **base,
        ),
        siniestro(
            codigo="88-2026-0504",
            reportado_en=reportado,
            fecha_evento=reportado - timedelta(hours=1),
            estado=EstadoSiniestro.APROBADO,
            decidido_en=ahora - timedelta(hours=2),
            **base,
        ),
        siniestro(
            codigo="88-2026-0505",
            reportado_en=reportado,
            fecha_evento=reportado - timedelta(hours=1),
            estado=EstadoSiniestro.EN_EVALUACION,
            pausa_acumulada_s=1800,
            **base,
        ),
    ]
    for v in variantes:
        guardar(conexion, v)
    repo = SiniestrosPg(pool)
    for esperada in PrioridadSiniestro:
        pagina = await repo.bandeja(filtro(ahora, prioridad=esperada, limite=100))
        de_sql = {s.codigo for s in pagina.items}
        de_dominio = {
            v.codigo
            for v in variantes
            if prioridad(tiempo_restante(v, ahora), alta_min=480, media_min=1440) is esperada
        }
        assert de_sql == de_dominio, esperada


# --- Indicadores -----------------------------------------------------------------------------------
async def test_indicadores_de_la_semilla(
    pool: Any, casos: dict[str, Siniestro], ahora: Any
) -> None:
    desde = ahora.replace(hour=5)
    resultado = await SiniestrosPg(pool).indicadores(
        ANALISTA, desde, desde + timedelta(days=1), ahora, 48
    )
    assert (resultado.pendientes, resultado.asignados_al_actor, resultado.sin_asignar) == (3, 3, 3)
    assert resultado.fuera_de_compromiso == 1
    assert (resultado.resueltos_semana, resultado.resueltos_semana_previa) == (0, 0)
    assert resultado.tiempo_medio_decision_horas is None
    assert resultado.compromiso_horas == 48


async def test_vencen_hoy_cuenta_los_que_cumplen_el_compromiso_en_el_dia(
    pool: Any, casos: dict[str, Siniestro], ahora: Any
) -> None:
    # MED-01 vence 18 h después de `ahora`; EVA-01, 34 h después.
    repo = SiniestrosPg(pool)
    manana = ahora + timedelta(hours=17)
    uno = await repo.indicadores(ANALISTA, manana, manana + timedelta(hours=2), ahora, 48)
    assert uno.vencen_hoy == 1
    nada = await repo.indicadores(ANALISTA, ahora, ahora + timedelta(hours=10), ahora, 48)
    assert nada.vencen_hoy == 0


async def test_resueltos_de_la_semana_y_tiempo_medio(
    pool: Any, conexion: psycopg.Connection[Any], siniestro: Callable[..., Siniestro], ahora: Any
) -> None:
    def resuelto(
        codigo: str, decidido_hace_h: float, tardo_h: float, estado: EstadoSiniestro
    ) -> Siniestro:
        decidido = ahora - timedelta(hours=decidido_hace_h)
        reportado = decidido - timedelta(hours=tardo_h)
        return siniestro(
            codigo=codigo,
            estado=estado,
            analista_id=ANALISTA,
            asignado_en=reportado + timedelta(hours=1),
            reportado_en=reportado,
            fecha_evento=reportado - timedelta(hours=1),
            decidido_en=decidido,
        )

    for caso in (
        resuelto("88-2026-0601", 24, 10, EstadoSiniestro.APROBADO),
        resuelto("88-2026-0602", 48, 20, EstadoSiniestro.RECHAZADO),
        resuelto("88-2026-0603", 24 * 9, 5, EstadoSiniestro.APROBADO),
        resuelto("88-2026-0604", 24 * 20, 5, EstadoSiniestro.APROBADO),
    ):
        guardar(conexion, caso)
    resultado = await SiniestrosPg(pool).indicadores(
        ANALISTA, ahora, ahora + timedelta(days=1), ahora + timedelta(days=1), 48
    )
    # `hasta` = mañana: los dos primeros caen en [hasta−7 d, hasta), el tercero en la semana previa.
    assert resultado.resueltos_semana == 2
    assert resultado.resueltos_semana_previa == 1
    assert resultado.tiempo_medio_decision_horas == 15.0


# --- Detalle y evidencia ---------------------------------------------------------------------------
async def test_detalle_devuelve_el_caso_sus_evidencias_y_su_historial_en_orden(
    pool: Any,
    conexion: psycopg.Connection[Any],
    siniestro: Callable[..., Siniestro],
    evidencia: Callable[..., Evidencia],
    ahora: Any,
) -> None:
    caso = siniestro()
    tarde = evidencia(
        siniestro_id=caso.id,
        nombre_archivo="b.jpg",
        capturada_en=ahora - timedelta(hours=1),
        tamano_bytes=2048,
        tipo_contenido="image/jpeg",
    )
    temprano = evidencia(
        siniestro_id=caso.id, nombre_archivo="a.jpg", capturada_en=ahora - timedelta(hours=2)
    )
    sin_fecha = evidencia(
        siniestro_id=caso.id,
        nombre_archivo="c.pdf",
        capturada_en=None,
        ubicacion=None,
        tipo=TipoEvidencia.SOPORTE,
        soporte_tipo=TipoSoporte.PERITAJE,
    )
    guardar(conexion, caso, tarde, temprano, sin_fecha)
    for orden, accion in enumerate(("EVIDENCIAS_VALIDADAS", "REPORTADO")):
        conexion.execute(
            "INSERT INTO historial_siniestro (id, siniestro_id, accion, ocurrido_en) VALUES (%s,%s,%s,%s)",
            (uuid4(), caso.id, accion, ahora - timedelta(hours=14 - (1 - orden))),
        )
    leido = await SiniestrosPg(pool).detalle(caso.id)
    assert leido is not None
    assert leido.siniestro == caso
    assert [e.nombre_archivo for e in leido.evidencias] == ["a.jpg", "b.jpg", "c.pdf"]
    assert leido.evidencias[1] == tarde
    assert leido.evidencias[2] == sin_fecha
    assert [h.accion for h in leido.historial] == ["REPORTADO", "EVIDENCIAS_VALIDADAS"]
    assert await SiniestrosPg(pool).detalle(uuid4()) is None


async def test_evidencia_solo_dentro_de_su_siniestro(
    pool: Any,
    conexion: psycopg.Connection[Any],
    siniestro: Callable[..., Siniestro],
    evidencia: Callable[..., Evidencia],
) -> None:
    uno, otro = siniestro(), siniestro(codigo="88-2026-0443")
    archivo = evidencia(siniestro_id=uno.id)
    guardar(conexion, uno, archivo)
    guardar(conexion, otro)
    repo = SiniestrosPg(pool)
    assert await repo.evidencia(uno.id, archivo.id) == (True, archivo)
    assert await repo.evidencia(otro.id, archivo.id) == (True, None)
    assert await repo.evidencia(uuid4(), archivo.id) == (False, None)


# --- Tomar el siguiente ----------------------------------------------------------------------------
class RelojTest:
    def __init__(self, ahora: Any) -> None:
        self._ahora = ahora

    def ahora(self) -> Any:
        return self._ahora


def tomar_con(pool: Any, ahora: Any) -> TomarSiguiente:
    return TomarSiguiente(SiniestrosPg(pool), RelojTest(ahora), PLAZOS)


def actor(analista: UUID, nombre: str = "Luis Mora") -> Actor:
    return Actor(str(analista), "ANALISTA_SINIESTROS", nombre)


async def test_tomar_deja_asignacion_historial_evento_e_idempotencia_en_una_transaccion(
    pool: Any, conexion: psycopg.Connection[Any], siniestro: Callable[..., Siniestro], ahora: Any
) -> None:
    libre = siniestro()
    guardar(conexion, libre)
    clave = uuid4()
    resumen = await tomar_con(pool, ahora).ejecutar(actor(ANALISTA), ANALISTA, clave)
    assert resumen.siniestro.estado is EstadoSiniestro.EN_EVALUACION
    fila = conexion.execute(
        "SELECT estado, analista_id, asignado_en, version FROM siniestros WHERE id = %s",
        (libre.id,),
    ).fetchone()
    assert fila == ("EN_EVALUACION", ANALISTA, ahora, 2)
    historial = conexion.execute(
        "SELECT accion, estado_anterior, estado_nuevo, actor_id, actor_rol, actor_nombre FROM historial_siniestro"
    ).fetchall()
    assert historial == [
        (
            "ASIGNADO",
            "REPORTADO",
            "EN_EVALUACION",
            str(ANALISTA),
            "ANALISTA_SINIESTROS",
            "Luis Mora",
        )
    ]
    evento = conexion.execute(
        "SELECT event_type, aggregate_id, payload, published_at FROM outbox"
    ).fetchall()
    assert len(evento) == 1
    tipo, agregado, payload, publicado = evento[0]
    assert (tipo, agregado, publicado) == ("SiniestroEnEvaluacion", libre.id, None)
    assert payload["analistaId"] == str(ANALISTA)
    assert "Luis" not in json.dumps(payload)
    assert conexion.execute("SELECT clave FROM idempotencia").fetchall() == [
        (f"siguiente:{clave}",)
    ]


async def test_la_misma_clave_devuelve_el_mismo_caso_aunque_haya_mas(
    pool: Any, conexion: psycopg.Connection[Any], siniestro: Callable[..., Siniestro], ahora: Any
) -> None:
    for n in range(3):
        guardar(
            conexion,
            siniestro(
                codigo=f"88-2026-070{n}",
                reportado_en=ahora - timedelta(hours=10 - n),
                fecha_evento=ahora - timedelta(hours=12),
            ),
        )
    clave = uuid4()
    tomar = tomar_con(pool, ahora)
    uno = await tomar.ejecutar(actor(ANALISTA), ANALISTA, clave)
    otra_vez = await tomar.ejecutar(actor(ANALISTA), ANALISTA, clave)
    assert uno.siniestro.id == otra_vez.siniestro.id
    assert conexion.execute("SELECT count(*) FROM historial_siniestro").fetchone() == (1,)
    assert conexion.execute("SELECT count(*) FROM outbox").fetchone() == (1,)
    with pytest.raises(IdempotenciaConflicto):
        await tomar.ejecutar(actor(OTRO), OTRO, clave)


async def test_solicitudes_simultaneas_con_la_misma_clave_asignan_un_solo_caso(
    pool: Any, conexion: psycopg.Connection[Any], siniestro: Callable[..., Siniestro], ahora: Any
) -> None:
    for n in range(4):
        guardar(
            conexion,
            siniestro(
                codigo=f"88-2026-080{n}",
                reportado_en=ahora - timedelta(hours=10 - n),
                fecha_evento=ahora - timedelta(hours=12),
            ),
        )
    clave = uuid4()
    tomar = tomar_con(pool, ahora)
    resultados = await asyncio.gather(
        *(tomar.ejecutar(actor(ANALISTA), ANALISTA, clave) for _ in range(8))
    )
    assert len({r.siniestro.id for r in resultados}) == 1
    assert conexion.execute(
        "SELECT count(*) FROM siniestros WHERE estado = 'EN_EVALUACION'"
    ).fetchone() == (1,)
    assert conexion.execute("SELECT count(*) FROM outbox").fetchone() == (1,)


async def test_dos_analistas_nunca_toman_el_mismo_caso(
    pool: Any, conexion: psycopg.Connection[Any], siniestro: Callable[..., Siniestro], ahora: Any
) -> None:
    """AC2: 12 analistas piden a la vez con 5 casos libres. Con SKIP LOCKED cada caso se asigna a
    exactamente uno y los demás reciben cola vacía (sin esperar ni repetir)."""
    for n in range(5):
        guardar(
            conexion,
            siniestro(
                codigo=f"88-2026-090{n}",
                reportado_en=ahora - timedelta(hours=10 - n),
                fecha_evento=ahora - timedelta(hours=12),
            ),
        )
    analistas = [UUID(int=0xA0A0A0A0 << 96 | n) for n in range(1, 13)]
    tomar = tomar_con(pool, ahora)
    resultados = await asyncio.gather(
        *(tomar.ejecutar(actor(a, f"Analista {n}"), a, uuid4()) for n, a in enumerate(analistas)),
        return_exceptions=True,
    )
    asignados = [r for r in resultados if not isinstance(r, BaseException)]
    vacios = [r for r in resultados if isinstance(r, ColaSinCasos)]
    assert len(asignados) == 5
    assert len(vacios) == 7
    assert len({r.siniestro.id for r in asignados}) == 5  # type: ignore[union-attr]
    assert len({r.siniestro.analista_id for r in asignados}) == 5  # type: ignore[union-attr]
    assert conexion.execute(
        "SELECT count(*) FROM siniestros WHERE estado = 'REPORTADO'"
    ).fetchone() == (0,)
    assert conexion.execute("SELECT count(*) FROM outbox").fetchone() == (5,)


async def test_cola_vacia_no_deja_filas(
    pool: Any, conexion: psycopg.Connection[Any], ahora: Any
) -> None:
    with pytest.raises(ColaSinCasos):
        await tomar_con(pool, ahora).ejecutar(actor(ANALISTA), ANALISTA, uuid4())
    for tabla in ("historial_siniestro", "outbox", "idempotencia"):
        assert conexion.execute(f"SELECT count(*) FROM {tabla}").fetchone() == (0,)


async def test_si_la_transaccion_falla_no_queda_nada_a_medias(
    pool: Any, conexion: psycopg.Connection[Any], siniestro: Callable[..., Siniestro], ahora: Any
) -> None:
    guardar(conexion, siniestro())

    # Una entrada del historial inválida (acción en minúscula) rompe la transacción al escribirla.
    class Roto(SiniestrosPg):
        pass

    repo = SiniestrosPg(pool)
    with pytest.raises(psycopg.errors.CheckViolation):
        async with repo.transaccion() as tx:
            libre = await tx.reservar_siguiente()
            assert libre is not None
            from siniestros.domain.siniestro.modelos import EntradaHistorial
            from siniestros.domain.siniestro.transiciones import asignar

            await tx.guardar_asignacion(asignar(libre, ANALISTA, ahora), libre.version)
            await tx.agregar_historial(EntradaHistorial(uuid4(), libre.id, "mala", ahora))
    assert conexion.execute("SELECT estado, analista_id FROM siniestros").fetchone() == (
        "REPORTADO",
        None,
    )


async def test_el_cursor_malo_es_solicitud_invalida_con_base_real(pool: Any, ahora: Any) -> None:
    with pytest.raises(SolicitudInvalida):
        await SiniestrosPg(pool).bandeja(filtro(ahora, cursor="basura"))


async def test_las_conexiones_del_pool_siguen_devolviendo_tuplas(
    pool: Any, casos: dict[str, Siniestro], ahora: Any
) -> None:
    """El relay del Outbox comparte el pool y lee tuplas: las lecturas no cambian la fábrica."""
    await SiniestrosPg(pool).bandeja(filtro(ahora))
    async with pool.connection() as conexion:
        fila = await (await conexion.execute("SELECT 1, 2")).fetchone()
    assert fila == (1, 2)
