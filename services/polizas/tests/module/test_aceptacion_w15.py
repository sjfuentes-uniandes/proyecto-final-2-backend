"""MT-W15-01: la aceptación con PostgreSQL efímero (Testcontainers), con el arnés de R-60.

Cubre los criterios técnicos 1 a 6 de §2.3: aceptación y fila OfertaAceptada en el outbox en una
transacción (si falla el outbox no queda nada), vencida sin filas, idempotencia y 20 solicitudes
concurrentes (una sola aceptación y un solo evento), rechazos sin filas, inmutabilidad con la
huella recalculada desde la fila, logs sin PII y migración adelante y atrás.
"""

import asyncio
import json
from datetime import timedelta
from importlib import resources
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from jsonschema import Draft202012Validator
from polizas.adapters.outbound.aceptaciones_pg import aceptacion_de
from polizas.adapters.outbound.decisiones_pg import migrar
from polizas.domain.aceptacion.evidencia import huella_evidencia
from psycopg.rows import dict_row

docker = pytest.importorskip("docker")
pytestmark = pytest.mark.anyio
MIGRACIONES = resources.files("polizas.adapters.outbound").joinpath("migraciones")
ESQUEMA = json.loads(
    (Path(__file__).parents[4] / "contracts/events/OfertaAceptada.v1.schema.json").read_text(
        encoding="utf-8"
    )
)
ASESOR = {"X-Actor-Id": "sub-asesor-1", "X-Actor-Rol": "ASESOR"}
TODAS = {
    "leidoCondiciones": True,
    "informacionVeraz": True,
    "debitoAutomatico": True,
    "avisosElectronicos": True,
}


def _filas(url: str, sql: str, *parametros: Any) -> list[tuple[Any, ...]]:
    with psycopg.connect(url, autocommit=True) as conexion:
        return conexion.execute(sql, parametros).fetchall()


def _ejecutar(url: str, sql: str) -> None:
    with psycopg.connect(url, autocommit=True) as conexion:
        conexion.execute(sql)


def _contar(url: str) -> tuple[int, int]:
    """(aceptaciones, filas OfertaAceptada en el outbox)."""
    aceptaciones = _filas(url, "SELECT count(*) FROM aceptaciones")[0][0]
    eventos = _filas(url, "SELECT count(*) FROM outbox WHERE event_type = 'OfertaAceptada'")[0][0]
    return aceptaciones, eventos


async def _decidir(api: Any, sinteticos: Any, id_caso: str) -> dict[str, Any]:
    respuesta = await api.post(
        "/v1/suscripciones",
        json={"cotizacionId": sinteticos.cotizacion_id(id_caso)},
        headers={**ASESOR, "Idempotency-Key": str(uuid4())},
    )
    assert respuesta.status_code == 201, respuesta.json()
    decision: dict[str, Any] = respuesta.json()
    return decision


async def _aceptar(
    api: Any, decision: dict[str, Any], clave: str | None = None, **cambios: Any
) -> Any:
    return await api.post(
        f"/v1/cotizaciones/{decision['cotizacionId']}/aceptacion",
        json={
            "decisionId": decision["id"],
            "versionDeclaraciones": "2026.10.1",
            "declaraciones": TODAS,
            **cambios,
        },
        headers={**ASESOR, "Idempotency-Key": clave or str(uuid4())},
    )


async def test_aceptacion_y_evento_en_una_transaccion(
    api: Any, sinteticos: Any, limpia: str
) -> None:
    """Criterio 1 (AC1): 201, una aceptación y un OfertaAceptada con el mismo correlationId."""
    decision = await _decidir(api, sinteticos, "S01")
    respuesta = await _aceptar(api, decision)
    assert respuesta.status_code == 201, respuesta.json()
    cuerpo = respuesta.json()
    assert cuerpo["recaudoHabilitado"] is True
    correlacion = respuesta.headers["X-Correlation-Id"]
    assert _contar(limpia) == (1, 1)
    [(correlacion_fila,)] = _filas(limpia, "SELECT correlation_id FROM aceptaciones")
    [(correlacion_evento, agregado, payload)] = _filas(
        limpia,
        "SELECT correlation_id, aggregate_id, payload FROM outbox "
        "WHERE event_type = 'OfertaAceptada'",
    )
    assert correlacion_fila == correlacion_evento == correlacion
    assert str(agregado) == cuerpo["id"] == payload["claveRecaudo"]
    sobre = {
        "eventId": str(uuid4()),
        "aggregateId": str(agregado),
        "type": "OfertaAceptada",
        "version": 1,
        "occurredAt": payload["aceptadaEn"],
        "correlationId": correlacion,
        "payload": payload,
    }
    assert list(Draft202012Validator(ESQUEMA).iter_errors(sobre)) == []
    # La decisión también dejó su evento (R3: DecisionSuscripcionRegistrada + OfertaAceptada).
    tipos = _filas(limpia, "SELECT event_type FROM outbox ORDER BY occurred_at, event_type")
    assert sorted(t for (t,) in tipos) == ["DecisionSuscripcionRegistrada", "OfertaAceptada"]


async def test_si_falla_el_outbox_no_queda_nada(api: Any, sinteticos: Any, limpia: str) -> None:
    """La aceptación y su evento son una sola transacción."""
    decision = await _decidir(api, sinteticos, "S01")
    _ejecutar(
        limpia,
        "ALTER TABLE outbox ADD CONSTRAINT sin_aceptadas CHECK (event_type <> 'OfertaAceptada')",
    )
    # El transporte ASGI de httpx relanza la excepción; en el servicio, el manejador global
    # responde 500 ERROR_INTERNO.
    try:
        with pytest.raises(psycopg.errors.CheckViolation):
            await _aceptar(api, decision)
    finally:
        _ejecutar(limpia, "ALTER TABLE outbox DROP CONSTRAINT sin_aceptadas")
    assert _contar(limpia) == (0, 0)
    # Sin la falla, la misma clave sí registra.
    assert (await _aceptar(api, decision)).status_code == 201


@pytest.mark.parametrize("despues", [timedelta(0), timedelta(days=8)], ids=str)
async def test_oferta_vencida_sin_filas(
    api: Any, sinteticos: Any, dobles: Any, limpia: str, despues: timedelta
) -> None:
    """Criterio 2 (AC2), incluido el instante exacto de vigenteHasta."""
    decision = await _decidir(api, sinteticos, "S01")
    vigente_hasta = sinteticos.insumos("S01").vigente_hasta
    dobles.reloj.instante = vigente_hasta + despues
    respuesta = await _aceptar(api, decision)
    assert respuesta.status_code == 409
    assert respuesta.json()["detalles"] == {"accion": "NUEVA_COTIZACION"}
    assert respuesta.json()["codigo"] == "OFERTA_VENCIDA"
    assert _contar(limpia) == (0, 0)


async def test_idempotencia_y_veinte_solicitudes_concurrentes(
    api: Any, sinteticos: Any, limpia: str
) -> None:
    """Criterio 3 (AC3): misma clave → 200 igual; 20 concurrentes → una aceptación y un evento."""
    decision = await _decidir(api, sinteticos, "S01")
    clave = str(uuid4())
    otras = [str(uuid4()) for _ in range(10)]
    respuestas = await asyncio.gather(
        *(_aceptar(api, decision, clave) for _ in range(10)),
        *(_aceptar(api, decision, otra) for otra in otras),
    )
    assert _contar(limpia) == (1, 1)
    [(aceptacion_id, clave_ganadora)] = _filas(
        limpia, "SELECT id::text, idempotency_key::text FROM aceptaciones"
    )
    for respuesta in respuestas:
        if respuesta.status_code == 409:
            assert respuesta.json()["codigo"] == "OFERTA_YA_ACEPTADA"
            assert respuesta.json()["detalles"] == {"aceptacionId": aceptacion_id}
        else:
            assert respuesta.status_code in (200, 201), respuesta.json()
            assert respuesta.json()["id"] == aceptacion_id
    assert sum(r.status_code == 201 for r in respuestas) == 1
    # La clave ganadora nunca recibe 409 y cualquier otra clave, siempre.
    por_clave = [(clave, r) for r in respuestas[:10]] + list(zip(otras, respuestas[10:]))
    for clave_usada, respuesta in por_clave:
        assert (respuesta.status_code == 409) is (clave_usada != clave_ganadora)
    repetida = await _aceptar(api, decision, clave_ganadora)
    assert (repetida.status_code, repetida.json()["id"]) == (200, aceptacion_id)
    assert _contar(limpia) == (1, 1)


async def test_rechazos_sin_filas(api: Any, sinteticos: Any, limpia: str) -> None:
    """Criterio 4."""
    rechazada = await _decidir(api, sinteticos, "S02")
    revision = await _decidir(api, sinteticos, "S06")
    aprobada = await _decidir(api, sinteticos, "S01")
    casos = [
        (await _aceptar(api, rechazada), 409, "DECISION_NO_APROBADA"),
        (await _aceptar(api, revision), 409, "DECISION_NO_APROBADA"),
        (
            await _aceptar(api, aprobada, declaraciones={**TODAS, "informacionVeraz": False}),
            422,
            "DECLARACIONES_INCOMPLETAS",
        ),
        (
            await _aceptar(api, aprobada, versionDeclaraciones="2026.09.1"),
            422,
            "TEXTO_NO_VIGENTE",
        ),
        (await _aceptar(api, {**aprobada, "id": str(uuid4())}), 404, "DECISION_NO_ENCONTRADA"),
    ]
    for respuesta, estado, codigo in casos:
        assert (respuesta.status_code, respuesta.json()["codigo"]) == (estado, codigo)
    assert _contar(limpia) == (0, 0)


async def test_la_aceptacion_es_inmutable_y_su_huella_se_recalcula(
    api: Any, sinteticos: Any, limpia: str
) -> None:
    """Criterio 5."""
    decision = await _decidir(api, sinteticos, "S01")
    cuerpo = (await _aceptar(api, decision)).json()
    with pytest.raises(psycopg.errors.RaiseException, match="inmutable"):
        _ejecutar(limpia, "UPDATE aceptaciones SET canal = 'PORTAL_ASESOR'")
    with pytest.raises(psycopg.errors.RaiseException, match="inmutable"):
        _ejecutar(limpia, "DELETE FROM aceptaciones")
    with psycopg.connect(limpia, autocommit=True) as conexion:
        fila = (
            conexion.cursor(row_factory=dict_row).execute("SELECT * FROM aceptaciones").fetchone()
        )
    assert fila is not None
    assert (
        huella_evidencia(aceptacion_de(fila))
        == fila["huella_evidencia"]
        == cuerpo["huellaEvidencia"]
    )
    consulta = await api.get(
        f"/v1/cotizaciones/{decision['cotizacionId']}/aceptacion", headers=ASESOR
    )
    assert consulta.json() == cuerpo


async def test_los_logs_y_el_evento_no_tienen_pii(
    api: Any, sinteticos: Any, telemetria: Any, limpia: str
) -> None:
    """Criterio 6 (SEC-W15-01): ni montos, ni edad, ni declaraciones en los logs."""
    decision = await _decidir(api, sinteticos, "S01")
    await _aceptar(api, decision)
    texto = telemetria.texto_logs()
    assert "aceptacion registrada" in texto
    assert json.dumps("aceptacion.registrar") in texto
    insumos = sinteticos.caso("S01")["insumos"]
    for centinela in (
        insumos["sumaAsegurada"],
        insumos["primaTotal"],
        '"edad"',
        "leidoCondiciones",
    ):
        assert centinela not in texto, centinela
    [(payload,)] = _filas(limpia, "SELECT payload FROM outbox WHERE event_type = 'OfertaAceptada'")
    assert not {"sumaAsegurada", "edad", "declaraciones", "nombre"} & set(payload)


async def test_migracion_adelante_y_atras(base: str) -> None:
    """MT-W15-01: revertir 002 (dos veces) deja las decisiones; volver a aplicar la recrea."""
    reversa = MIGRACIONES.joinpath("down", "002_aceptaciones.sql").read_text(encoding="utf-8")
    with psycopg.connect(base, autocommit=True) as conexion:
        conexion.execute(reversa)
        conexion.execute(reversa)
    existe = "SELECT to_regclass('{}') IS NOT NULL"
    assert _filas(base, existe.format("aceptaciones")) == [(False,)]
    assert _filas(base, existe.format("decisiones_suscripcion")) == [(True,)]
    migrar(base)
    migrar(base)
    assert _filas(base, existe.format("aceptaciones")) == [(True,)]
    disparador = "SELECT count(*) FROM pg_trigger WHERE tgname = 'aceptaciones_inmutables'"
    assert _filas(base, disparador) == [(1,)]
