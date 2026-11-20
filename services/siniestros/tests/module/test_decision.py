"""La decisión del analista y la tarea de plazos contra PostgreSQL real (HU-W20).

MT-W20-01: decisión, historial y Outbox en una transacción; repetición con la misma clave; 20
decisiones concurrentes; historial y decisiones sin `UPDATE` ni `DELETE`.
MT-W20-02: la tarea de plazos con el reloj inyectado y dos tareas a la vez (`SKIP LOCKED`).
"""

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from jsonschema import Draft202012Validator
from psycopg.types.json import Jsonb
from siniestros.adapters.outbound.mapeo import copia_a_json, ubicacion_a_json
from siniestros.adapters.outbound.siniestros_pg import SiniestrosPg
from siniestros.application.siniestro.decidir import (
    Decidir,
    SolicitudAprobar,
    SolicitudRechazar,
    SolicitudSoportar,
)
from siniestros.application.siniestro.errores import (
    IdempotenciaConflicto,
    TransicionNoPermitida,
)
from siniestros.application.siniestro.plazos import VencerPlazosSoporte
from siniestros.application.siniestro.puertos import DecisionFinalDuplicada
from siniestros.application.siniestro.vistas import Actor, DecisionRegistrada
from siniestros.domain.siniestro.modelos import (
    CanalSoporte,
    EstadoSiniestro,
    Evidencia,
    Siniestro,
    SoporteSolicitable,
)

pytestmark = pytest.mark.anyio
ANALISTA = UUID("a0a0a0a0-0000-4000-8000-000000000001")
ACTOR = Actor(str(ANALISTA), "ANALISTA_SINIESTROS", "Luis Mora")
E = EstadoSiniestro
EVENTOS = Path(__file__).parents[4] / "contracts" / "events"
AHORA = datetime(2026, 11, 5, 15, 0, tzinfo=UTC)


class RelojAjustable:
    def __init__(self, ahora: datetime) -> None:
        self.valor = ahora

    def ahora(self) -> datetime:
        return self.valor


@pytest.fixture
def reloj() -> RelojAjustable:
    return RelojAjustable(AHORA)


def guardar(conexion: psycopg.Connection[Any], s: Siniestro, *evidencias: Evidencia) -> None:
    conexion.execute(
        "INSERT INTO siniestros (id, codigo, origen, estado, poliza_id, cliente_id, copia_poliza, "
        "causa, fecha_evento, ubicacion_declarada, reportado_en, analista_id, asignado_en, "
        "pausado_desde, pausa_acumulada_s, plazo_soporte_vence_en, decidido_en, version, "
        "correlation_id, valor_reclamado, moneda, deducible, valor_indemnizar) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
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
            s.valor_reclamado,
            s.moneda,
            s.deducible,
            s.valor_indemnizar,
        ),
    )
    for e in evidencias:
        conexion.execute(
            "INSERT INTO evidencias (id, siniestro_id, nombre_archivo, tipo, soporte_tipo, "
            "clave_objeto, sha256, capturada_en, tipo_contenido) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                e.id,
                e.siniestro_id,
                e.nombre_archivo,
                e.tipo.value,
                e.soporte_tipo.value if e.soporte_tipo else None,
                e.clave_objeto,
                e.sha256,
                e.capturada_en,
                e.tipo_contenido,
            ),
        )


@pytest.fixture
def en_evaluacion(siniestro: Callable[..., Siniestro]) -> Callable[..., Siniestro]:
    def fabricar(**cambios: Any) -> Siniestro:
        datos: dict[str, Any] = {
            "estado": E.EN_EVALUACION,
            "analista_id": ANALISTA,
            "asignado_en": AHORA - timedelta(hours=13),
            "reportado_en": AHORA - timedelta(hours=14),
            "fecha_evento": AHORA - timedelta(hours=15),
            "codigo": f"88-2026-{uuid4().int % 9000 + 1000}",
            "correlation_id": "corr-w20",
        }
        datos.update(cambios)
        return siniestro(**datos)

    return fabricar


def aprobar(**cambios: Any) -> SolicitudAprobar:
    datos: dict[str, Any] = {
        "justificacion": "Daño verificado.",
        "evidencias_consideradas": (),
        "valor_reclamado": "2850000",
        "deducible": "350000",
        "valor_indemnizar": "2500000",
        "moneda": "COP",
    }
    datos.update(cambios)
    return SolicitudAprobar(**datos)


def rechazar(**cambios: Any) -> SolicitudRechazar:
    datos: dict[str, Any] = {
        "justificacion": "La póliza había vencido.",
        "evidencias_consideradas": (),
        "causal": "POLIZA_NO_VIGENTE",
        "explicacion_cliente": "Tu póliza venció y no fue renovada.",
    }
    datos.update(cambios)
    return SolicitudRechazar(**datos)


def soporte(**cambios: Any) -> SolicitudSoportar:
    datos: dict[str, Any] = {
        "justificacion": "Falta el certificado.",
        "evidencias_consideradas": (),
        "documentos": (SoporteSolicitable.CERTIFICADO_REPARACION,),
        "mensaje_cliente": "Necesitamos el certificado del taller.",
        "canal": CanalSoporte.PUSH_Y_CORREO,
        "plazo_dias_habiles": 10,
    }
    datos.update(cambios)
    return SolicitudSoportar(**datos)


def contar(conexion: psycopg.Connection[Any], tabla: str, donde: str = "true") -> int:
    fila = conexion.execute(f"SELECT count(*) FROM {tabla} WHERE {donde}").fetchone()
    assert fila is not None
    return int(fila[0])


def validar(tipo: str, payload: dict[str, Any], siniestro_id: UUID) -> None:
    esquema = json.loads((EVENTOS / f"{tipo}.v1.schema.json").read_text(encoding="utf-8"))
    sobre = {
        "eventId": str(uuid4()),
        "aggregateId": str(siniestro_id),
        "type": tipo,
        "version": 1,
        "occurredAt": AHORA.isoformat(),
        "correlationId": "c",
        "payload": payload,
    }
    validador = Draft202012Validator(esquema, format_checker=Draft202012Validator.FORMAT_CHECKER)
    assert list(validador.iter_errors(sobre)) == []


# --- MT-W20-01 -----------------------------------------------------------------------------------


async def test_aprobar_escribe_el_cambio_el_historial_la_decision_y_el_outbox_juntos(
    pool: Any,
    conexion: psycopg.Connection[Any],
    en_evaluacion,
    reloj: RelojAjustable,  # type: ignore[no-untyped-def]
) -> None:
    caso = en_evaluacion()
    guardar(conexion, caso)
    clave = uuid4()
    decidir = Decidir(SiniestrosPg(pool), reloj)
    resultado = await decidir.ejecutar(caso.id, ACTOR, ANALISTA, clave, aprobar())

    fila = conexion.execute(
        "SELECT estado, decidido_en, deducible, valor_indemnizar, version FROM siniestros "
        "WHERE id = %s",
        (caso.id,),
    ).fetchone()
    assert fila == (
        "APROBADO",
        AHORA,
        Decimal("350000.00"),
        Decimal("2500000.00"),
        caso.version + 1,
    )
    historial = conexion.execute(
        "SELECT accion, estado_anterior, estado_nuevo, actor_id, actor_nombre, justificacion, "
        "detalles FROM historial_siniestro WHERE siniestro_id = %s",
        (caso.id,),
    ).fetchall()
    assert historial == [
        (
            "APROBADO",
            "EN_EVALUACION",
            "APROBADO",
            str(ANALISTA),
            "Luis Mora",
            "Daño verificado.",
            {
                "valorIndemnizar": "2500000.00",
                "moneda": "COP",
                "referenciaPago": "IND-88-2026-0442-01"
                if caso.codigo == "88-2026-0442"
                else f"IND-{caso.codigo}-01",
            },
        )
    ]
    decision = conexion.execute(
        "SELECT id, tipo, idempotency_key, detalle FROM decisiones_siniestro WHERE siniestro_id=%s",
        (caso.id,),
    ).fetchall()
    assert [(d[0], d[1], d[2]) for d in decision] == [(resultado.decision_id, "APROBAR", clave)]
    assert decision[0][3]["liquidacion"]["valorReclamado"] == "2850000.00"

    outbox = conexion.execute(
        "SELECT event_type, aggregate_id, payload, correlation_id FROM outbox WHERE aggregate_id=%s",
        (caso.id,),
    ).fetchall()
    assert [(o[0], o[1]) for o in outbox] == [("SiniestroAprobado", caso.id)]
    payload = outbox[0][2]
    assert payload["referenciaPago"] == f"IND-{caso.codigo}-01"
    assert payload["beneficiarioRef"] == "ben-sint-004" and payload["decididoPor"] == str(ANALISTA)
    validar("SiniestroAprobado", payload, caso.id)
    assert contar(conexion, "idempotencia", f"clave = 'decision:{clave}'") == 1


async def test_repetir_con_la_misma_clave_no_agrega_filas_y_devuelve_lo_mismo(
    pool: Any,
    conexion: psycopg.Connection[Any],
    en_evaluacion,
    reloj: RelojAjustable,  # type: ignore[no-untyped-def]
) -> None:
    caso = en_evaluacion()
    guardar(conexion, caso)
    decidir = Decidir(SiniestrosPg(pool), reloj)
    clave = uuid4()
    primero = await decidir.ejecutar(caso.id, ACTOR, ANALISTA, clave, aprobar())
    segundo = await decidir.ejecutar(caso.id, ACTOR, ANALISTA, clave, aprobar())
    assert segundo == primero
    for tabla in ("historial_siniestro", "decisiones_siniestro", "outbox"):
        assert contar(conexion, tabla) == 1, tabla
    with pytest.raises(IdempotenciaConflicto):
        await decidir.ejecutar(caso.id, ACTOR, ANALISTA, clave, rechazar())


async def test_la_misma_clave_en_ocho_solicitudes_simultaneas_deja_una_sola_decision(
    pool: Any,
    conexion: psycopg.Connection[Any],
    en_evaluacion,
    reloj: RelojAjustable,  # type: ignore[no-untyped-def]
) -> None:
    caso = en_evaluacion()
    guardar(conexion, caso)
    decidir = Decidir(SiniestrosPg(pool), reloj)
    clave = uuid4()
    resultados = await asyncio.gather(
        *(decidir.ejecutar(caso.id, ACTOR, ANALISTA, clave, aprobar()) for _ in range(8))
    )
    assert len({r.decision_id for r in resultados}) == 1
    for tabla in ("historial_siniestro", "decisiones_siniestro", "outbox"):
        assert contar(conexion, tabla) == 1, tabla


async def test_veinte_decisiones_distintas_sobre_el_mismo_caso_dejan_una_sola_decision_final(
    pool: Any,
    conexion: psycopg.Connection[Any],
    en_evaluacion,
    reloj: RelojAjustable,  # type: ignore[no-untyped-def]
) -> None:
    caso = en_evaluacion()
    guardar(conexion, caso)
    decidir = Decidir(SiniestrosPg(pool), reloj)
    pedidos = [
        aprobar() if i % 3 == 0 else rechazar() if i % 3 == 1 else soporte() for i in range(20)
    ]
    resultados = await asyncio.gather(
        *(decidir.ejecutar(caso.id, ACTOR, ANALISTA, uuid4(), p) for p in pedidos),
        return_exceptions=True,
    )
    ganadoras = [r for r in resultados if not isinstance(r, BaseException)]
    perdedoras = [r for r in resultados if isinstance(r, BaseException)]
    assert len(ganadoras) == 1
    assert all(isinstance(r, TransicionNoPermitida) for r in perdedoras)
    assert len(perdedoras) == 19
    # Una sola decisión, un solo historial, un solo evento: el resto no escribió nada.
    for tabla in ("decisiones_siniestro", "historial_siniestro", "outbox"):
        assert contar(conexion, tabla) == 1, tabla
    estado = conexion.execute("SELECT estado FROM siniestros WHERE id = %s", (caso.id,)).fetchone()
    assert estado == (ganadoras[0].estado_nuevo.value,)
    # Todas las perdedoras vieron el estado nuevo, no «EN_EVALUACION».
    assert {r.detalles["estadoActual"] for r in perdedoras} == {estado[0]}  # type: ignore[union-attr,index]


async def test_decidir_un_caso_ya_decidido_no_toca_nada(
    pool: Any,
    conexion: psycopg.Connection[Any],
    en_evaluacion,
    reloj: RelojAjustable,  # type: ignore[no-untyped-def]
) -> None:
    caso = en_evaluacion()
    guardar(conexion, caso)
    decidir = Decidir(SiniestrosPg(pool), reloj)
    await decidir.ejecutar(caso.id, ACTOR, ANALISTA, uuid4(), rechazar())
    antes = conexion.execute("SELECT * FROM siniestros WHERE id = %s", (caso.id,)).fetchone()
    with pytest.raises(TransicionNoPermitida) as error:
        await decidir.ejecutar(caso.id, ACTOR, ANALISTA, uuid4(), aprobar())
    assert error.value.detalles == {"estadoActual": "RECHAZADO"}
    assert (
        conexion.execute("SELECT * FROM siniestros WHERE id = %s", (caso.id,)).fetchone() == antes
    )
    for tabla in ("decisiones_siniestro", "historial_siniestro", "outbox"):
        assert contar(conexion, tabla) == 1, tabla


async def test_rechazar_guarda_la_causal_y_la_explicacion_solo_en_la_decision(
    pool: Any,
    conexion: psycopg.Connection[Any],
    en_evaluacion,
    reloj: RelojAjustable,  # type: ignore[no-untyped-def]
) -> None:
    caso = en_evaluacion()
    guardar(conexion, caso)
    await Decidir(SiniestrosPg(pool), reloj).ejecutar(
        caso.id,
        ACTOR,
        ANALISTA,
        uuid4(),
        rechazar(explicacion_cliente="EXPLICACION-PRIVADA"),
    )
    assert conexion.execute(
        "SELECT estado, valor_indemnizar FROM siniestros WHERE id = %s", (caso.id,)
    ).fetchone() == ("RECHAZADO", None)
    (detalle,) = conexion.execute("SELECT detalle FROM decisiones_siniestro").fetchone()  # type: ignore[misc]
    assert detalle == {"causal": "POLIZA_NO_VIGENTE", "explicacionCliente": "EXPLICACION-PRIVADA"}
    fuera = conexion.execute(
        "SELECT (SELECT string_agg(h::text, ' ') FROM historial_siniestro h), "
        "(SELECT string_agg(o::text, ' ') FROM outbox o)"
    ).fetchone()
    assert "EXPLICACION-PRIVADA" not in repr(fuera)
    (payload,) = conexion.execute("SELECT payload FROM outbox").fetchone()  # type: ignore[misc]
    assert payload["causal"] == "POLIZA_NO_VIGENTE"
    validar("SiniestroRechazado", payload, caso.id)


async def test_solicitar_soporte_pausa_el_reloj_y_deja_el_plazo_en_dias_habiles(
    pool: Any,
    conexion: psycopg.Connection[Any],
    en_evaluacion,
    reloj: RelojAjustable,  # type: ignore[no-untyped-def]
) -> None:
    caso = en_evaluacion()
    guardar(conexion, caso)
    resultado = await Decidir(SiniestrosPg(pool), reloj).ejecutar(
        caso.id, ACTOR, ANALISTA, uuid4(), soporte()
    )
    assert resultado.estado_nuevo is E.FALTA_SOPORTE
    fila = conexion.execute(
        "SELECT estado, pausado_desde, plazo_soporte_vence_en FROM siniestros WHERE id = %s",
        (caso.id,),
    ).fetchone()
    # 2026-11-05 es jueves: 10 días hábiles después, jueves 19 a la misma hora.
    assert fila == ("FALTA_SOPORTE", AHORA, AHORA + timedelta(days=14))
    (payload,) = conexion.execute("SELECT payload FROM outbox").fetchone()  # type: ignore[misc]
    assert payload["documentos"] == ["CERTIFICADO_REPARACION"]
    assert "certificado del taller" not in json.dumps(payload)
    validar("SiniestroSoporteSolicitado", payload, caso.id)


async def test_el_historial_y_las_decisiones_no_admiten_update_ni_delete(
    pool: Any,
    conexion: psycopg.Connection[Any],
    en_evaluacion,
    reloj: RelojAjustable,  # type: ignore[no-untyped-def]
) -> None:
    caso = en_evaluacion()
    guardar(conexion, caso)
    await Decidir(SiniestrosPg(pool), reloj).ejecutar(caso.id, ACTOR, ANALISTA, uuid4(), aprobar())
    for sentencia in (
        "UPDATE historial_siniestro SET justificacion = 'otra'",
        "DELETE FROM historial_siniestro",
        "UPDATE decisiones_siniestro SET tipo = 'RECHAZAR'",
        "DELETE FROM decisiones_siniestro",
    ):
        with pytest.raises(psycopg.errors.RaiseException, match="solo inserción"):
            conexion.execute(sentencia)  # type: ignore[arg-type]
    assert contar(conexion, "historial_siniestro") == 1
    assert contar(conexion, "decisiones_siniestro") == 1


async def test_la_restriccion_unica_rechaza_una_segunda_decision_final_y_deshace_todo(
    pool: Any, conexion: psycopg.Connection[Any], en_evaluacion
) -> None:
    caso = en_evaluacion()
    guardar(conexion, caso)
    repo = SiniestrosPg(pool)

    def decision() -> DecisionRegistrada:
        return DecisionRegistrada(
            uuid4(), caso.id, "APROBAR", uuid4(), "h" * 64, {}, str(ANALISTA), AHORA
        )

    async with repo.transaccion() as tx:
        await tx.registrar_decision(decision())
    with pytest.raises(DecisionFinalDuplicada):
        async with repo.transaccion() as tx:
            await tx.guardar_idempotencia("decision:x", "h", {}, AHORA)
            await tx.registrar_decision(decision())
    assert contar(conexion, "decisiones_siniestro") == 1
    assert contar(conexion, "idempotencia", "clave = 'decision:x'") == 0


async def test_el_detalle_de_un_caso_aprobado_muestra_la_liquidacion_y_el_tramite(
    pool: Any,
    conexion: psycopg.Connection[Any],
    en_evaluacion,
    reloj: RelojAjustable,  # type: ignore[no-untyped-def]
) -> None:
    caso = en_evaluacion()
    guardar(conexion, caso)
    repo = SiniestrosPg(pool)
    await Decidir(repo, reloj).ejecutar(caso.id, ACTOR, ANALISTA, uuid4(), aprobar())
    leido = await repo.detalle(caso.id)
    assert leido is not None and leido.liquidacion is not None
    assert leido.liquidacion.valor_reclamado == Decimal("2850000.00")
    assert leido.liquidacion.deducible == Decimal("350000.00")
    assert leido.liquidacion.valor_indemnizar == Decimal("2500000.00")
    assert leido.liquidacion.referencia_pago == f"IND-{caso.codigo}-01"
    assert leido.liquidacion.aprobada_en == AHORA
    assert [h.accion for h in leido.historial] == ["APROBADO"]
    assert leido.historial[0].detalles is not None


# --- MT-W20-02: la tarea de plazos ---------------------------------------------------------------


def en_soporte(
    en_evaluacion: Callable[..., Siniestro], vencido_hace: timedelta, **cambios: Any
) -> Siniestro:
    return en_evaluacion(
        estado=E.FALTA_SOPORTE,
        pausado_desde=AHORA - timedelta(days=5),
        pausa_acumulada_s=60,
        plazo_soporte_vence_en=AHORA - vencido_hace,
        **cambios,
    )


async def test_la_tarea_devuelve_los_vencidos_al_mismo_analista_con_el_reloj_reanudado(
    pool: Any,
    conexion: psycopg.Connection[Any],
    en_evaluacion,
    reloj: RelojAjustable,  # type: ignore[no-untyped-def]
) -> None:
    vencido = en_soporte(en_evaluacion, timedelta(hours=1))
    vigente = en_soporte(en_evaluacion, -timedelta(days=2))
    guardar(conexion, vencido)
    guardar(conexion, vigente)
    tarea = VencerPlazosSoporte(SiniestrosPg(pool), reloj, lote=10)
    assert await tarea.pasada() == 1

    fila = conexion.execute(
        "SELECT estado, analista_id, pausado_desde, plazo_soporte_vence_en, pausa_acumulada_s "
        "FROM siniestros WHERE id = %s",
        (vencido.id,),
    ).fetchone()
    assert fila == ("EN_EVALUACION", ANALISTA, None, None, 60 + 5 * 24 * 3600)
    assert conexion.execute(
        "SELECT estado FROM siniestros WHERE id = %s", (vigente.id,)
    ).fetchone() == ("FALTA_SOPORTE",)
    assert conexion.execute(
        "SELECT accion, actor_rol, actor_id, estado_anterior, estado_nuevo "
        "FROM historial_siniestro WHERE siniestro_id = %s",
        (vencido.id,),
    ).fetchall() == [("PLAZO_SOPORTE_VENCIDO", "SISTEMA", None, "FALTA_SOPORTE", "EN_EVALUACION")]
    ((tipo, payload, correlacion),) = conexion.execute(
        "SELECT event_type, payload, correlation_id FROM outbox"
    ).fetchall()
    assert (tipo, payload["motivo"], payload["analistaId"]) == (
        "SiniestroEnEvaluacion",
        "PLAZO_SOPORTE_VENCIDO",
        str(ANALISTA),
    )
    assert correlacion == "corr-w20"  # la correlación del caso, no una nueva
    validar("SiniestroEnEvaluacion", payload, vencido.id)


async def test_dos_tareas_a_la_vez_no_procesan_dos_veces_el_mismo_caso(
    pool: Any,
    conexion: psycopg.Connection[Any],
    en_evaluacion,
    reloj: RelojAjustable,  # type: ignore[no-untyped-def]
) -> None:
    casos = [en_soporte(en_evaluacion, timedelta(hours=1 + n)) for n in range(30)]
    for caso in casos:
        guardar(conexion, caso)
    primera = VencerPlazosSoporte(SiniestrosPg(pool), reloj, lote=4)
    segunda = VencerPlazosSoporte(SiniestrosPg(pool), reloj, lote=4)
    totales = await asyncio.gather(primera.pasada(), segunda.pasada())
    assert sum(totales) == 30
    assert contar(conexion, "siniestros", "estado = 'EN_EVALUACION'") == 30
    # Un solo historial y un solo evento por caso: ninguna tarea repitió el trabajo de la otra.
    assert contar(conexion, "historial_siniestro") == 30
    assert contar(conexion, "outbox") == 30
    assert conexion.execute(
        "SELECT max(n) FROM (SELECT count(*) n FROM historial_siniestro GROUP BY siniestro_id) t"
    ).fetchone() == (1,)


async def test_ciclo_completo_soporte_vencimiento_y_aprobacion(
    pool: Any,
    conexion: psycopg.Connection[Any],
    en_evaluacion,
    reloj: RelojAjustable,  # type: ignore[no-untyped-def]
) -> None:
    caso = en_evaluacion()
    guardar(conexion, caso)
    repo = SiniestrosPg(pool)
    decidir = Decidir(repo, reloj)
    tarea = VencerPlazosSoporte(repo, reloj, lote=10)

    pedido = await decidir.ejecutar(
        caso.id, ACTOR, ANALISTA, uuid4(), soporte(plazo_dias_habiles=5)
    )
    assert await tarea.pasada() == 0  # el plazo no ha vencido
    # Mientras espera, ninguna decisión es válida.
    with pytest.raises(TransicionNoPermitida) as error:
        await decidir.ejecutar(caso.id, ACTOR, ANALISTA, uuid4(), aprobar())
    assert error.value.detalles == {"estadoActual": "FALTA_SOPORTE"}

    assert pedido.plazo_soporte_vence_en is not None
    reloj.valor = pedido.plazo_soporte_vence_en + timedelta(hours=1)  # el reloj adelantado
    assert await tarea.pasada() == 1
    assert await tarea.pasada() == 0  # idempotente: no vuelve a tocarlo

    reloj.valor += timedelta(minutes=5)  # el analista decide un rato después
    reanudado = await decidir.ejecutar(caso.id, ACTOR, ANALISTA, uuid4(), aprobar())
    assert reanudado.estado_nuevo is E.APROBADO
    acciones = conexion.execute(
        "SELECT accion FROM historial_siniestro WHERE siniestro_id = %s ORDER BY ocurrido_en, id",
        (caso.id,),
    ).fetchall()
    assert [a[0] for a in acciones] == ["SOPORTE_SOLICITADO", "PLAZO_SOPORTE_VENCIDO", "APROBADO"]
    # Pidió soporte una vez y decidió una vez.
    assert contar(conexion, "decisiones_siniestro", "tipo = 'SOLICITAR_SOPORTE'") == 1
    assert contar(conexion, "decisiones_siniestro", "tipo = 'APROBAR'") == 1
    # El reloj no corrió mientras esperaba: la pausa quedó acumulada.
    pausa = conexion.execute(
        "SELECT pausa_acumulada_s FROM siniestros WHERE id = %s", (caso.id,)
    ).fetchone()
    assert pausa is not None and pausa[0] > 5 * 24 * 3600
