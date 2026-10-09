import pytest
from adaptador_datos.domain.consultas.catalogo_fuentes import CATALOGO, PROVEEDOR, claves_proveedor
from adaptador_datos.domain.consultas.modelos import Aliado, Fuente


def test_ut_w10_01_las_cuatro_fuentes_completas() -> None:
    assert set(CATALOGO) == set(Fuente)
    assert set(PROVEEDOR) == set(Aliado)
    for fuente, definicion in CATALOGO.items():
        assert definicion.fuente is fuente
        assert definicion.campos
        assert "{cliente_id}" in definicion.ruta
        for campo in definicion.campos.values():
            assert campo.ruta == tuple(campo.clave_proveedor.split("."))
    assert CATALOGO[Fuente.FA_HISTORIAL_PAGOS_12M].extras == {"months": "12"}
    assert CATALOGO[Fuente.DA_REGISTROS_PUBLICOS].parametro == "campos"
    assert CATALOGO[Fuente.DA_REGISTROS_PUBLICOS].aliado is Aliado.DATOS_ABIERTOS


def test_ut_w10_02_claves_ordenadas_sin_duplicados() -> None:
    campos = [
        "entidades_con_deuda",
        "cuota_mensual_obligaciones",
        "antiguedad_productos_anios",
        "entidades_con_deuda",
    ]
    assert claves_proveedor(Fuente.FA_PRODUCTOS_VIGENTES, campos) == (
        "creditorCount",
        "oldestProductYears",
        "totalMonthlyInstallment",
    )


def test_datos_abiertos_usa_la_clave_de_primer_nivel() -> None:
    assert claves_proveedor(
        Fuente.DA_REGISTROS_PUBLICOS, ["coincidencias_listas_restrictivas"]
    ) == ("listasRestrictivas",)


def test_ut_w10_03_campo_desconocido_lanza_key_error() -> None:
    with pytest.raises(KeyError):
        claves_proveedor(Fuente.FA_HISTORIAL_PAGOS_12M, ["ingreso_mensual_estimado"])
