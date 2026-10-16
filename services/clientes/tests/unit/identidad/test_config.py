import pytest
from clientes.config import SECRETO_LOCAL, Settings
from pydantic import ValidationError

SECRETO = "secreto-sintetico-de-prueba-0123456789"


def test_en_local_usa_el_secreto_de_desarrollo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REGISTRO_TOKEN_SECRET", raising=False)
    monkeypatch.delenv("SOLVENTA_REGISTRO_TOKEN_SECRET", raising=False)

    assert Settings().secreto_registro == SECRETO_LOCAL


@pytest.mark.parametrize("nombre", ["REGISTRO_TOKEN_SECRET", "SOLVENTA_REGISTRO_TOKEN_SECRET"])
def test_acepta_el_nombre_de_la_infra_y_el_de_los_demas_servicios(
    monkeypatch: pytest.MonkeyPatch, nombre: str
) -> None:
    monkeypatch.setenv(nombre, SECRETO)

    assert Settings().secreto_registro == SECRETO


def test_fuera_de_local_el_secreto_es_obligatorio(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REGISTRO_TOKEN_SECRET", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "int")

    with pytest.raises(ValidationError) as error:
        Settings()

    assert "REGISTRO_TOKEN_SECRET" in str(error.value)


def test_el_secreto_no_aparece_en_repr_ni_en_errores(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REGISTRO_TOKEN_SECRET", SECRETO)

    assert SECRETO not in repr(Settings()) and SECRETO not in str(Settings())
    monkeypatch.setenv("REGISTRO_VINCULACION_GRACIA_S", "no-es-un-numero")
    monkeypatch.setenv("SOLVENTA_REGISTRO_VINCULACION_GRACIA_S", "no-es-un-numero")
    with pytest.raises(ValidationError) as error:
        Settings()
    assert SECRETO not in str(error.value)
