"""La semilla de QA (R7) aplicada a PostgreSQL y S3 reales: cada caso cumple lo que declara su
`esperado` y el resumen cuadra con `esperadoResumen` (HU-W19, DS3-07)."""

import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from siniestros.adapters.outbound.semilla_qa import cargar_semilla, clave_objeto
from siniestros.config import Settings
from siniestros.main import crear_app

SEMILLA = cargar_semilla()
ANALISTA = SEMILLA["analistaQa"]["analistaId"]
CABECERAS = {
    "X-Actor-Id": ANALISTA,
    "X-Actor-Rol": "ANALISTA_SINIESTROS",
    "X-Actor-Nombre": "Luis Mora",
}
OTRO = {**CABECERAS, "X-Actor-Id": "a0a0a0a0-0000-4000-8000-000000000002"}


@pytest.fixture
def cliente(
    limpia: str, localstack: str, bucket: str, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    for nombre, valor in {
        "SOLVENTA_DATABASE_URL": limpia,
        "EVIDENCIAS_BUCKET": bucket,
        "SOLVENTA_AWS_ENDPOINT_URL": localstack,
        "SOLVENTA_SEMILLA_SINIESTROS_QA": "true",
        "SOLVENTA_S3_TIMEOUT_MS": "3000",
    }.items():
        monkeypatch.setenv(nombre, valor)
    with TestClient(crear_app(Settings())) as aplicacion:
        limite = time.monotonic() + 30
        while time.monotonic() < limite:
            if (
                aplicacion.get("/v1/siniestros/resumen", params=_dia(), headers=OTRO)
                .json()
                .get("sinAsignar")
                == 3
                and aplicacion.app.state.semilla_qa.done()
            ):  # type: ignore[attr-defined]
                break
            time.sleep(0.3)
        yield aplicacion


def _dia() -> dict[str, str]:
    ahora = datetime.now(UTC)
    return {
        "desde": (ahora - timedelta(hours=10)).isoformat(),
        "hasta": (ahora + timedelta(hours=14)).isoformat(),
    }


def test_el_resumen_cuadra_con_lo_esperado(cliente: TestClient) -> None:
    esperado = SEMILLA["esperadoResumen"]
    resumen = cliente.get("/v1/siniestros/resumen", params=_dia(), headers=CABECERAS).json()
    for campo in (
        "pendientes",
        "fueraDeCompromiso",
        "asignadosAlActor",
        "sinAsignar",
        "resueltosSemana",
        "resueltosSemanaPrevia",
        "compromisoHoras",
    ):
        assert resumen[campo] == esperado[campo], campo
    assert resumen["tiempoMedioDecisionHoras"] is None


@pytest.mark.parametrize("caso", SEMILLA["casos"], ids=lambda c: c["clave"])
def test_cada_caso_cumple_lo_que_declara(cliente: TestClient, caso: dict[str, Any]) -> None:
    respuesta = cliente.get(f"/v1/siniestros/{caso['id']}", headers=CABECERAS)
    assert respuesta.status_code == 200
    detalle = respuesta.json()
    esperado = caso["esperado"]
    assert detalle["codigo"] == caso["codigo"]
    assert detalle["estado"] == caso["estado"]
    assert detalle["prioridad"] == esperado["prioridad"]
    # La semilla se aplicó segundos antes: el tiempo restante cambia a lo sumo unos minutos.
    assert abs(detalle["tiempoRestanteMin"] - esperado["tiempoRestanteMinAlSembrar"]) <= 3
    assert {v["codigo"]: v["resultado"] for v in detalle["verificacionCobertura"]} == esperado[
        "verificacionCobertura"
    ]
    assert detalle["coherencia"]["resultado"] == esperado["coherencia"]
    assert [e["integridad"] for e in detalle["evidencias"]] == esperado[
        "integridadEvidencias"
    ] or sorted(e["integridad"] for e in detalle["evidencias"]) == sorted(
        esperado["integridadEvidencias"]
    )
    assert [p["tipo"] for p in detalle["soportesPendientes"]] == esperado["soportesPendientes"]


def test_la_bandeja_del_analista_qa_y_la_cola(cliente: TestClient) -> None:
    mia = cliente.get("/v1/siniestros", headers=CABECERAS).json()
    assert mia["items"][0]["codigo"] == "88-2026-0439"  # el vencido por 2 h, primero
    assert mia["total"] == 5
    assert mia["items"][0]["tiempoRestanteMin"] < 0
    cola = cliente.get(
        "/v1/siniestros", params={"bandeja": "SIN_ASIGNAR"}, headers=CABECERAS
    ).json()
    # El más antiguo primero: SIN-03 se reportó hace 30 h, SIN-01 hace 6.
    assert [i["codigo"] for i in cola["items"]] == ["88-2026-0436", "88-2026-0433", "88-2026-0431"]
    busqueda = cliente.get("/v1/siniestros", params={"q": "0442"}, headers=CABECERAS).json()
    assert [i["codigo"] for i in busqueda["items"]] == ["88-2026-0442"]


def test_los_archivos_se_pueden_abrir_menos_el_ausente(cliente: TestClient) -> None:
    eva = next(c for c in SEMILLA["casos"] if c["clave"] == "EVA-01")
    foto = eva["evidencias"][0]
    enlace = cliente.post(
        f"/v1/siniestros/{eva['id']}/evidencias/{foto['id']}/enlace", headers=CABECERAS
    )
    assert enlace.status_code == 200
    assert httpx.get(enlace.json()["url"]).headers["content-type"] == "image/png"
    aus = next(c for c in SEMILLA["casos"] if c["clave"] == "AUS-01")
    video = next(e for e in aus["evidencias"] if e["objetoSubido"] is None)
    ausente = cliente.post(
        f"/v1/siniestros/{aus['id']}/evidencias/{video['id']}/enlace", headers=CABECERAS
    )
    assert (ausente.status_code, ausente.json()["codigo"]) == (404, "EVIDENCIA_NO_DISPONIBLE")


def test_los_objetos_se_subieron_con_checksum_y_cifrado(
    cliente: TestClient, s3: Any, bucket: str
) -> None:
    eva = next(c for c in SEMILLA["casos"] if c["clave"] == "EVA-01")
    foto = eva["evidencias"][0]
    cabecera = s3.head_object(
        Bucket=bucket, Key=clave_objeto(eva["id"], foto["id"]), ChecksumMode="ENABLED"
    )
    assert cabecera["ServerSideEncryption"] == "AES256"
    assert cabecera["ChecksumSHA256"]
    claves = [o["Key"] for o in s3.list_objects_v2(Bucket=bucket)["Contents"]]
    assert all(k.startswith("siniestros/") for k in claves)
    assert len(claves) == 29


def test_repetir_la_semilla_no_duplica_nada(cliente: TestClient, limpia: str) -> None:
    from siniestros.adapters.outbound.semilla_qa import sembrar

    antes = _contar(limpia)
    pool_url = limpia

    async def repetir() -> None:
        from psycopg_pool import AsyncConnectionPool

        async with AsyncConnectionPool(pool_url, min_size=1, max_size=2, open=False) as pool:
            await sembrar(pool, None, None, "AES256", datetime.now(UTC) + timedelta(days=2))

    import asyncio

    asyncio.run(repetir())
    assert (
        _contar(limpia) == antes == {"siniestros": 8, "evidencias": 30, "historial_siniestro": 21}
    )


def _contar(url: str) -> dict[str, int]:
    with psycopg.connect(url, autocommit=True) as conexion:
        return {
            tabla: conexion.execute(f"SELECT count(*) FROM {tabla}").fetchone()[0]  # type: ignore[index]
            for tabla in ("siniestros", "evidencias", "historial_siniestro")
        }
