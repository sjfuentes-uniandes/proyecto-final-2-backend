import re
from unittest.mock import MagicMock, patch

import pytest
from consulta.adapters.outbound import semilla_polizas
from consulta.config import Settings

NUMERO = re.compile(r"^[0-9]{3}-[0-9]{4}-[0-9]{6}$")


def test_semilla_tiene_polizas_validas() -> None:
    polizas = semilla_polizas.cargar()
    assert polizas
    assert len({p["polizaId"] for p in polizas}) == len(polizas)
    assert all(NUMERO.match(str(p["numero"])) for p in polizas)


def test_sembrar_inserta_y_cuenta_creadas() -> None:
    conexion = MagicMock()
    conexion.execute.return_value.rowcount = 1
    with patch.object(semilla_polizas.psycopg, "connect") as connect:
        connect.return_value.__enter__.return_value = conexion
        creadas = semilla_polizas.sembrar("postgresql://x")
    assert creadas == len(semilla_polizas.cargar())
    assert "ON CONFLICT DO NOTHING" in conexion.execute.call_args.args[0]


def test_semilla_no_se_permite_fuera_de_int_y_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONSULTA_SEMILLA_POLIZAS", "true")
    monkeypatch.setenv("ENVIRONMENT", "prod")
    with pytest.raises(ValueError, match="CONSULTA_SEMILLA_POLIZAS"):
        Settings()
    monkeypatch.setenv("ENVIRONMENT", "int")
    assert Settings().semilla_polizas is True
