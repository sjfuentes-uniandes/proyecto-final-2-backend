"""El siniestro paramétrico contra PostgreSQL real (HU-W22).

MT-W22-01 (lado `siniestros`): la misma solicitud, 20 a la vez, deja **un** siniestro, **un**
`SiniestroAprobado` y **un** historial; dos eventos distintos no repiten código; el índice único
evento-póliza es la segunda barrera aunque la clave de idempotencia se haya purgado.
"""

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from siniestros.adapters.outbound.codigos import CodigosPg
from siniestros.adapters.outbound.siniestros_pg import SiniestrosPg
from siniestros.application.siniestro.crear_parametrico import CrearParametrico
from siniestros.application.siniestro.errores import IdempotenciaConflicto
from siniestros.application.siniestro.pagos_parametricos import ConsultarPagosParametricos
from siniestros.application.siniestro.vistas import Plazos
from siniestros.domain.siniestro.modelos import EstadoSiniestro
from siniestros.domain.siniestro.pago import EstadoPago
from siniestros.domain.siniestro.parametrico import DatosParametrico, clave_idempotente

pytestmark = pytest.mark.anyio
AHORA = datetime(2026, 11, 10, 14, 5, tzinfo=UTC)
EVENTOS = Path(__file__).parents[4] / "contracts" / "events"
PLAZOS = Plazos(48, 480, 1440)
ANALISTA = "a0a0a0a0-0000-4000-8000-000000000001"


class Reloj:
    def ahora(self) -> datetime:
        return AHORA


def crear_caso(pool: Any) -> CrearParametrico:
    return CrearParametrico(SiniestrosPg(pool), CodigosPg(pool), Reloj(), PLAZOS)


def contar(conexion: psycopg.Connection[Any], tabla: str, donde: str = "true") -> int:
    fila = conexion.execute(f"SELECT count(*) FROM {tabla} WHERE {donde}").fetchone()
    assert fila is not None
    return int(fila[0])


async def test_la_creacion_escribe_siniestro_historial_evento_y_clave_en_una_transaccion(
    pool: Any, conexion: psycopg.Connection[Any], datos_parametrico: Callable[..., DatosParametrico]
) -> None:
    datos = datos_parametrico()

    resultado = await crear_caso(pool).ejecutar(datos.clave, datos)

    assert resultado.creado is True
    s = resultado.resumen.siniestro
    fila = conexion.execute(
        "SELECT codigo, origen, estado, analista_id, valor_indemnizar, deducible, valor_reclamado, "
        "moneda, decidido_en, evento_id, regla, evento, copia_poliza->'beneficiario' "
        "FROM siniestros WHERE id = %s",
        (s.id,),
    ).fetchone()
    assert fila is not None
    (
        codigo,
        origen,
        estado,
        analista,
        valor,
        deducible,
        reclamado,
        moneda,
        decidido,
        evento_id,
        regla,
        medicion,
        beneficiario,
    ) = fila
    assert (codigo, origen, estado, analista) == ("88-2026-1000", "PARAMETRICO", "APROBADO", None)
    assert (valor, deducible, reclamado, moneda) == (
        Decimal("1400000.00"),
        Decimal("0.00"),
        None,
        "COP",
    )
    assert (decidido, evento_id) == (AHORA, datos.evento.evento_id)
    assert regla == {"codigo": "VALOR_IGUAL_O_SUPERIOR_AL_UMBRAL", "version": "1"}
    assert medicion["valorObservado"] == "135" and medicion["eventoOrigen"] == "EVT-2026-0913"
    assert beneficiario == {
        "beneficiarioRef": f"ben-par-{datos.cliente_id}",
        "cuentaMascarada": "****0000",
    }
    # El historial automático, sin actor ni nombre.
    (historial,) = conexion.execute(
        "SELECT accion, estado_anterior, estado_nuevo, actor_id, actor_rol, actor_nombre "
        "FROM historial_siniestro WHERE siniestro_id = %s",
        (s.id,),
    ).fetchall()
    assert historial == (
        "CREADO_AUTOMATICAMENTE",
        None,
        "APROBADO",
        None,
        "SISTEMA_PARAMETRICO",
        None,
    )
    # Un solo SiniestroAprobado en el Outbox, válido contra su esquema y sin quien decide.
    ((tipo, aggregate, payload),) = conexion.execute(
        "SELECT event_type, aggregate_id, payload FROM outbox"
    ).fetchall()
    assert (tipo, aggregate) == ("SiniestroAprobado", s.id)
    esquema = json.loads((EVENTOS / "SiniestroAprobado.v1.schema.json").read_text(encoding="utf-8"))
    sobre = {
        "eventId": str(uuid4()),
        "aggregateId": str(s.id),
        "type": tipo,
        "version": 1,
        "occurredAt": AHORA.isoformat(),
        "correlationId": "corr-w22",
        "payload": payload,
    }
    Draft202012Validator(esquema).validate(sobre)
    assert payload["origen"] == "PARAMETRICO" and payload["decididoPor"] is None
    assert payload["referenciaPago"] == "IND-88-2026-1000-01"
    assert payload["valorIndemnizar"] == "1400000.00"
    assert payload["beneficiarioRef"] == f"ben-par-{datos.cliente_id}"
    assert contar(conexion, "idempotencia", f"clave = 'parametrico:{datos.clave}'") == 1


async def test_veinte_solicitudes_a_la_vez_dejan_un_solo_siniestro_y_un_solo_evento(
    pool: Any, conexion: psycopg.Connection[Any], datos_parametrico: Callable[..., DatosParametrico]
) -> None:
    datos = datos_parametrico()

    resultados = await asyncio.gather(
        *(crear_caso(pool).ejecutar(datos.clave, datos) for _ in range(20))
    )

    assert sum(r.creado for r in resultados) == 1
    assert len({r.resumen.siniestro.id for r in resultados}) == 1
    assert contar(conexion, "siniestros") == 1
    assert contar(conexion, "historial_siniestro", "accion = 'CREADO_AUTOMATICAMENTE'") == 1
    assert contar(conexion, "outbox", "event_type = 'SiniestroAprobado'") == 1
    assert contar(conexion, "idempotencia") == 1


async def test_dos_eventos_distintos_a_la_vez_no_repiten_el_codigo(
    pool: Any, conexion: psycopg.Connection[Any], datos_parametrico: Callable[..., DatosParametrico]
) -> None:
    pares = [datos_parametrico(poliza_id=uuid4()) for _ in range(12)]

    resultados = await asyncio.gather(*(crear_caso(pool).ejecutar(d.clave, d) for d in pares))

    codigos = {r.resumen.siniestro.codigo for r in resultados}
    assert len(codigos) == 12 and all(r.creado for r in resultados)
    assert contar(conexion, "siniestros") == 12 and contar(conexion, "outbox") == 12


async def test_el_indice_unico_cubre_la_clave_purgada(
    pool: Any, conexion: psycopg.Connection[Any], datos_parametrico: Callable[..., DatosParametrico]
) -> None:
    datos = datos_parametrico()
    primero = await crear_caso(pool).ejecutar(datos.clave, datos)
    conexion.execute("DELETE FROM idempotencia")  # la clave se purgó: queda el índice único

    otra = await crear_caso(pool).ejecutar(datos.clave, datos)

    assert (otra.creado, otra.resumen.siniestro.id) == (False, primero.resumen.siniestro.id)
    assert contar(conexion, "siniestros") == 1 and contar(conexion, "outbox") == 1
    assert contar(conexion, "idempotencia") == 1  # se volvió a guardar


async def test_insertar_parametrico_dice_si_lo_inserto_sin_lanzar(
    pool: Any, conexion: psycopg.Connection[Any], datos_parametrico: Callable[..., DatosParametrico]
) -> None:
    from siniestros.domain.siniestro.parametrico import nuevo_siniestro

    datos = datos_parametrico()
    uno = nuevo_siniestro(
        datos, siniestro_id=uuid4(), codigo="88-2026-2001", ahora=AHORA, correlation_id="c"
    )
    otro = nuevo_siniestro(
        datos, siniestro_id=uuid4(), codigo="88-2026-2002", ahora=AHORA, correlation_id="c"
    )

    async with SiniestrosPg(pool).transaccion() as tx:
        assert await tx.insertar_parametrico(uno) is True  # type: ignore[attr-defined]
        assert await tx.insertar_parametrico(otro) is False  # type: ignore[attr-defined]
        leido = await tx.siniestro_parametrico(datos.evento.evento_id, datos.poliza_id)  # type: ignore[attr-defined]

    assert leido == uno  # ida y vuelta sin pérdidas: la fila del paramétrico se reconstruye igual
    assert contar(conexion, "siniestros") == 1


async def test_la_misma_clave_con_otro_cuerpo_es_un_conflicto(
    pool: Any, conexion: psycopg.Connection[Any], datos_parametrico: Callable[..., DatosParametrico]
) -> None:
    datos = datos_parametrico()
    await crear_caso(pool).ejecutar(datos.clave, datos)

    with pytest.raises(IdempotenciaConflicto):
        await crear_caso(pool).ejecutar(datos.clave, datos_parametrico(valor=Decimal("620000.00")))

    assert contar(conexion, "siniestros") == 1


async def test_el_detalle_real_trae_la_liquidacion_el_evento_y_el_pago_pendiente(
    pool: Any, datos_parametrico: Callable[..., DatosParametrico]
) -> None:
    datos = datos_parametrico()
    creado = (await crear_caso(pool).ejecutar(datos.clave, datos)).resumen.siniestro

    leido = await SiniestrosPg(pool).detalle(creado.id)

    assert leido is not None
    assert leido.siniestro.evento == datos.evento
    liquidacion = leido.liquidacion
    assert liquidacion is not None
    assert (liquidacion.valor_indemnizar, liquidacion.deducible, liquidacion.valor_reclamado) == (
        Decimal("1400000.00"),
        Decimal("0.00"),
        None,
    )
    assert liquidacion.referencia_pago == "IND-88-2026-1000-01"
    assert liquidacion.aprobada_en == AHORA
    assert leido.pago is None  # `dinero` aún no informó: el detalle deriva PENDIENTE
    assert [h.accion for h in leido.historial] == ["CREADO_AUTOMATICAMENTE"]


async def test_la_lectura_por_lote_deriva_el_pendiente_y_ve_el_resultado_de_dinero(
    pool: Any, conexion: psycopg.Connection[Any], datos_parametrico: Callable[..., DatosParametrico]
) -> None:
    uno = datos_parametrico()
    otro = datos_parametrico(poliza_id=uuid4())
    a = (await crear_caso(pool).ejecutar(uno.clave, uno)).resumen.siniestro
    b = (await crear_caso(pool).ejecutar(otro.clave, otro)).resumen.siniestro
    conexion.execute(
        "UPDATE siniestros SET pago_estado = 'CONFIRMADO', pago_referencia = %s WHERE id = %s",
        (f"IND-{b.codigo}-01", b.id),
    )

    pagos = await ConsultarPagosParametricos(SiniestrosPg(pool)).ejecutar([a.id, b.id, uuid4()])

    assert {(p.siniestro_id, p.estado_pago) for p in pagos} == {
        (a.id, EstadoPago.PENDIENTE),
        (b.id, EstadoPago.CONFIRMADO),
    }


async def test_un_paramétrico_no_aparece_en_la_cola_sin_asignar_ni_en_mi_bandeja(
    pool: Any, datos_parametrico: Callable[..., DatosParametrico]
) -> None:
    from siniestros.application.siniestro.vistas import FiltroBandeja, TipoBandeja

    datos = datos_parametrico()
    await crear_caso(pool).ejecutar(datos.clave, datos)
    repo = SiniestrosPg(pool)

    for bandeja in (TipoBandeja.SIN_ASIGNAR, TipoBandeja.MIA):
        pagina = await repo.bandeja(
            FiltroBandeja(bandeja, UUID(ANALISTA), AHORA, PLAZOS, limite=20)
        )
        assert pagina.items == () and pagina.total == 0
    asignado = await repo.bandeja(
        FiltroBandeja(
            TipoBandeja.SIN_ASIGNAR, UUID(ANALISTA), AHORA, PLAZOS, estado=EstadoSiniestro.APROBADO
        )
    )
    assert asignado.total == 0  # nace APROBADO: nunca REPORTADO


def test_la_app_crea_una_vez_y_lee_el_evento_en_el_detalle(
    limpia: str,
    monkeypatch: pytest.MonkeyPatch,
    datos_parametrico: Callable[..., DatosParametrico],
) -> None:
    """La app completa sobre PostgreSQL real: POST dos veces (201 y 200) y el detalle del analista."""
    from siniestros.config import Settings
    from siniestros.main import crear_app

    datos = datos_parametrico()
    cuerpo = {
        "eventoId": str(datos.evento.evento_id),
        "polizaId": str(datos.poliza_id),
        "clienteId": str(datos.cliente_id),
        "poliza": {
            "numero": datos.numero,
            "ramo": datos.ramo,
            "vigencia": {"desde": "2026-09-01", "hasta": "2027-08-31"},
            "moneda": "COP",
        },
        "evento": {
            "fuente": "IDEAM-SIM",
            "tipoEvento": "LLUVIA_ACUMULADA",
            "valorObservado": "135",
            "umbral": "120",
            "unidad": "mm",
            "zona": "11001",
            "observadoEn": "2026-11-10T14:00:00Z",
            "eventoOrigen": "EVT-2026-0913",
        },
        "regla": "VALOR_IGUAL_O_SUPERIOR_AL_UMBRAL",
        "versionRegla": "1",
        "valor": "1400000.00",
        "moneda": "COP",
    }
    sistema = {
        "X-Actor-Id": "pagos-parametricos",
        "X-Actor-Rol": "SISTEMA_PARAMETRICO",
        "Idempotency-Key": datos.clave,
    }
    analista = {"X-Actor-Id": ANALISTA, "X-Actor-Rol": "ANALISTA_SINIESTROS"}
    monkeypatch.setenv("SOLVENTA_DATABASE_URL", limpia)

    with TestClient(crear_app(Settings())) as cliente:
        primero = cliente.post("/v1/siniestros/parametricos", json=cuerpo, headers=sistema)
        segundo = cliente.post("/v1/siniestros/parametricos", json=cuerpo, headers=sistema)
        detalle = cliente.get(f"/v1/siniestros/{primero.json()['id']}", headers=analista)
        pagos = cliente.get(
            "/v1/siniestros/parametricos/pagos",
            params={"siniestroIds": [primero.json()["id"]]},
            headers=analista,
        )

    assert (primero.status_code, segundo.status_code) == (201, 200)
    assert segundo.json() == primero.json()
    cuerpo_detalle = detalle.json()
    assert cuerpo_detalle["evento"]["regla"] == "VALOR_IGUAL_O_SUPERIOR_AL_UMBRAL"
    assert cuerpo_detalle["liquidacion"]["valorIndemnizar"] == "1400000.00"
    assert cuerpo_detalle["liquidacion"]["deducible"] == "0.00"
    assert cuerpo_detalle["pago"]["estado"] == "PENDIENTE"
    assert pagos.json()["items"][0]["estadoPago"] == "PENDIENTE"
    assert clave_idempotente(datos.evento.evento_id, datos.poliza_id) == datos.clave
