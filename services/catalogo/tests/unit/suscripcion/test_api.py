"""API /v1/productos/{productoId}/reglas-suscripcion (HU-W13) con los repositorios en memoria y el
reloj fijo (2026-10-15)."""

from typing import Any

from catalogo.domain.reglas.hash import hash_canonico
from fastapi.testclient import TestClient

BASE = "/v1/productos"


def test_reglas_vigentes_ahora(api: TestClient) -> None:
    respuesta = api.get(f"{BASE}/vida-hipotecario/reglas-suscripcion/vigente")
    assert respuesta.status_code == 200
    cuerpo: dict[str, Any] = respuesta.json()
    assert (cuerpo["productoId"], cuerpo["version"]) == ("vida-hipotecario", "2026.10.1")
    assert cuerpo["vigenteDesde"] == "2026-10-01T00:00:00Z"
    assert cuerpo["hash"] == hash_canonico(cuerpo["definicion"])
    reglas = cuerpo["definicion"]["reglas"]
    assert [(r["codigo"], r["tipo"]) for r in reglas] == [
        ("SUS-001", "EXCLUYENTE"),
        ("SUS-004", "REVISION"),
        ("SUS-007", "REVISION"),
        ("SUS-011", "REVISION"),
        ("SUS-015", "EXCLUYENTE"),
        ("SUS-019", "EXCLUYENTE"),
    ]
    reservadas = [r["codigo"] for r in reglas if r["visibilidad"] == "RESERVADA"]
    assert reservadas == ["SUS-015"]
    assert [a["productoId"] for a in cuerpo["definicion"]["alternativas"]] == [
        "vida-senior",
        "proteccion-pagos",
        "auxilio-funerario",
    ]


def test_vigencia_en_los_limites(api: TestClient) -> None:
    """Vigentes desde 2026-10-01T00:00:00Z inclusive: −1 µs no hay reglas, exacto sí."""
    ruta = f"{BASE}/vida-hipotecario/reglas-suscripcion/vigente"
    exacta = api.get(ruta, params={"fecha": "2026-10-01T00:00:00Z"})
    assert exacta.json()["version"] == "2026.10.1"
    antes = api.get(ruta, params={"fecha": "2026-09-30T23:59:59.999999Z"})
    assert (antes.status_code, antes.json()["codigo"]) == (404, "REGLAS_NO_VIGENTES")


def test_producto_sin_reglas_de_suscripcion(api: TestClient) -> None:
    respuesta = api.get(f"{BASE}/vida-hipotecario-2025/reglas-suscripcion/vigente")
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (404, "REGLAS_NO_VIGENTES")


def test_producto_inexistente(api: TestClient) -> None:
    for ruta in ("vigente", "2026.10.1"):
        respuesta = api.get(f"{BASE}/no-existe/reglas-suscripcion/{ruta}")
        assert (respuesta.status_code, respuesta.json()["codigo"]) == (
            404,
            "PRODUCTO_NO_ENCONTRADO",
        )


def test_reglas_por_version(api: TestClient) -> None:
    respuesta = api.get(f"{BASE}/vida-hipotecario/reglas-suscripcion/2026.10.1")
    assert respuesta.status_code == 200
    vigente = api.get(f"{BASE}/vida-hipotecario/reglas-suscripcion/vigente").json()
    assert respuesta.json() == vigente
    sin_version = api.get(f"{BASE}/vida-hipotecario/reglas-suscripcion/2030.01.1")
    assert (sin_version.status_code, sin_version.json()["codigo"]) == (404, "REGLAS_NO_VIGENTES")


def test_parametros_invalidos(api: TestClient) -> None:
    assert api.get(f"{BASE}/Mayusculas/reglas-suscripcion/vigente").status_code == 422
    version_mala = api.get(f"{BASE}/vida-hipotecario/reglas-suscripcion/v1")
    assert (version_mala.status_code, version_mala.json()["codigo"]) == (422, "SOLICITUD_INVALIDA")
    sin_zona = api.get(
        f"{BASE}/vida-hipotecario/reglas-suscripcion/vigente",
        params={"fecha": "2026-10-01T00:00:00"},
    )
    assert sin_zona.status_code == 422
