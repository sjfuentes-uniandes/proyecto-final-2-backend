"""R2 y R8 (HU-W31, PR 3.7), versión de módulo: cotizacion → `clientes` real → PostgreSQL.

Corre en CI sin AWS (decisión P1-5, "R8 en dos niveles"). Nada se simula entre cotizacion y la
base de `clientes`: la verificación es la ruta real (PR 3.2) y cada decisión deja su
`VerificacionConsentimientoRegistrada` en el outbox de `clientes`, que aquí se lee como prueba de
que la decisión quedó registrada (W31 AC1 y AC3). Los aliados son el simulador (WireMock).

- R2 feliz: consentimiento vigente → `PERMITIDO` → perfil completo y decisión registrada.
- R2 fallo (§e Parte 3, d): (a) revocado, vencido, sin consentimiento o de otro propósito →
  `RECHAZADO` con causa estable sin llamar al proveedor; (c) `clientes` caído o sin poder
  registrar → falla cerrado. El caso (b), revocación a mitad del perfilamiento, está en
  `test_perfilamiento_w31.py` (`test_mt_w31_01`).
- R8: otorgar → verificar `PERMITIDO` → revocar → verificar `RECHAZADO` (cierra M09 AC2).

La versión para QA, contra el ambiente desplegado, está en `tests/e2e/test_hu_w31_r8_verificacion.py`.
Solo datos sintéticos.
"""

import asyncio
import json
import socket
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from cotizacion.domain.perfilamiento.modelos import Fuente
from jsonschema import Draft202012Validator

RAIZ = Path(__file__).resolve().parents[4]
ESQUEMA_VERIFICACION = json.loads(
    (
        RAIZ / "contracts" / "events" / "VerificacionConsentimientoRegistrada.v1.schema.json"
    ).read_text(encoding="utf-8")
)
CASOS: list[dict[str, Any]] = json.loads(
    (RAIZ / "contracts" / "datos-sinteticos" / "clientes-perfilamiento.json").read_text(
        encoding="utf-8"
    )
)
PROPOSITO = "PERFILAMIENTO_PRECIO"
TODOS_LOS_ALCANCES = [f.value for f in Fuente]
# `Clientes` del conftest (clientes real): siembra, outbox y API del titular.
Clientes = Any
# Un cliente fuera de C01–C19, con un consentimiento vigente de otro propósito (`proposito` no
# tiene CHECK en la migración 002; el dominio solo conoce uno).
C20 = "00000000-0000-4000-8000-000000000014"
OTRO_PROPOSITO = "PROPOSITO_SINTETICO_OTRO"


def cliente_id(nn: str) -> str:
    return f"00000000-0000-4000-8000-0000000000{nn}"


def consentimiento_id(nn: str) -> str:
    """El que siembra el conftest para cada caso."""
    return f"00000000-0000-4000-9000-0000000000{nn}"


def _instante(valor: str) -> datetime:
    return datetime.fromisoformat(valor)


def _fuentes(perfil: dict[str, Any]) -> dict[str, str]:
    return {f["fuente"]: f["estado"] for f in perfil["fuentes"]}


def _decisiones(clientes: Clientes, cliente: str) -> list[tuple[str, str | None]]:
    return [(v["decision"], v["causa"]) for v in clientes.verificaciones(cliente)]


def _eventos_de_cotizacion(svc: Any, perfil_id: str) -> list[str]:
    filas = svc.bases.filas(
        "SELECT event_type FROM outbox WHERE aggregate_id = %s ORDER BY event_type", perfil_id
    )
    return [tipo for (tipo,) in filas]


def _sin_valores_sensibles(texto: str, centinelas: tuple[str, ...]) -> None:
    for centinela in centinelas:
        assert centinela not in texto, centinela


def _sembrar_otro_proposito(clientes: Clientes) -> None:
    clientes.filas(
        "INSERT INTO cliente (id, sub, estado, verificado_en) "
        "VALUES (%s, 'sub-sintetico-14', 'VERIFICADO', %s)",
        C20,
        datetime(2026, 10, 1, tzinfo=UTC),
    )
    clientes.filas(
        "INSERT INTO consentimiento (id, cliente_id, proposito, alcances, version_texto, idioma, "
        "estado, otorgado_en, vigente_hasta) VALUES (%s, %s, %s, %s, '2026-10-17.1', 'es-CO', "
        "'ACTIVO', %s, %s)",
        consentimiento_id("14"),
        C20,
        OTRO_PROPOSITO,
        TODOS_LOS_ALCANCES,
        datetime(2026, 10, 1, tzinfo=UTC),
        datetime(2027, 10, 1, tzinfo=UTC),
    )


# --- Los datos sintéticos, contra `clientes` real ------------------------------------------


@pytest.mark.parametrize("caso", CASOS, ids=lambda c: c["id"])
def test_clientes_real_decide_lo_que_dicen_los_datos_sinteticos(
    clientes_real: Clientes, caso: dict[str, Any]
) -> None:
    """La siembra reproduce cada caso: el adaptador real de cotizacion (`ClientesHttp`) recibe
    de `clientes` real la identidad y la decisión de `clientes-perfilamiento.json` (lo que antes
    aseguraba `test_dobles_clientes.py` sobre el doble)."""
    from cotizacion.adapters.outbound.clientes_http import ClientesHttp

    clientes_real.sembrar()

    async def consultar() -> tuple[Any, Any]:
        async with httpx.AsyncClient() as cliente:
            adaptador = ClientesHttp(cliente, clientes_real.url, timeout_ms=5000)
            identificador = UUID(caso["clienteId"])
            return (
                await adaptador.estado(identificador),
                await adaptador.verificar(identificador, PROPOSITO, list(Fuente)),
            )

    identidad, verificacion = asyncio.run(consultar())

    esperado = caso["consentimiento"]
    assert identidad.value == caso["identidad"]
    assert verificacion.decision.value == esperado["decision"]
    assert verificacion.causa == esperado["causa"]
    assert {f.value for f in verificacion.alcances_permitidos} == set(
        esperado["alcancesPermitidos"]
    )
    assert verificacion.consentimiento_id == (
        UUID(esperado["consentimientoId"]) if esperado["consentimientoId"] else None
    )


# --- R2 feliz ------------------------------------------------------------------------------


def test_r2_feliz_consentimiento_vigente_perfil_completo_y_decision_registrada(
    cotizacion: Any, centinelas: tuple[str, ...]
) -> None:
    svc = cotizacion()

    respuesta = svc.enviar("01", correlation_id="corr-r2-feliz")

    assert respuesta.status_code == 201, respuesta.text
    perfil = respuesta.json()
    assert (perfil["estado"], perfil["nivelRiesgo"], perfil["puntaje"]) == (
        "CALCULADO",
        "BAJO",
        75.9,
    )
    assert perfil["consentimiento"]["decision"] == "PERMITIDO"
    assert set(_fuentes(perfil).values()) == {"CONSULTADA"}
    assert len(svc.wiremock.a_aliados(cliente_id("01"))) == 4
    assert _eventos_de_cotizacion(svc, perfil["id"]) == [
        "DatosConsentidosConsultados",
        "PerfilRiesgoCalculado",
    ]
    # AC1: `clientes` registró cada decisión (fase A y comprobación final) con el consentimiento
    # evaluado, sin datos financieros.
    filas = svc.clientes.filas(
        "SELECT event_id, aggregate_id, event_type, event_version, occurred_at, correlation_id, "
        "payload FROM outbox WHERE event_type = 'VerificacionConsentimientoRegistrada' "
        "ORDER BY occurred_at"
    )
    assert len(filas) == 2
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
        Draft202012Validator(ESQUEMA_VERIFICACION).validate(sobre)
        assert str(agregado) == cliente_id("01")
        assert correlacion == "corr-r2-feliz"
        assert (payload["decision"], payload["causa"]) == ("PERMITIDO", None)
        assert payload["consentimientoId"] == consentimiento_id("01")
        assert payload["alcancesRechazados"] == []
    _sin_valores_sensibles(json.dumps([f[6] for f in filas]), centinelas)


def test_r2_alcance_no_autorizado_rechaza_solo_ese_acceso(
    cotizacion: Any, centinelas: tuple[str, ...]
) -> None:
    """W31 AC3 (C02, sin `FA_INGRESOS_AGREGADOS`): solo ese acceso se rechaza y se registra qué
    faltó; la comprobación final pide solo los alcances consultados."""
    svc = cotizacion()

    perfil = svc.enviar("02").json()

    assert perfil["consentimiento"]["decision"] == "PERMITIDO_PARCIAL"
    assert _fuentes(perfil)["FA_INGRESOS_AGREGADOS"] == "NO_AUTORIZADA"
    assert {f: e for f, e in _fuentes(perfil).items() if f != "FA_INGRESOS_AGREGADOS"} == {
        "FA_PRODUCTOS_VIGENTES": "CONSULTADA",
        "FA_HISTORIAL_PAGOS_12M": "CONSULTADA",
        "DA_REGISTROS_PUBLICOS": "CONSULTADA",
    }
    assert not [s for s in svc.wiremock.a_aliados(cliente_id("02")) if "income" in s["url"]]
    fase_a, final = svc.clientes.verificaciones(cliente_id("02"))
    assert (fase_a["decision"], fase_a["alcancesRechazados"]) == (
        "PERMITIDO_PARCIAL",
        ["FA_INGRESOS_AGREGADOS"],
    )
    assert "FA_INGRESOS_AGREGADOS" not in final["alcancesSolicitados"]
    assert (final["decision"], final["alcancesRechazados"]) == ("PERMITIDO", [])
    _sin_valores_sensibles(json.dumps([fase_a, final]), centinelas)


# --- R2 fallo (a): rechazo antes de llamar al proveedor --------------------------------------


@pytest.mark.parametrize(
    ("cliente", "causa", "evaluado"),
    [
        (cliente_id("08"), "REVOCADO", consentimiento_id("08")),
        (cliente_id("09"), "VENCIDO", consentimiento_id("09")),
        (cliente_id("0a"), "SIN_CONSENTIMIENTO", None),
        (C20, "PROPOSITO_NO_AUTORIZADO", None),
    ],
    ids=["C08-revocado", "C09-vencido", "C10-sin-consentimiento", "C20-otro-proposito"],
)
def test_r2_fallo_rechazo_con_causa_estable_sin_llamar_al_proveedor(
    cotizacion: Any, cliente: str, causa: str, evaluado: str | None
) -> None:
    svc = cotizacion()
    if cliente == C20:
        _sembrar_otro_proposito(svc.clientes)

    respuesta = svc.enviar(cliente[-2:])

    assert respuesta.status_code == 201, respuesta.text
    perfil = respuesta.json()
    assert (perfil["estado"], perfil["accionSugerida"]) == (
        "INFORMACION_INSUFICIENTE",
        "COMPLETAR_CONSENTIMIENTO",
    )
    assert (perfil["consentimiento"]["decision"], perfil["consentimiento"]["causa"]) == (
        "RECHAZADO",
        causa,
    )
    assert set(_fuentes(perfil).values()) == {"NO_AUTORIZADA"}
    assert svc.wiremock.a_aliados(cliente) == []
    assert (perfil["comprobacionFinal"], perfil["revocadoDurantePerfilamiento"]) == (None, False)
    # Una sola verificación registrada, con la causa y el consentimiento evaluado (PR 3.2).
    (registrada,) = svc.clientes.verificaciones(cliente)
    assert (registrada["decision"], registrada["causa"]) == ("RECHAZADO", causa)
    assert registrada["consentimientoId"] == evaluado
    assert sorted(registrada["alcancesRechazados"]) == sorted(TODOS_LOS_ALCANCES)


def test_r2_fallo_cliente_inexistente_sin_perfil_ni_registro(cotizacion: Any) -> None:
    svc = cotizacion()
    desconocido = cliente_id("ff")

    respuesta = svc.enviar("ff")

    assert respuesta.status_code == 404
    assert respuesta.json()["codigo"] == "CLIENTE_NO_ENCONTRADO"
    assert svc.bases.filas(
        "SELECT count(*) FROM perfiles_riesgo WHERE cliente_id = %s", desconocido
    ) == [(0,)]
    assert svc.wiremock.a_aliados(desconocido) == []
    assert svc.clientes.filas("SELECT count(*) FROM outbox") == [(0,)]


# --- R2 fallo (c): `clientes` con falla técnica → falla cerrado -------------------------------


def _puerto_cerrado() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        puerto: int = s.getsockname()[1]
        return puerto


def test_r2_fallo_clientes_caido_falla_cerrado(cotizacion: Any) -> None:
    svc = cotizacion(CLIENTES_URL=f"http://127.0.0.1:{_puerto_cerrado()}")
    clave = uuid4()

    respuesta = svc.enviar("01", clave=clave)

    assert respuesta.status_code == 503, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["codigo"] == "DEPENDENCIA_NO_DISPONIBLE"
    assert cuerpo["detalles"] == {"dependencia": "clientes.identidad"}
    assert "127.0.0.1" not in respuesta.text
    assert svc.wiremock.a_aliados(cliente_id("01")) == []
    assert svc.bases.filas(
        "SELECT count(*) FROM perfiles_riesgo WHERE idempotency_key = %s", clave
    ) == [(0,)]


def test_r2_fallo_clientes_sin_registrar_la_decision_falla_cerrado(
    cotizacion: Any, clientes_real: Clientes
) -> None:
    """La identidad responde, pero `clientes` no puede escribir la decisión en su outbox (la
    lectura y el registro van en una transacción): no responde a tiempo y cotizacion no consulta
    ninguna fuente."""
    svc = cotizacion()
    clave = uuid4()
    bloqueo = clientes_real.bloqueo_outbox()
    bloqueo.tomar()
    try:
        respuesta = svc.enviar("01", clave=clave)
    finally:
        bloqueo.soltar()

    assert respuesta.status_code == 503, respuesta.text
    assert respuesta.json()["detalles"] == {"dependencia": "clientes.consentimiento"}
    assert svc.wiremock.a_aliados(cliente_id("01")) == []
    assert svc.bases.filas(
        "SELECT count(*) FROM perfiles_riesgo WHERE idempotency_key = %s", clave
    ) == [(0,)]
    # La decisión que `clientes` tomó queda registrada al liberarse el outbox, aunque la
    # respuesta ya no llegó: el registro de auditoría no pierde verificaciones.
    (registrada,) = clientes_real.esperar_verificaciones(cliente_id("01"), 1)
    assert registrada["decision"] == "PERMITIDO"


# --- R8: otorgar → verificar → revocar → verificar ------------------------------------------


def test_r8_la_revocacion_deja_de_permitir_en_la_siguiente_verificacion(
    cotizacion: Any, centinelas: tuple[str, ...]
) -> None:
    """§e Parte 3, d (R8) y M09 AC2: tras revocar, la siguiente consulta se rechaza sin llamar al
    proveedor. La revocación es efectiva en la verificación siguiente (no hay caché, ADR 0011),
    muy por debajo de los 5 min."""
    svc = cotizacion()
    c01 = cliente_id("01")
    svc.clientes.sembrar(sin_consentimiento={"01"})

    otorgado = svc.clientes.otorgar("01", TODOS_LOS_ALCANCES)
    antes = svc.enviar("01")

    assert antes.status_code == 201, antes.text
    assert antes.json()["consentimiento"]["decision"] == "PERMITIDO"
    assert antes.json()["estado"] == "CALCULADO"
    assert len(svc.wiremock.a_aliados(c01)) == 4

    revocado = svc.clientes.revocar("01", otorgado["consentimientoId"])
    svc.wiremock.limpiar_journal()
    despues = svc.enviar("01")

    assert despues.status_code == 201, despues.text
    perfil = despues.json()
    assert (perfil["consentimiento"]["decision"], perfil["consentimiento"]["causa"]) == (
        "RECHAZADO",
        "REVOCADO",
    )
    assert perfil["accionSugerida"] == "COMPLETAR_CONSENTIMIENTO"
    assert set(_fuentes(perfil).values()) == {"NO_AUTORIZADA"}
    assert svc.wiremock.a_aliados(c01) == []
    # En `clientes`: dos PERMITIDO del primer perfil y un RECHAZADO posterior a la revocación,
    # todos sobre el consentimiento otorgado; los hechos del consentimiento en su outbox.
    verificaciones = svc.clientes.verificaciones(c01)
    assert _decisiones(svc.clientes, c01) == [
        ("PERMITIDO", None),
        ("PERMITIDO", None),
        ("RECHAZADO", "REVOCADO"),
    ]
    assert {v["consentimientoId"] for v in verificaciones} == {otorgado["consentimientoId"]}
    assert _instante(verificaciones[-1]["verificadoEn"]) >= _instante(revocado["revocadoEn"])
    tipos = svc.clientes.filas(
        "SELECT event_type, count(*) FROM outbox "
        "WHERE event_type IN ('ConsentimientoOtorgado', 'ConsentimientoRevocado') "
        "GROUP BY event_type ORDER BY event_type"
    )
    assert tipos == [("ConsentimientoOtorgado", 1), ("ConsentimientoRevocado", 1)]
    _sin_valores_sensibles(json.dumps(verificaciones) + svc.logs.getvalue(), centinelas)
