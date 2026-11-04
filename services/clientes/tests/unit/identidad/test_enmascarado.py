import pytest
from clientes.domain.identidad.enmascarado import enmascarar_documento


@pytest.mark.parametrize(
    ("numero", "esperado"),
    [
        ("1020456789", "1.020.***.789"),
        ("0123456789", "0.123.***.789"),
        ("123456789", "1.234.***.789"),
        ("12345678", "12.***.678"),
        ("123456", "12.***.56"),
        ("12345", "1.***.5"),
        ("123", "***"),
        ("AB12CD345", "AB12***345"),
    ],
)
def test_enmascara_el_numero_dejando_inicio_y_final(numero: str, esperado: str) -> None:
    enmascarado = enmascarar_documento(numero)
    assert enmascarado == esperado
    assert numero not in enmascarado
