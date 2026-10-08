from fastapi.testclient import TestClient

from bff_movil.main import app


def test_health() -> None:
    assert TestClient(app).get("/health").json() == {"status": "ok"}
