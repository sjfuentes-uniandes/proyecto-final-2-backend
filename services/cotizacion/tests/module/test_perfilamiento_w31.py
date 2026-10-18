"""Pruebas de módulo de HU-W31 (PR 3.4): comprobación final del consentimiento tras la fase B.

Con PostgreSQL, WireMock (doble de `clientes` con escenarios) y las apps reales de catalogo y
adaptador-datos. El escenario hace que `clientes` responda vigente en la fase A y distinto en la
comprobación final, como si la revocación ocurriera mientras se consultan las fuentes. La PR 3.7
repite el caso contra `clientes` real (R2 de fallo b).
"""

import json
from datetime import UTC
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
from jsonschema import Draft202012Validator

EVENTOS = Path(__file__).resolve().parents[4] / "contracts" / "events"
MIGRACIONES = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "cotizacion"
    / "adapters"
    / "outbound"
    / "migraciones"
)
C01 = "00000000-0000-4000-8000-000000000001"
TODAS = [
    "FA_PRODUCTOS_VIGENTES",
    "FA_HISTORIAL_PAGOS_12M",
    "FA_INGRESOS_AGREGADOS",
    "DA_REGISTROS_PUBLICOS",
]
ACTOR = {"X-Actor-Id": "asesor-sintetico", "X-Actor-Rol": "ASESOR"}


def _verificaciones(svc: Any) -> list[list[str]]:
    """Alcances de cada verificación de C01 que recibió el doble, en orden."""
    return [
        json.loads(r["body"])["alcances"]
        for r in svc.wiremock.solicitudes()
        if r["url"] == f"/clientes/v1/clientes/{C01}/consentimientos/verificaciones"
    ]


def _datos_consentidos(svc: Any, perfil_id: str) -> dict[str, Any]:
    ((evento_id, ocurrido, correlacion, payload),) = svc.bases.filas(
        "SELECT event_id, occurred_at, correlation_id, payload FROM outbox "
        "WHERE aggregate_id = %s AND event_type = 'DatosConsentidosConsultados'",
        perfil_id,
    )
    sobre = {
        "eventId": str(evento_id),
        "aggregateId": perfil_id,
        "type": "DatosConsentidosConsultados",
        "version": 1,
        "occurredAt": ocurrido.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "correlationId": correlacion,
        "payload": payload,
    }
    esquema = json.loads(
        (EVENTOS / "DatosConsentidosConsultados.v1.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator(esquema).validate(sobre)
    return dict(payload)


def test_mt_w31_01_revocacion_durante_la_fase_b_conserva_y_marca(
    cotizacion: Any, centinelas: tuple[str, ...]
) -> None:
    svc = cotizacion()
    svc.wiremock.activar_escenario("revocacion-en-vuelo-c01", "fase-a")

    respuesta = svc.enviar("01")

    assert respuesta.status_code == 201, respuesta.text
    perfil = respuesta.json()
    assert perfil["revocadoDurantePerfilamiento"] is True
    assert perfil["comprobacionFinal"] == {
        "decision": "RECHAZADO",
        "causa": "REVOCADO",
        "vigenteHasta": None,
    }
    # La fase A autorizó; lo obtenido se conserva (ADR 0004): mismo oráculo que C01.
    assert perfil["consentimiento"]["decision"] == "PERMITIDO"
    assert (perfil["estado"], perfil["nivelRiesgo"], perfil["puntaje"]) == (
        "CALCULADO",
        "BAJO",
        75.9,
    )
    assert [f["estado"] for f in perfil["fuentes"]] == ["CONSULTADA"] * 4
    # Dos verificaciones: fase A (todos los alcances) y comprobación final (los consultados).
    assert _verificaciones(svc) == [TODAS, TODAS]
    # Ninguna consulta nueva después de la comprobación: una por fuente.
    assert len(svc.wiremock.a_aliados(C01)) == 4

    ((comprobacion,),) = svc.bases.filas(
        "SELECT comprobacion_final FROM perfiles_riesgo WHERE id = %s", perfil["id"]
    )
    assert (comprobacion["decision"], comprobacion["causa"]) == ("RECHAZADO", "REVOCADO")
    payload = _datos_consentidos(svc, perfil["id"])
    assert payload["revocadoDurantePerfilamiento"] is True
    assert payload["comprobacionFinal"] == {"decision": "RECHAZADO", "causa": "REVOCADO"}
    assert payload["consentimientoId"] == "00000000-0000-4000-9000-000000000001"

    consultado = svc.cliente.get(f"/v1/perfiles/{perfil['id']}", headers=ACTOR).json()
    assert consultado["revocadoDurantePerfilamiento"] is True
    assert consultado["comprobacionFinal"] == perfil["comprobacionFinal"]
    # Sin valores financieros ni declarados en la respuesta, el evento ni los logs.
    texto = respuesta.text + json.dumps(payload) + svc.logs.getvalue()
    for centinela in centinelas:
        assert centinela not in texto


def test_mt_w31_02_comprobacion_vigente_sin_marca(cotizacion: Any) -> None:
    svc = cotizacion()
    perfil = svc.enviar("01").json()
    assert perfil["revocadoDurantePerfilamiento"] is False
    assert perfil["comprobacionFinal"] == {
        "decision": "PERMITIDO",
        "causa": None,
        "vigenteHasta": "2027-10-01T00:00:00Z",
    }
    assert _verificaciones(svc) == [TODAS, TODAS]
    payload = _datos_consentidos(svc, perfil["id"])
    assert payload["comprobacionFinal"] == {"decision": "PERMITIDO", "causa": None}
    assert payload["revocadoDurantePerfilamiento"] is False


def test_mt_w31_03_rechazo_en_fase_a_no_comprueba(cotizacion: Any) -> None:
    svc = cotizacion()
    perfil = svc.enviar("08").json()  # C08: revocado antes de empezar
    assert perfil["accionSugerida"] == "COMPLETAR_CONSENTIMIENTO"
    assert (perfil["comprobacionFinal"], perfil["revocadoDurantePerfilamiento"]) == (None, False)
    verificaciones = [r for r in svc.wiremock.solicitudes() if r["url"].endswith("/verificaciones")]
    assert len(verificaciones) == 1


def test_mt_w31_04_caida_de_clientes_en_la_comprobacion_falla_cerrado(cotizacion: Any) -> None:
    svc = cotizacion()
    svc.wiremock.activar_escenario("caida-al-comprobar-c01", "fase-a")
    clave = uuid4()

    respuesta = svc.enviar("01", clave=clave)

    assert respuesta.status_code == 503, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["codigo"] == "DEPENDENCIA_NO_DISPONIBLE"
    assert cuerpo["detalles"] == {"dependencia": "clientes.consentimiento"}
    assert len(_verificaciones(svc)) == 2
    assert (
        svc.bases.filas(
            "SELECT id FROM perfiles_riesgo WHERE cliente_id = %s AND idempotency_key = %s",
            C01,
            clave,
        )
        == []
    )


def test_mt_w31_05_migracion_de_la_comprobacion_y_su_reversa(cotizacion: Any) -> None:
    from cotizacion.adapters.outbound.perfiles_pg import migrar

    svc = cotizacion()
    servidor = svc.bases.cotizacion.rsplit("/", 1)[0]
    base = f"w31_{uuid4().hex[:8]}"
    with psycopg.connect(svc.bases.cotizacion, autocommit=True) as conexion:
        conexion.execute(f"CREATE DATABASE {base}")
    url = f"{servidor}/{base}"
    columna = (
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_name = 'perfiles_riesgo' AND column_name = 'comprobacion_final'"
    )
    reversa = (MIGRACIONES / "down" / "002_comprobacion_final.sql").read_text(encoding="utf-8")

    migrar(url)
    migrar(url)  # idempotente
    with psycopg.connect(url, autocommit=True) as conexion:
        assert conexion.execute(columna).fetchall() == [("YES",)]
        conexion.execute(reversa)
        conexion.execute(reversa)  # la reversa también es idempotente
        assert conexion.execute(columna).fetchall() == []
    migrar(url)
    with psycopg.connect(url, autocommit=True) as conexion:
        assert conexion.execute(columna).fetchall() == [("YES",)]
