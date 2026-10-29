"""API /v1/productos/{productoId}/reglas-fallback (HU-W06) con los repositorios en memoria y el
reloj fijo (2026-10-15)."""

from datetime import UTC, datetime
from typing import Any

from catalogo.domain.fallback.reglas import ReglasFallback
from catalogo.domain.reglas.hash import hash_canonico
from fastapi.testclient import TestClient

BASE = "/v1/productos"


def test_reglas_vigentes_ahora(api: TestClient) -> None:
    respuesta = api.get(f"{BASE}/vida-hipotecario/reglas-fallback/vigente")
    assert respuesta.status_code == 200
    cuerpo: dict[str, Any] = respuesta.json()
    assert (cuerpo["productoId"], cuerpo["version"]) == ("vida-hipotecario", "2026.10.1")
    assert (cuerpo["vigenteDesde"], cuerpo["vigenteHasta"]) == ("2026-10-01T00:00:00Z", None)
    assert cuerpo["hash"] == hash_canonico(cuerpo["definicion"])
    assert [(r["id"], r["fuente"], r["accion"]) for r in cuerpo["definicion"]["reglas"]] == [
        ("FB-001", "FA_PRODUCTOS_VIGENTES", "PENDIENTE"),
        ("FB-002", "FA_HISTORIAL_PAGOS_12M", "PENDIENTE"),
        ("FB-003", "FA_INGRESOS_AGREGADOS", "DEGRADAR"),
        ("FB-004", "DA_REGISTROS_PUBLICOS", "DEGRADAR"),
    ]


def test_vigencia_en_los_limites(api: TestClient) -> None:
    """Vigentes desde 2026-10-01T00:00:00Z inclusive: −1 µs no hay reglas, exacto sí."""
    ruta = f"{BASE}/vida-hipotecario/reglas-fallback/vigente"
    assert api.get(ruta, params={"fecha": "2026-10-01T00:00:00Z"}).json()["version"] == "2026.10.1"
    antes = api.get(ruta, params={"fecha": "2026-09-30T23:59:59.999999Z"})
    assert (antes.status_code, antes.json()["codigo"]) == (404, "REGLAS_NO_VIGENTES")


def test_una_version_vencida_no_es_vigente(api: TestClient, reglas_fallback_repo: Any) -> None:
    """Un permiso temporal: vence en vigenteHasta (semiabierto) y no se vuelve a la anterior."""
    semilla = reglas_fallback_repo.reglas[0]
    reglas_fallback_repo.reglas.append(
        ReglasFallback(
            semilla.producto_id,
            "2026.10.2",
            datetime(2026, 10, 10, tzinfo=UTC),
            datetime(2026, 10, 12, tzinfo=UTC),
            semilla.definicion,
            semilla.hash,
            "prueba",
        )
    )
    ruta = f"{BASE}/vida-hipotecario/reglas-fallback/vigente"
    durante = api.get(ruta, params={"fecha": "2026-10-11T00:00:00Z"}).json()
    assert (durante["version"], durante["vigenteHasta"]) == ("2026.10.2", "2026-10-12T00:00:00Z")
    vencida = api.get(ruta, params={"fecha": "2026-10-12T00:00:00Z"})
    assert (vencida.status_code, vencida.json()["codigo"]) == (404, "REGLAS_NO_VIGENTES")


def test_producto_sin_reglas_de_fallback(api: TestClient) -> None:
    respuesta = api.get(f"{BASE}/vida-hipotecario-2025/reglas-fallback/vigente")
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (404, "REGLAS_NO_VIGENTES")


def test_producto_inexistente(api: TestClient) -> None:
    for ruta in ("vigente", "2026.10.1"):
        respuesta = api.get(f"{BASE}/no-existe/reglas-fallback/{ruta}")
        assert (respuesta.status_code, respuesta.json()["codigo"]) == (
            404,
            "PRODUCTO_NO_ENCONTRADO",
        )


def test_reglas_por_version(api: TestClient) -> None:
    respuesta = api.get(f"{BASE}/vida-hipotecario/reglas-fallback/2026.10.1")
    assert respuesta.status_code == 200
    assert respuesta.json()["version"] == "2026.10.1"
    inexistente = api.get(f"{BASE}/vida-hipotecario/reglas-fallback/2099.01.1")
    assert (inexistente.status_code, inexistente.json()["codigo"]) == (404, "REGLAS_NO_VIGENTES")


def test_parametros_invalidos(api: TestClient) -> None:
    assert api.get(f"{BASE}/vida-hipotecario/reglas-fallback/v1").status_code == 422
    fecha_sin_zona = {"fecha": "2026-10-15T00:00:00"}
    ruta = f"{BASE}/vida-hipotecario/reglas-fallback/vigente"
    assert api.get(ruta, params=fecha_sin_zona).status_code == 422
