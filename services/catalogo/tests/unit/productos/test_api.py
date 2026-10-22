"""API /v1/productos (HU-W03) con el repositorio en memoria y el reloj fijo (2026-10-15)."""

from typing import Any

from catalogo.domain.reglas.hash import hash_canonico
from fastapi.testclient import TestClient

BASE = "/v1/productos"


def test_producto_vigente_con_coberturas(api: TestClient) -> None:
    respuesta = api.get(f"{BASE}/vida-hipotecario")
    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["moneda"] == "COP"
    assert cuerpo["estado"] == "ACTIVO"
    assert cuerpo["vigenteHasta"] is None
    assert [c["codigo"] for c in cuerpo["coberturas"] if not c["obligatoria"]] == [
        "ENFERMEDADES_GRAVES",
        "AUXILIO_FUNERARIO",
    ]


def test_producto_vencido_se_entrega_tal_cual(api: TestClient) -> None:
    """catalogo no decide la vigencia del producto: la decide quien cotiza."""
    cuerpo = api.get(f"{BASE}/vida-hipotecario-2025").json()
    assert cuerpo["vigenteHasta"] == "2026-09-01T00:00:00Z"


def test_producto_inexistente(api: TestClient) -> None:
    respuesta = api.get(f"{BASE}/no-existe")
    assert respuesta.status_code == 404
    assert respuesta.json()["codigo"] == "PRODUCTO_NO_ENCONTRADO"


def test_reglas_vigentes_ahora_y_en_una_fecha(api: TestClient) -> None:
    ahora = api.get(f"{BASE}/vida-hipotecario/reglas-prima/vigente")
    assert ahora.status_code == 200
    cuerpo: dict[str, Any] = ahora.json()
    assert cuerpo["version"] == "2026.09.1"
    assert cuerpo["hash"] == hash_canonico(cuerpo["definicion"])
    # Límite exacto: vigente desde 2026-09-01T00:00:00Z inclusive.
    exacta = api.get(
        f"{BASE}/vida-hipotecario/reglas-prima/vigente", params={"fecha": "2026-09-01T00:00:00Z"}
    )
    assert exacta.json()["version"] == "2026.09.1"


def test_reglas_antes_de_su_vigencia(api: TestClient) -> None:
    respuesta = api.get(
        f"{BASE}/vida-hipotecario/reglas-prima/vigente",
        params={"fecha": "2026-08-31T23:59:59.999999Z"},
    )
    assert respuesta.status_code == 404
    assert respuesta.json()["codigo"] == "REGLAS_NO_VIGENTES"


def test_reglas_de_un_producto_inexistente(api: TestClient) -> None:
    respuesta = api.get(f"{BASE}/no-existe/reglas-prima/vigente")
    assert respuesta.status_code == 404
    assert respuesta.json()["codigo"] == "PRODUCTO_NO_ENCONTRADO"


def test_reglas_por_version(api: TestClient) -> None:
    respuesta = api.get(f"{BASE}/vida-hipotecario-2025/reglas-prima/2025.01.1")
    assert respuesta.status_code == 200
    assert respuesta.json()["definicion"]["recargos"] == []
    sin_version = api.get(f"{BASE}/vida-hipotecario/reglas-prima/2030.01.1")
    assert (sin_version.status_code, sin_version.json()["codigo"]) == (404, "REGLAS_NO_VIGENTES")
    sin_producto = api.get(f"{BASE}/no-existe/reglas-prima/2030.01.1")
    assert sin_producto.json()["codigo"] == "PRODUCTO_NO_ENCONTRADO"


def test_parametros_invalidos(api: TestClient) -> None:
    assert api.get(f"{BASE}/Mayusculas").status_code == 422
    assert api.get(f"{BASE}/vida-hipotecario/reglas-prima/v1").status_code == 422
    sin_zona = api.get(
        f"{BASE}/vida-hipotecario/reglas-prima/vigente", params={"fecha": "2026-10-01T00:00:00"}
    )
    assert sin_zona.status_code == 422
