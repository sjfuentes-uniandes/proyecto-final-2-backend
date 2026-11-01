"""MT-W17-06 (17.6): la póliza EMITIENDO en PostgreSQL real, y su creación desde el recaudo.

- La migración 004 hacia adelante, hacia atrás y otra vez hacia adelante; cada CHECK, la llave
  hacia `aceptaciones`, los índices únicos, el disparador y la secuencia de números.
- `PolizasPg` en una transacción recibida: crear, leer y guardar las transiciones con la guarda.
- Desde `polizas-recaudos` (LocalStack): el primer `RecaudoConfirmado` crea una sola póliza, aunque
  llegue otro con distinto `eventId`; `RecaudoRechazado` y una clave desconocida no crean ninguna.
- Dos `RecaudoConfirmado` concurrentes (dos transacciones abiertas a la vez) crean una sola, y una
  transacción que hace rollback no deja nada.
"""

import asyncio
import itertools
import json
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from importlib import resources
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from jsonschema import Draft202012Validator
from polizas.adapters.inbound.sqs.recaudos import manejador_recaudos
from polizas.adapters.outbound.decisiones_pg import migrar
from polizas.adapters.outbound.polizas_pg import COLUMNAS, PolizasPg, fila_poliza
from polizas.adapters.outbound.recaudos_pg import RecaudosPg
from polizas.application.recaudo.proyectar import ProyectarRecaudo
from polizas.domain.poliza.modelos import (
    Documento,
    Firma,
    OrigenPoliza,
    Poliza,
    Producto,
    Vigencia,
)
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from solventa_common.inbox import InboxPg, MensajeSqs, TransaccionInboxPg, leer_sobre

docker = pytest.importorskip("docker")
pytestmark = pytest.mark.anyio
MIGRACIONES = resources.files("polizas.adapters.outbound").joinpath("migraciones")
EVENTOS = Path(__file__).parents[4] / "contracts/events"
ASESOR = {"X-Actor-Id": "sub-asesor-1", "X-Actor-Rol": "ASESOR"}
TODAS = {
    "leidoCondiciones": True,
    "informacionVeraz": True,
    "debitoAutomatico": True,
    "avisosElectronicos": True,
}
RESUELTO_EN = "2026-10-22T15:00:01.250000Z"
CREADA = datetime(2026, 10, 22, 15, 0, tzinfo=UTC)
NUMERO = re.compile(r"^100-2026-[0-9]{6}$")
_SECUENCIAS = itertools.count(900_000)
DOC = {
    "documento_clave": "polizas/100-2026-900000.pdf",
    "documento_version_id": "v-1",
    "documento_huella": "sha256:" + "d" * 64,
    "documento_tamano_bytes": 2048,
    "documento_fecha": CREADA,
}
FIRMA = {"firma_id": "FIR-SINT-C01", "firma_sello_tiempo": datetime(2026, 1, 1, tzinfo=UTC)}
ACTIVA = {"estado": "ACTIVA", "emitida_en": CREADA}
_INSERTAR = (
    f"INSERT INTO polizas ({', '.join(COLUMNAS)}) VALUES ({', '.join(['%s'] * len(COLUMNAS))})"
)


def _filas(url: str, consulta: str, parametros: Any = None) -> list[tuple[Any, ...]]:
    with psycopg.connect(url, autocommit=True) as conexion:
        return conexion.execute(consulta, parametros).fetchall()


def _ejecutar(url: str, sentencia: str, parametros: Any = None) -> None:
    with psycopg.connect(url, autocommit=True) as conexion:
        conexion.execute(sentencia, parametros)


def _sobre(tipo: str, clave: str) -> dict[str, Any]:
    """Un sobre que cumple el esquema publicado por dinero (se comprueba aquí mismo)."""
    recaudo_id = str(uuid4())
    datos: dict[str, Any] = {
        "recaudoId": recaudo_id,
        "claveRecaudo": clave,
        "cotizacionId": str(uuid4()),
    }
    if tipo == "RecaudoConfirmado":
        datos |= {
            "monto": {"valor": "64638.10", "moneda": "COP"},
            "transaccionId": "TX-SINTETICA-0001",
            "confirmadoEn": RESUELTO_EN,
        }
    else:
        datos |= {"motivo": "FONDOS_INSUFICIENTES", "rechazadoEn": RESUELTO_EN}
    sobre = {
        "eventId": str(uuid4()),
        "aggregateId": recaudo_id,
        "type": tipo,
        "version": 1,
        "occurredAt": RESUELTO_EN,
        "correlationId": "corr-poliza-modulo",
        "payload": datos,
    }
    esquema = json.loads((EVENTOS / f"{tipo}.v1.schema.json").read_text(encoding="utf-8"))
    assert list(Draft202012Validator(esquema).iter_errors(sobre)) == []
    return sobre


async def _aceptar(api: Any, sinteticos: Any) -> UUID:
    """Suscripción y aceptación de S01 por la API de polizas; devuelve la clave (= su id)."""
    decision = await api.post(
        "/v1/suscripciones",
        json={"cotizacionId": sinteticos.cotizacion_id("S01")},
        headers={**ASESOR, "Idempotency-Key": str(uuid4())},
    )
    assert decision.status_code == 201, decision.json()
    cuerpo = decision.json()
    aceptacion = await api.post(
        f"/v1/cotizaciones/{cuerpo['cotizacionId']}/aceptacion",
        json={
            "decisionId": cuerpo["id"],
            "versionDeclaraciones": "2026.10.1",
            "declaraciones": TODAS,
        },
        headers={**ASESOR, "Idempotency-Key": str(uuid4())},
    )
    assert aceptacion.status_code == 201, aceptacion.json()
    return UUID(aceptacion.json()["id"])


def _copiar_aceptacion(url: str, aceptacion_id: UUID) -> UUID:
    """Otra aceptación conocida (solo S01 sale APROBADA): copia la decisión y la aceptación con
    identificadores nuevos."""
    nueva_decision, nueva = uuid4(), uuid4()
    with psycopg.connect(url, autocommit=True) as conexion:
        cursor = conexion.cursor(row_factory=dict_row)
        aceptacion = cursor.execute(
            "SELECT * FROM aceptaciones WHERE id = %s", (aceptacion_id,)
        ).fetchone()
        assert aceptacion is not None
        decision = cursor.execute(
            "SELECT * FROM decisiones_suscripcion WHERE id = %s", (aceptacion["decision_id"],)
        ).fetchone()
        assert decision is not None
        decision |= {
            "id": nueva_decision,
            "codigo": "DEC-2026-999999",
            "cotizacion_id": uuid4(),
            "idempotency_key": uuid4(),
        }
        aceptacion |= {
            "id": nueva,
            "cotizacion_id": decision["cotizacion_id"],
            "decision_id": nueva_decision,
            "idempotency_key": uuid4(),
        }
        for tabla, fila in (("decisiones_suscripcion", decision), ("aceptaciones", aceptacion)):
            valores = [Jsonb(v) if isinstance(v, dict | list) else v for v in fila.values()]
            conexion.execute(
                f"INSERT INTO {tabla} ({', '.join(fila)}) VALUES ({', '.join(['%s'] * len(fila))})",
                valores,
            )
    return nueva


def _fila(aceptacion_id: UUID, **cambios: Any) -> dict[str, Any]:
    """Una fila válida (EMITIENDO) con su número propio, más los cambios."""
    origen = OrigenPoliza(
        aceptacion_id=aceptacion_id,
        cotizacion_id=uuid4(),
        cliente_id=uuid4(),
        producto=Producto("vida-hipotecario", "2026.09.1"),
        aceptada_en=CREADA,
    )
    poliza = Poliza.emitiendo(origen, secuencia=next(_SECUENCIAS), ahora=CREADA)
    return fila_poliza(poliza) | cambios


def _insertar(url: str, fila: dict[str, Any]) -> None:
    _ejecutar(url, _INSERTAR, [fila[c] for c in COLUMNAS])


def _polizas(url: str) -> list[tuple[Any, ...]]:
    return _filas(
        url,
        "SELECT aceptacion_id, estado, numero, vigencia_desde, vigencia_hasta, producto_id, "
        "producto_version, ramo_codigo, ramo_nombre, documento_clave, firma_id, emitida_en "
        "FROM polizas",
    )


async def _esperar(condicion: Callable[[], Awaitable[bool]], limite_s: float = 20) -> None:
    fin = time.monotonic() + limite_s
    while not await condicion():
        assert time.monotonic() < fin, "la condición no se cumplió a tiempo"
        await asyncio.sleep(0.2)


async def _sin_pendientes(cola: Any) -> bool:
    return await asyncio.to_thread(cola.pendientes, cola.cola) == 0


# --- Esquema: CHECK, llave, únicos y disparador ----------------------------------------------
INVALIDAS: dict[str, dict[str, Any]] = {
    "estado_desconocido": {"estado": "PENDIENTE"},
    "vigencia_sin_avance": {
        "vigencia_desde": date(2026, 10, 23),
        "vigencia_hasta": date(2026, 10, 23),
    },
    "vigencia_al_reves": {"vigencia_desde": date(2026, 10, 23), "vigencia_hasta": date(2026, 1, 1)},
    "fallo_no_es_codigo": {"ultimo_fallo": "se cayó la firma"},
    "numero_mal_formado": {"numero": "100-2026-1"},
    "numero_de_otro_ramo": {"numero": "200-2026-000001"},
    "producto_vacio": {"producto_id": " "},
    "documento_parcial": {"documento_clave": "polizas/x.pdf"},
    "documento_sin_fecha": {**DOC, "documento_fecha": None},
    "huella_mal_formada": {**DOC, "documento_huella": "sha256:XYZ"},
    "tamano_cero": {**DOC, "documento_tamano_bytes": 0},
    "firma_sin_documento": FIRMA,
    "firma_parcial": {**DOC, "firma_id": "FIR-1"},
    "activa_sin_documento": {**FIRMA, **ACTIVA},
    "activa_sin_firma": {**DOC, **ACTIVA},
    "activa_sin_emitida_en": {**DOC, **FIRMA, "estado": "ACTIVA"},
    "emitiendo_con_emitida_en": {"emitida_en": CREADA},
}


async def test_cada_check_rechaza_su_forma_invalida(api: Any, sinteticos: Any, limpia: str) -> None:
    clave = await _aceptar(api, sinteticos)
    for nombre, cambios in INVALIDAS.items():
        with pytest.raises(psycopg.errors.CheckViolation):
            _insertar(limpia, _fila(clave, **cambios))
        assert _polizas(limpia) == [], nombre
    # Completa y ACTIVA: válida.
    _insertar(limpia, _fila(clave, **DOC, **FIRMA, **ACTIVA))
    assert [(a, e) for a, e, *_ in _polizas(limpia)] == [(clave, "ACTIVA")]


async def test_solo_para_una_aceptacion_conocida(limpia: str) -> None:
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _insertar(limpia, _fila(uuid4()))
    assert _polizas(limpia) == []


async def test_una_poliza_por_aceptacion_y_un_numero_por_poliza(
    api: Any, sinteticos: Any, limpia: str
) -> None:
    clave = await _aceptar(api, sinteticos)
    primera = _fila(clave)
    _insertar(limpia, primera)
    with pytest.raises(psycopg.errors.UniqueViolation):
        _insertar(limpia, _fila(clave))
    otra = _copiar_aceptacion(limpia, clave)
    with pytest.raises(psycopg.errors.UniqueViolation):
        _insertar(limpia, _fila(otra, numero=primera["numero"]))
    _insertar(limpia, _fila(otra))
    assert len(_polizas(limpia)) == 2


async def test_el_disparador_protege_la_identidad_y_los_puntos_de_control(
    api: Any, sinteticos: Any, limpia: str
) -> None:
    clave = await _aceptar(api, sinteticos)
    _insertar(limpia, _fila(clave))
    actualizar = "UPDATE polizas SET {} WHERE aceptacion_id = %s"
    with pytest.raises(psycopg.errors.RaiseException, match="no admite DELETE"):
        _ejecutar(limpia, "DELETE FROM polizas")
    identidad = [
        "numero = '100-2026-000001'",
        "cotizacion_id = gen_random_uuid()",
        "cliente_id = gen_random_uuid()",
        "producto_version = '2027.01.1'",
        "ramo_nombre = 'Otro'",
        "vigencia_hasta = vigencia_hasta + 1",
        "creada_en = now()",
        "poliza_id = gen_random_uuid()",
    ]
    for cambio in identidad:
        with pytest.raises(psycopg.errors.RaiseException, match="identidad"):
            _ejecutar(limpia, actualizar.format(cambio), (clave,))
    # Cambian el fallo y la marca de tiempo mientras está EMITIENDO.
    _ejecutar(
        limpia,
        actualizar.format("ultimo_fallo = 'TIEMPO_AGOTADO', actualizada_en = now()"),
        (clave,),
    )
    # El documento se registra una vez y ya no cambia.
    asignar = ", ".join(f"{c} = %s" for c in DOC)
    _ejecutar(limpia, actualizar.format(asignar), [*DOC.values(), clave])
    for cambio in ("documento_version_id = 'v-2'", "documento_tamano_bytes = 1"):
        with pytest.raises(psycopg.errors.RaiseException, match="documento"):
            _ejecutar(limpia, actualizar.format(cambio), (clave,))
    # La firma también.
    asignar = ", ".join(f"{c} = %s" for c in FIRMA)
    _ejecutar(limpia, actualizar.format(asignar), [*FIRMA.values(), clave])
    with pytest.raises(psycopg.errors.RaiseException, match="firma"):
        _ejecutar(limpia, actualizar.format("firma_id = 'FIR-2'"), (clave,))
    # ACTIVA es final: ni siquiera el fallo o la marca de tiempo cambian.
    _ejecutar(limpia, actualizar.format("estado = 'ACTIVA', emitida_en = now()"), (clave,))
    for cambio in ("ultimo_fallo = NULL", "actualizada_en = now()", "estado = 'EMITIENDO'"):
        with pytest.raises(psycopg.errors.RaiseException, match="ACTIVA"):
            _ejecutar(limpia, actualizar.format(cambio), (clave,))
    with pytest.raises(psycopg.errors.RaiseException, match="no admite DELETE"):
        _ejecutar(limpia, "DELETE FROM polizas")
    [(_, estado, *_resto)] = _polizas(limpia)
    assert estado == "ACTIVA"


async def test_la_secuencia_no_vuelve_atras_con_un_rollback(limpia: str) -> None:
    repo = PolizasPg()
    async with await psycopg.AsyncConnection.connect(limpia) as conexion:
        tx = TransaccionInboxPg(conexion, sql.Identifier("inbox"))
        async with conexion.transaction(force_rollback=True):
            primero = await repo.siguiente_secuencia(tx)
        async with conexion.transaction():
            segundo = await repo.siguiente_secuencia(tx)
    # Un número reservado que no se usa deja un hueco (se acepta).
    assert segundo == primero + 1


# --- El repositorio en una transacción recibida ----------------------------------------------
async def test_el_repositorio_crea_lee_y_guarda_las_transiciones(
    api: Any, sinteticos: Any, limpia: str
) -> None:
    clave = await _aceptar(api, sinteticos)
    [(cotizacion_id, cliente_id, aceptada_en)] = _filas(
        limpia, "SELECT cotizacion_id, cliente_id, aceptada_en FROM aceptaciones"
    )
    repo = PolizasPg()
    documento = Documento(
        clave="polizas/doc.pdf",
        version_id="v-1",
        huella=DOC["documento_huella"],
        tamano_bytes=2048,
        fecha_documento=CREADA,
    )
    firma = Firma("FIR-SINT-C01", datetime(2026, 1, 1, tzinfo=UTC))
    async with await psycopg.AsyncConnection.connect(limpia) as conexion:
        tx = TransaccionInboxPg(conexion, sql.Identifier("inbox"))
        async with conexion.transaction():
            origen = await repo.origen(clave, tx)
            assert origen == OrigenPoliza(
                clave,
                cotizacion_id,
                cliente_id,
                Producto("vida-hipotecario", "2026.09.1"),
                aceptada_en,
            )
            assert await repo.origen(uuid4(), tx) is None
            poliza = Poliza.emitiendo(
                origen, secuencia=await repo.siguiente_secuencia(tx), ahora=CREADA
            )
            assert await repo.crear(poliza, tx) is True
            otra = Poliza.emitiendo(
                origen, secuencia=await repo.siguiente_secuencia(tx), ahora=CREADA
            )
            assert await repo.crear(otra, tx) is False
            assert await repo.por_aceptacion(clave, tx) == poliza
            assert await repo.por_aceptacion(uuid4(), tx) is None
        async with conexion.transaction():
            firmada = poliza.registrar_documento(documento, CREADA).registrar_firma(
                firma, documento.huella, CREADA
            )
            assert await repo.actualizar(firmada.registrar_fallo("TIEMPO_AGOTADO", CREADA), tx)
            activa = firmada.activar(CREADA)
            assert await repo.actualizar(activa, tx) is True
            # Guarda: una copia vieja (otra tarea) no cambia nada ni choca con el disparador.
            assert await repo.actualizar(firmada, tx) is False
            assert await repo.por_aceptacion(clave, tx) == activa


# --- Desde polizas-recaudos ------------------------------------------------------------------
async def test_el_primer_confirmado_crea_una_sola_poliza_emitiendo(
    abrir_api_con_consumidor: Any, cola_recaudos: Any, sinteticos: Any, limpia: str
) -> None:
    async with abrir_api_con_consumidor() as api:
        clave = await _aceptar(api, sinteticos)
        # Dos RecaudoConfirmado de la misma clave con distinto eventId.
        await asyncio.to_thread(cola_recaudos.publicar, _sobre("RecaudoConfirmado", str(clave)))
        await asyncio.to_thread(cola_recaudos.publicar, _sobre("RecaudoConfirmado", str(clave)))

        async def consumidos() -> bool:
            return _filas(limpia, "SELECT count(*) FROM inbox") == [(2,)]

        await _esperar(consumidos)
        await _esperar(lambda: _sin_pendientes(cola_recaudos))

    [poliza] = _polizas(limpia)
    aceptacion, estado, numero, desde, hasta, producto, version, ramo, nombre, *control = poliza
    assert (aceptacion, estado) == (clave, "EMITIENDO")
    assert NUMERO.fullmatch(numero), numero
    # Aceptada el 22/10/2026 a las 10:00 de Bogotá (reloj de los dobles).
    assert Vigencia(desde, hasta) == Vigencia(date(2026, 10, 23), date(2027, 10, 22))
    assert (producto, version, ramo, nombre) == (
        "vida-hipotecario",
        "2026.09.1",
        "100",
        "Vida hipotecario",
    )
    assert control == [None, None, None]
    assert _filas(limpia, "SELECT count(*) FROM inbox") == [(2,)]
    assert _filas(limpia, "SELECT count(*) FROM recaudos_aceptacion") == [(1,)]
    assert await asyncio.to_thread(cola_recaudos.pendientes, cola_recaudos.dlq) == 0


@pytest.mark.parametrize("conocida", [True, False], ids=["rechazado", "clave_desconocida"])
async def test_sin_confirmado_de_una_aceptacion_conocida_no_hay_poliza(
    abrir_api_con_consumidor: Any,
    cola_recaudos: Any,
    sinteticos: Any,
    limpia: str,
    conocida: bool,
) -> None:
    async with abrir_api_con_consumidor() as api:
        if conocida:
            sobre = _sobre("RecaudoRechazado", str(await _aceptar(api, sinteticos)))
        else:
            sobre = _sobre("RecaudoConfirmado", str(uuid4()))
        await asyncio.to_thread(cola_recaudos.publicar, sobre)

        async def proyectada() -> bool:
            return _filas(limpia, "SELECT count(*) FROM recaudos_aceptacion") == [(1,)]

        await _esperar(proyectada)
        await _esperar(lambda: _sin_pendientes(cola_recaudos))

    assert _polizas(limpia) == []
    assert await asyncio.to_thread(cola_recaudos.pendientes, cola_recaudos.dlq) == 0


# --- Concurrencia y rollback, con el inbox y el caso de uso de producción ---------------------
def _evento(sobre: dict[str, Any]) -> Any:
    return leer_sobre(MensajeSqs("m-1", "r-1", json.dumps(sobre), sobre["type"], 1))


async def test_dos_confirmados_concurrentes_crean_una_sola_poliza(
    api: Any, sinteticos: Any, limpia: str, dobles: Any
) -> None:
    from psycopg_pool import AsyncConnectionPool

    clave = str(await _aceptar(api, sinteticos))
    primero, segundo = (_evento(_sobre("RecaudoConfirmado", clave)) for _ in range(2))
    async with AsyncConnectionPool(limpia, min_size=2, max_size=4, open=False) as pool:
        inbox = InboxPg(pool)
        manejar = manejador_recaudos(ProyectarRecaudo(RecaudosPg(pool), PolizasPg(), dobles.reloj))

        async def procesar(evento: Any) -> None:
            async with inbox.transaccion() as tx:
                assert await tx.registrar(evento.event_id, evento.event_type, CREADA)
                await manejar(evento, tx)

        async with inbox.transaccion() as tx:
            assert await tx.registrar(primero.event_id, primero.event_type, CREADA)
            await manejar(primero, tx)
            # Con la primera abierta, la segunda espera en el índice único de la proyección.
            otra = asyncio.create_task(procesar(segundo))
            await asyncio.sleep(1)
            assert not otra.done()
        await otra

    [(aceptacion, estado, *_)] = _polizas(limpia)
    assert (str(aceptacion), estado) == (clave, "EMITIENDO")
    assert _filas(limpia, "SELECT count(*) FROM inbox") == [(2,)]


async def test_una_transaccion_que_hace_rollback_no_deja_nada(
    api: Any, sinteticos: Any, limpia: str, dobles: Any
) -> None:
    from psycopg_pool import AsyncConnectionPool

    clave = str(await _aceptar(api, sinteticos))
    evento = _evento(_sobre("RecaudoConfirmado", clave))

    class Interrumpida(Exception):
        pass

    async with AsyncConnectionPool(limpia, min_size=1, max_size=2, open=False) as pool:
        inbox = InboxPg(pool)
        manejar = manejador_recaudos(ProyectarRecaudo(RecaudosPg(pool), PolizasPg(), dobles.reloj))
        with pytest.raises(Interrumpida):
            async with inbox.transaccion() as tx:
                assert await tx.registrar(evento.event_id, evento.event_type, CREADA)
                await manejar(evento, tx)
                raise Interrumpida

    assert _polizas(limpia) == []
    assert _filas(limpia, "SELECT count(*) FROM recaudos_aceptacion") == [(0,)]
    assert _filas(limpia, "SELECT count(*) FROM inbox") == [(0,)]


# --- Migración --------------------------------------------------------------------------------
async def test_migracion_004_adelante_atras_y_adelante(base: str) -> None:
    """Revertir 004 (dos veces) deja las aceptaciones y el recaudo; volver a aplicar recrea la
    tabla, la secuencia, la llave y el disparador."""
    reversa = MIGRACIONES.joinpath("down", "004_polizas.sql").read_text(encoding="utf-8")
    with psycopg.connect(base, autocommit=True) as conexion:
        conexion.execute(reversa)
        conexion.execute(reversa)
    existe = "SELECT to_regclass('{}') IS NOT NULL"
    funcion = "SELECT count(*) FROM pg_proc WHERE proname = 'polizas_protegidas'"
    assert _filas(base, existe.format("polizas")) == [(False,)]
    assert _filas(base, existe.format("polizas_numero")) == [(False,)]
    assert _filas(base, funcion) == [(0,)]
    for tabla in ("aceptaciones", "recaudos_aceptacion", "inbox"):
        assert _filas(base, existe.format(tabla)) == [(True,)]
    migrar(base)
    migrar(base)
    assert _filas(base, existe.format("polizas")) == [(True,)]
    assert _filas(base, existe.format("polizas_numero")) == [(True,)]
    assert _filas(base, funcion) == [(1,)]
    disparador = "SELECT count(*) FROM pg_trigger WHERE tgname = 'polizas_protegidas'"
    assert _filas(base, disparador) == [(1,)]
    llaves = (
        "SELECT confrelid::regclass::text FROM pg_constraint WHERE contype = 'f' "
        "AND conrelid = 'polizas'::regclass"
    )
    assert _filas(base, llaves) == [("aceptaciones",)]
