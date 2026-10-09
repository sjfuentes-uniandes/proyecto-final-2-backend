from adaptador_datos.main import app
from fastapi.testclient import TestClient


def test_health() -> None:
    assert TestClient(app).get("/health").json() == {"status": "ok"}
