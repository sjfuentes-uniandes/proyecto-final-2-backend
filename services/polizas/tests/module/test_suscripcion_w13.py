"""MT-W13-01 y MT-W13-02: la API de polizas con PostgreSQL efímero (Testcontainers) y dobles HTTP
de cotizacion y catalogo (transporte simulado de httpx con los contratos publicados).

Cubre los criterios técnicos 1 a 6 de §2.1: los tres resultados, idempotencia y 20 solicitudes
concurrentes (una sola fila de decisión y una de outbox), precondiciones sin filas, dependencias
caídas o lentas (503 en menos de 1 s, sin decisión), inmutabilidad, reconstrucción, bandeja y
resumen sobre SQL real, logs sin PII y migraciones adelante y atrás.
"""

import asyncio
import json
import time
from importlib import resources
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from polizas.adapters.outbound.decisiones_pg import migrar

docker = pytest.importorskip("docker")
pytestmark = pytest.mark.anyio
MIGRACIONES = resources.files("polizas.adapters.outbound").joinpath("migraciones")
BASE = "/v1/suscripciones"
ASESOR = {"X-Actor-Id": "sub-asesor-1", "X-Actor-Rol": "ASESOR"}
SUSCRIPTOR = {"X-Actor-Id": "sub-suscriptor-1", "X-Actor-Rol": "SUSCRIPTOR"}


def _filas(url: str, sql: str, *parametros: Any) -> list[tuple[Any, ...]]:
    with psycopg.connect(url, autocommit=True) as conexion:
        return conexion.execute(sql, parametros).fetchall()


def _ejecutar(url: str, sql: str) -> None:
    with psycopg.connect(url, autocommit=True) as conexion:
        conexion.execute(sql)


async def _solicitar(api: Any, sinteticos: Any, id_caso: str, clave: str | None = None) -> Any:
    return await api.post(
        BASE,
        json={"cotizacionId": sinteticos.cotizacion_id(id_caso)},
        headers={**ASESOR, "Idempotency-Key": clave or str(uuid4())},
    )


async def test_los_resultados_se_persisten_con_su_evento_y_se_reconstruyen(
    api: Any, sinteticos: Any, limpia: str
) -> None:
    """Criterios 1, 2, 3 y 6: resultado, versión y huella de reglas, fila de outbox por decisión
    y reconstrucción que coincide."""
    decididos = ["S01", "S02", "S03", "S04", "S05", "S06", "S07", "S08"]
    for id_caso in decididos:
        respuesta = await _solicitar(api, sinteticos, id_caso)
        esperado = sinteticos.caso(id_caso)["esperado"]
        cuerpo = respuesta.json()
        assert respuesta.status_code == 201, id_caso
        assert cuerpo["resultado"] == esperado["resultado"], id_caso
        assert cuerpo["reglaDeterminante"] == esperado["reglaDeterminante"], id_caso
        assert (cuerpo["versionReglas"], cuerpo["hashReglas"]) == (
            "2026.10.1",
            sinteticos.respuesta_reglas()["hash"],
        )
        reconstruccion = await api.get(f"{BASE}/{cuerpo['id']}/reconstruccion", headers=SUSCRIPTOR)
        assert reconstruccion.json()["coincide"] is True, id_caso
    filas = _filas(limpia, "SELECT resultado, count(*) FROM decisiones_suscripcion GROUP BY 1")
    assert dict(filas) == {"APROBADA": 1, "RECHAZADA": 3, "REVISION_REQUERIDA": 4}
    eventos = _filas(limpia, "SELECT event_type, event_version, payload FROM outbox")
    assert len(eventos) == len(decididos)
    assert {(t, v) for t, v, _ in eventos} == {("DecisionSuscripcionRegistrada", 1)}
    for _, _, payload in eventos:
        assert not {"sumaAsegurada", "primaTotal", "edad"} & set(payload)
    # AC3: una revisión no emite póliza ni otro evento.
    assert _filas(
        limpia,
        "SELECT count(*) FROM outbox WHERE event_type <> %s",
        "DecisionSuscripcionRegistrada",
    ) == [(0,)]


async def test_idempotencia_y_veinte_solicitudes_concurrentes(
    api: Any, sinteticos: Any, limpia: str
) -> None:
    """Criterio 4: una sola decisión y un solo evento por cotización."""
    clave = str(uuid4())
    primera = await _solicitar(api, sinteticos, "S01", clave)
    repetida = await _solicitar(api, sinteticos, "S01", clave)
    assert (primera.status_code, repetida.status_code) == (201, 200)
    assert repetida.json() == primera.json()
    respuestas = await asyncio.gather(*(_solicitar(api, sinteticos, "S06") for _ in range(20)))
    estados = sorted(r.status_code for r in respuestas)
    assert estados == [200] * 19 + [201]
    assert len({r.json()["id"] for r in respuestas}) == 1
    por_cotizacion = "SELECT count(*) FROM decisiones_suscripcion WHERE cotizacion_id = %s"
    assert _filas(limpia, por_cotizacion, sinteticos.cotizacion_id("S06")) == [(1,)]
    assert _filas(limpia, "SELECT count(*) FROM outbox") == [(2,)]


async def test_precondiciones_y_dependencias_sin_filas(
    api: Any, sinteticos: Any, limpia: str
) -> None:
    """Criterio 5: vencida, inexistente, identidad y dependencias caídas o lentas, sin filas."""
    casos = {"S09": (409, "OFERTA_VENCIDA"), "S10": (403, "IDENTIDAD_NO_VERIFICADA")}
    for id_caso, esperado in casos.items():
        respuesta = await _solicitar(api, sinteticos, id_caso)
        assert (respuesta.status_code, respuesta.json()["codigo"]) == esperado
    inexistente = await api.post(
        BASE,
        json={"cotizacionId": str(uuid4())},
        headers={**ASESOR, "Idempotency-Key": str(uuid4())},
    )
    assert inexistente.json()["codigo"] == "COTIZACION_NO_ENCONTRADA"
    for dependencia in ("cotizacion", "catalogo"):
        for estado in ("caido", "lento"):
            setattr(api.proveedores, dependencia, estado)
            inicio = time.perf_counter()
            respuesta = await _solicitar(api, sinteticos, "S01")
            duracion = time.perf_counter() - inicio
            assert respuesta.status_code == 503, (dependencia, estado)
            assert respuesta.json()["detalles"] == {"dependencia": dependencia}
            assert duracion < 1, (dependencia, estado, duracion)
            setattr(api.proveedores, dependencia, "nominal")
    assert _filas(limpia, "SELECT count(*) FROM decisiones_suscripcion") == [(0,)]
    assert _filas(limpia, "SELECT count(*) FROM outbox") == [(0,)]


async def test_la_decision_es_inmutable(api: Any, sinteticos: Any, limpia: str) -> None:
    """Criterio 6."""
    await _solicitar(api, sinteticos, "S01")
    with pytest.raises(psycopg.errors.RaiseException, match="inmutable"):
        _ejecutar(limpia, "UPDATE decisiones_suscripcion SET resultado = 'RECHAZADA'")
    with pytest.raises(psycopg.errors.RaiseException, match="inmutable"):
        _ejecutar(limpia, "DELETE FROM decisiones_suscripcion")
    with pytest.raises(psycopg.errors.CheckViolation):
        _ejecutar(
            limpia,
            "INSERT INTO decisiones_suscripcion SELECT gen_random_uuid(), 'DEC-1', "
            "gen_random_uuid(), oferta_id, cotizacion_codigo, cliente_id, producto_id, "
            "gen_random_uuid(), huella_solicitud, resultado, tipo, regla_determinante, "
            "reglas_evaluadas, motivos, insumos, hash_insumos, version_reglas, hash_reglas, "
            "decidida_en, duracion_ms, correlation_id, actor_id FROM decisiones_suscripcion",
        )


async def test_bandeja_y_resumen_sobre_la_base(api: Any, sinteticos: Any, dobles: Any) -> None:
    from datetime import timedelta

    ahora = sinteticos.ahora
    for id_caso, horas in (("S05", 9), ("S06", 2), ("S07", 1), ("S01", 0)):
        dobles.reloj.instante = ahora - timedelta(hours=horas)
        await _solicitar(api, sinteticos, id_caso)
    dobles.reloj.instante = ahora
    pagina = (await api.get(f"{BASE}/bandeja", params={"limite": 2}, headers=SUSCRIPTOR)).json()
    assert (pagina["total"], [i["antiguedadMinutos"] for i in pagina["items"]]) == (3, [540, 120])
    resto = await api.get(
        f"{BASE}/bandeja", params={"cursor": pagina["siguienteCursor"]}, headers=SUSCRIPTOR
    )
    assert [i["reglasNoCumplidas"] for i in resto.json()["items"]] == [["SUS-007"]]
    por_motivo = await api.get(
        f"{BASE}/bandeja", params={"motivo": "SUMA_REQUIERE_REVISION"}, headers=SUSCRIPTOR
    )
    assert por_motivo.json()["total"] == 2
    codigo = pagina["items"][0]["codigo"]
    por_codigo = await api.get(f"{BASE}/bandeja", params={"q": codigo}, headers=SUSCRIPTOR)
    assert [i["codigo"] for i in por_codigo.json()["items"]] == [codigo]
    comodin = await api.get(f"{BASE}/bandeja", params={"q": "%"}, headers=SUSCRIPTOR)
    assert comodin.json()["total"] == 0
    dia = {"desde": "2026-10-22T05:00:00Z", "hasta": "2026-10-23T05:00:00Z"}
    resumen = (await api.get(f"{BASE}/resumen", params=dia, headers=SUSCRIPTOR)).json()
    assert (resumen["resueltasAutomaticamente"], resumen["enviadasARevision"]) == (1, 3)
    assert (resumen["fueraDelCompromiso"], resumen["tiempoMedioDecisionMs"] is not None) == (
        1,
        True,
    )


async def test_los_logs_no_tienen_pii(api: Any, sinteticos: Any, telemetria: Any) -> None:
    """SEC-W13-01: ni fecha de nacimiento, ni montos, ni edad en los logs; correlación en cada
    respuesta."""
    respuesta = await _solicitar(api, sinteticos, "S05")
    assert respuesta.headers["X-Correlation-Id"]
    await api.get(f"{BASE}/{respuesta.json()['id']}", headers=SUSCRIPTOR)
    texto = telemetria.texto_logs()
    assert "suscripcion decidida" in texto
    for centinela in ("620000000", "403620", "1968-05-02", '"edad"', "0.61"):
        assert centinela not in texto, centinela
    assert json.dumps("suscripcion.evaluar") in texto


async def test_migracion_adelante_y_atras(base: str) -> None:
    """MT-W13-02: aplicar, revertir (dos veces, idempotente) y volver a aplicar. Las reversas van
    de la más nueva a la más antigua (HU-W15: aceptaciones referencia a las decisiones)."""
    reversas = [
        MIGRACIONES.joinpath("down", nombre).read_text(encoding="utf-8")
        for nombre in ("002_aceptaciones.sql", "001_suscripcion.sql")
    ]
    with psycopg.connect(base, autocommit=True) as conexion:
        for _ in range(2):
            for reversa in reversas:
                conexion.execute(reversa)
    existe = "SELECT to_regclass('decisiones_suscripcion') IS NOT NULL"
    assert _filas(base, existe) == [(False,)]
    migrar(base)
    migrar(base)
    assert _filas(base, existe) == [(True,)]
    assert _filas(base, "SELECT count(*) FROM outbox") == [(0,)]
