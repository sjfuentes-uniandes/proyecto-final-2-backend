"""Pruebas de módulo de HU-W11 (MT-W11-07 a 16) con PostgreSQL, WireMock y apps reales.

MT-W11-15 (migraciones abajo y arriba) no aplica: las migraciones son SQL idempotente
(DT-29a) y su idempotencia la cubren MT-W11-01 y el arranque de cada prueba.
"""

import json
import socket
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import psycopg
import pytest
from jsonschema import Draft202012Validator

EVENTOS = Path(__file__).resolve().parents[4] / "contracts" / "events"


def cliente_id(nn: str) -> str:
    return f"00000000-0000-4000-8000-0000000000{nn}"


def puerto_libre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        puerto: int = s.getsockname()[1]
        return puerto


VOLATILES = {"id", "calculadoEn", "duracionMs", "fuentes"}


def _sin_volatiles(perfil: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in perfil.items() if k not in VOLATILES}


def test_mt_w11_07_c01_oraculo_linaje_y_outbox(cotizacion: Any) -> None:
    svc = cotizacion()
    respuesta = svc.enviar("01")
    assert respuesta.status_code == 201, respuesta.text
    perfil = respuesta.json()
    assert (perfil["estado"], perfil["nivelRiesgo"], perfil["puntaje"], perfil["confianza"]) == (
        "CALCULADO",
        "BAJO",
        75.9,
        0.92,
    )
    assert (perfil["fuentesConsultadas"], perfil["versionModelo"]) == (4, "2026.10.1")
    assert perfil["calculadoEn"].endswith("Z")
    linaje = svc.bases.filas(
        "SELECT fuente, estado FROM linaje_fuentes WHERE perfil_id = %s ORDER BY orden",
        perfil["id"],
    )
    assert linaje == [
        ("FA_PRODUCTOS_VIGENTES", "CONSULTADA"),
        ("FA_HISTORIAL_PAGOS_12M", "CONSULTADA"),
        ("FA_INGRESOS_AGREGADOS", "CONSULTADA"),
        ("DA_REGISTROS_PUBLICOS", "CONSULTADA"),
    ]
    filas = svc.bases.filas(
        "SELECT event_id, aggregate_id, event_type, event_version, occurred_at, correlation_id, "
        "payload FROM outbox WHERE aggregate_id = %s ORDER BY event_type",
        perfil["id"],
    )
    assert [f[2] for f in filas] == ["DatosConsentidosConsultados", "PerfilRiesgoCalculado"]
    for event_id, agregado, tipo, version, ocurrido, correlacion, payload in filas:
        sobre = {
            "eventId": str(event_id),
            "aggregateId": str(agregado),
            "type": tipo,
            "version": version,
            "occurredAt": ocurrido.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "correlationId": correlacion,
            "payload": payload,
        }
        esquema = json.loads((EVENTOS / f"{tipo}.v1.schema.json").read_text(encoding="utf-8"))
        Draft202012Validator(esquema).validate(sobre)
        assert payload["perfilId"] == perfil["id"]


@pytest.mark.parametrize(
    ("nn", "nivel", "puntaje", "ajustes"),
    [("0e", "ALTO", 68.9, ["COINCIDENCIA_LISTAS_RESTRICTIVAS"]), ("0f", "MEDIO", 66.9, [])],
    ids=["C14", "C15"],
)
def test_mt_w11_08_oraculos_c14_y_c15(
    cotizacion: Any, nn: str, nivel: str, puntaje: float, ajustes: list[str]
) -> None:
    perfil = cotizacion().enviar(nn).json()
    assert (perfil["estado"], perfil["nivelRiesgo"], perfil["puntaje"]) == (
        "CALCULADO",
        nivel,
        puntaje,
    )
    assert [a["codigo"] for a in perfil["ajustes"]] == ajustes


def test_mt_w11_09_ac2_determinismo_y_reconstruccion(cotizacion: Any) -> None:
    svc = cotizacion()
    primero = svc.enviar("01").json()
    segundo = svc.enviar("01").json()
    assert primero["id"] != segundo["id"]
    assert _sin_volatiles(primero) == _sin_volatiles(segundo)
    for perfil in (primero, segundo):
        reconstruccion = svc.cliente.get(
            f"/v1/perfiles/{perfil['id']}/reconstruccion", headers=_actor()
        ).json()
        assert reconstruccion["coincide"] is True
        assert reconstruccion["versionModelo"] == "2026.10.1"
        assert reconstruccion["hashModelo"].startswith("sha256:")


def _actor() -> dict[str, str]:
    return {"X-Actor-Id": "asesor-sintetico", "X-Actor-Rol": "ASESOR"}


def _publicar(catalogo_url: str, version: str, producto: str, desde_80: bool) -> None:
    from catalogo.adapters.outbound.modelos_pg import definicion_semilla

    definicion = definicion_semilla()
    definicion["productoId"] = producto
    if desde_80:
        definicion["niveles"][0]["desde"] = "80"
    respuesta = httpx.post(
        f"{catalogo_url}/v1/modelos-riesgo",
        json={
            "version": version,
            "productoId": producto,
            "vigenteDesde": datetime.now(UTC).isoformat(),
            "definicion": definicion,
        },
        headers={"X-Actor-Id": "actuaria-sintetica", "X-Actor-Rol": "ADMIN_REGLAS"},
    )
    assert respuesta.status_code == 201, respuesta.text


def test_mt_w11_10_ac4_version_nueva_y_perfiles_anteriores(
    cotizacion: Any, catalogo_url: str
) -> None:
    # Producto propio: publicar sobre vida-hipotecario cambiaría el modelo del resto de la suite.
    producto = "vida-hipotecario-ac4"
    _publicar(catalogo_url, "2026.10.3", producto, desde_80=False)
    svc = cotizacion()
    p1 = svc.enviar("01", productoId=producto).json()
    assert (p1["versionModelo"], p1["nivelRiesgo"]) == ("2026.10.3", "BAJO")
    _publicar(catalogo_url, "2026.10.4", producto, desde_80=True)
    p2 = svc.enviar("01", productoId=producto).json()
    assert (p2["versionModelo"], p2["nivelRiesgo"], p2["puntaje"]) == ("2026.10.4", "MEDIO", 75.9)
    p1_despues = svc.cliente.get(f"/v1/perfiles/{p1['id']}", headers=_actor()).json()
    assert (p1_despues["versionModelo"], p1_despues["nivelRiesgo"]) == ("2026.10.3", "BAJO")
    reconstruccion = svc.cliente.get(
        f"/v1/perfiles/{p1['id']}/reconstruccion", headers=_actor()
    ).json()
    assert reconstruccion["coincide"] is True


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE perfiles_riesgo SET nivel_riesgo = 'ALTO' WHERE id = %s",
        "DELETE FROM perfiles_riesgo WHERE id = %s",
        "UPDATE linaje_fuentes SET estado = 'NO_AUTORIZADA' WHERE perfil_id = %s",
    ],
    ids=["update-perfil", "delete-perfil", "update-linaje"],
)
def test_mt_w11_11_inmutabilidad(cotizacion: Any, sql: str) -> None:
    svc = cotizacion()
    perfil = svc.enviar("01").json()
    with pytest.raises(psycopg.errors.RaiseException, match="inmutable"):
        svc.bases.filas(sql, perfil["id"])


def test_mt_w11_12_idempotencia(cotizacion: Any) -> None:
    svc = cotizacion()
    clave = uuid4()
    primero = svc.enviar("01", clave=clave)
    repetido = svc.enviar("01", clave=clave)
    assert (primero.status_code, repetido.status_code) == (201, 200)
    assert repetido.json()["id"] == primero.json()["id"]
    otro = svc.enviar("01", clave=clave, plazoCreditoMeses=240)
    assert otro.status_code == 422
    assert otro.json()["codigo"] == "IDEMPOTENCIA_CONFLICTO"
    filas = svc.bases.filas(
        "SELECT count(*) FROM perfiles_riesgo WHERE idempotency_key = %s", clave
    )
    assert filas == [(1,)]


@pytest.mark.parametrize(
    ("nn", "estado", "codigo"),
    [
        ("0b", 403, "IDENTIDAD_NO_VERIFICADA"),
        ("0c", 409, "IDENTIDAD_PENDIENTE"),
        ("ff", 404, "CLIENTE_NO_ENCONTRADO"),
    ],
    ids=["C11", "C12", "desconocido"],
)
def test_mt_w11_13_rechazos_sin_persistir_ni_llamar(
    cotizacion: Any, nn: str, estado: int, codigo: str
) -> None:
    svc = cotizacion()
    respuesta = svc.enviar(nn)
    assert respuesta.status_code == estado
    assert respuesta.json()["codigo"] == codigo
    assert svc.bases.filas(
        "SELECT count(*) FROM perfiles_riesgo WHERE cliente_id = %s", cliente_id(nn)
    ) == [(0,)]
    assert svc.wiremock.a_aliados(cliente_id(nn)) == []


def test_mt_w11_14_catalogo_detenido(cotizacion: Any) -> None:
    svc = cotizacion(CATALOGO_URL=f"http://127.0.0.1:{puerto_libre()}")
    respuesta = svc.enviar("01")
    assert respuesta.status_code == 503
    assert respuesta.json()["codigo"] == "DEPENDENCIA_NO_DISPONIBLE"
    assert respuesta.json()["detalles"] == {"dependencia": "catalogo.modelo"}
    assert "127.0.0.1" not in respuesta.text


def test_mt_w11_16_traza_con_hijos_por_dependencia(cotizacion: Any) -> None:
    svc = cotizacion()
    respuesta = svc.enviar("01", correlation_id="corr-mt-w11-16")
    assert respuesta.headers["X-Correlation-Id"] == "corr-mt-w11-16"
    spans = [s for s in svc.lista_spans() if s.attributes.get("correlation_id") == "corr-mt-w11-16"]
    (padre,) = [s for s in spans if s.name == "perfilamiento"]
    assert padre.attributes["result"] == "ok"
    hijos = {s.name: s for s in spans if s.name.startswith("perfilamiento.dependencia.")}
    assert set(hijos) == {
        "perfilamiento.dependencia.clientes.identidad",
        "perfilamiento.dependencia.clientes.consentimiento",
        "perfilamiento.dependencia.catalogo.modelo",
    }
    for hijo in hijos.values():
        assert hijo.parent is not None and hijo.parent.span_id == padre.context.span_id
        assert hijo.context.trace_id == padre.context.trace_id


def test_health_con_base(cotizacion: Any) -> None:
    assert cotizacion().cliente.get("/health").json() == {"status": "ok"}
