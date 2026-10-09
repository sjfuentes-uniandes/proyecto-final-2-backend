import copy
from decimal import Decimal
from typing import Any

import pytest
from adaptador_datos.domain.consultas.catalogo_fuentes import CATALOGO
from adaptador_datos.domain.consultas.modelos import Fuente, RespuestaInvalida, ValorCanonico
from adaptador_datos.domain.consultas.proyeccion import proyectar

P, H, I, D = (
    Fuente.FA_PRODUCTOS_VIGENTES,
    Fuente.FA_HISTORIAL_PAGOS_12M,
    Fuente.FA_INGRESOS_AGREGADOS,
    Fuente.DA_REGISTROS_PUBLICOS,
)

NOMINAL: dict[Fuente, dict[str, Any]] = {
    P: {
        "schemaVersion": "3.1",
        "confidenceScore": 0.95,
        "data": {
            "oldestProductYears": 7,
            "totalMonthlyInstallment": {"amount": "1800000.00", "currency": "COP"},
            "creditorCount": 2,
        },
    },
    H: {"schemaVersion": "3.1", "confidenceScore": 0.91, "data": {"lateCount": 0}},
    I: {
        "schemaVersion": "3.1",
        "confidenceScore": 0.93,
        "data": {"estimatedMonthlyIncome": {"amount": "6500000.00", "currency": "COP"}},
    },
    D: {
        "version": "1.8",
        "nivelConfianza": 0.88,
        "registro": {"listasRestrictivas": {"coincidencias": 0}},
    },
}
ESPERADO = {
    P: {
        "antiguedad_productos_anios": Decimal(7),
        "cuota_mensual_obligaciones": Decimal("1800000.00"),
        "entidades_con_deuda": Decimal(2),
    },
    H: {"moras_12m": Decimal(0)},
    I: {"ingreso_mensual_estimado": Decimal("6500000.00")},
    D: {"coincidencias_listas_restrictivas": Decimal(0)},
}
VERSION_CONFIANZA = {P: ("3.1", "0.95"), H: ("3.1", "0.91"), I: ("3.1", "0.93"), D: ("1.8", "0.88")}
EXTRAS = {
    "accountNumbers": ["SIN-CTA-0004"],
    "fullName": "Cliente Sintético 04",
    "transactions": [{"amount": "123.00"}],
}


def _nominal(fuente: Fuente) -> dict[str, Any]:
    return copy.deepcopy(NOMINAL[fuente])


def _todos(fuente: Fuente) -> list[str]:
    return list(CATALOGO[fuente].campos)


@pytest.mark.parametrize("fuente", list(Fuente))
def test_ut_w10_04_nominal_de_cada_fuente(fuente: Fuente) -> None:
    campos, descartados, version, confianza = proyectar(fuente, _todos(fuente), _nominal(fuente))
    assert {c: v.valor for c, v in campos.items()} == ESPERADO[fuente]
    assert all(v.estado == "DISPONIBLE" for v in campos.values())
    assert descartados == 0
    assert (version, confianza) == (
        VERSION_CONFIANZA[fuente][0],
        Decimal(VERSION_CONFIANZA[fuente][1]),
    )


def test_ut_w10_05_campos_adicionales_se_descartan() -> None:
    cuerpo = _nominal(P)
    cuerpo["data"].update(copy.deepcopy(EXTRAS))
    campos, descartados, _, _ = proyectar(P, _todos(P), cuerpo)
    assert descartados == 3
    assert set(campos) == set(ESPERADO[P])
    texto = repr(campos) + str(campos)
    for centinela in ("SIN-CTA-0004", "Cliente Sintético 04", "123.00", "accountNumbers"):
        assert centinela not in texto


@pytest.mark.parametrize(
    ("fuente", "cambio"),
    [
        (H, lambda c: c["data"].pop("lateCount")),
        (H, lambda c: c["data"].update(lateCount=None)),
        (D, lambda c: c["registro"].pop("listasRestrictivas")),
        (D, lambda c: c["registro"].update(listasRestrictivas=None)),
        (D, lambda c: c["registro"]["listasRestrictivas"].pop("coincidencias")),
    ],
)
def test_ut_w10_06_campo_ausente_o_nulo_no_disponible(fuente: Fuente, cambio: Any) -> None:
    cuerpo = _nominal(fuente)
    cambio(cuerpo)
    campos, _, _, _ = proyectar(fuente, _todos(fuente), cuerpo)
    assert list(campos.values()) == [ValorCanonico(estado="NO_DISPONIBLE", valor=None)]


@pytest.mark.parametrize(
    ("fuente", "cuerpo"),
    [
        (H, "no-es-objeto"),
        (H, {"schemaVersion": "3.1", "confidenceScore": 0.91}),
        (H, {"schemaVersion": "3.1", "confidenceScore": 0.91, "data": "no-es-objeto"}),
        (H, {"schemaVersion": 3.1, "confidenceScore": 0.91, "data": {}}),
        (H, {"schemaVersion": "3.1", "data": {"lateCount": 0}}),
        (D, {"version": "1.8", "nivelConfianza": 0.88, "registro": ["x"]}),
        (D, {"version": "1.8", "nivelConfianza": 0.88, "registro": {"listasRestrictivas": 5}}),
    ],
)
def test_ut_w10_07_sobre_invalido(fuente: Fuente, cuerpo: Any) -> None:
    with pytest.raises(RespuestaInvalida):
        proyectar(fuente, _todos(fuente), cuerpo)


@pytest.mark.parametrize("confianza", [-0.01, 1.01, True, "0.9"])
def test_ut_w10_08_confianza_fuera_de_rango(confianza: Any) -> None:
    cuerpo = _nominal(H)
    cuerpo["confidenceScore"] = confianza
    with pytest.raises(RespuestaInvalida) as error:
        proyectar(H, _todos(H), cuerpo)
    assert error.value.motivo == "SOBRE"


def test_confianza_en_los_extremos_es_valida() -> None:
    for valor, esperado in ((0, Decimal(0)), (1, Decimal(1))):
        cuerpo = _nominal(H)
        cuerpo["confidenceScore"] = valor
        assert proyectar(H, _todos(H), cuerpo)[3] == esperado


@pytest.mark.parametrize(
    "dinero", [{"amount": "1800000.00", "currency": "USD"}, {"amount": "1"}, "1800000"]
)
def test_ut_w10_09_moneda_distinta_de_cop(dinero: Any) -> None:
    cuerpo = _nominal(P)
    cuerpo["data"]["totalMonthlyInstallment"] = dinero
    with pytest.raises(RespuestaInvalida) as error:
        proyectar(P, _todos(P), cuerpo)
    assert error.value.motivo == "CAMPO"


@pytest.mark.parametrize(
    ("fuente", "clave", "monto"),
    [
        (I, "estimatedMonthlyIncome", "abc"),
        (I, "estimatedMonthlyIncome", "NaN"),
        (I, "estimatedMonthlyIncome", 6500000),
        (I, "estimatedMonthlyIncome", "0"),
        (I, "estimatedMonthlyIncome", "-1"),
        (P, "totalMonthlyInstallment", "-0.01"),
    ],
)
def test_ut_w10_10_monto_no_decimal_o_fuera_de_rango(
    fuente: Fuente, clave: str, monto: Any
) -> None:
    cuerpo = _nominal(fuente)
    cuerpo["data"][clave] = {"amount": monto, "currency": "COP"}
    with pytest.raises(RespuestaInvalida):
        proyectar(fuente, _todos(fuente), cuerpo)


def test_cuota_cero_es_valida() -> None:
    cuerpo = _nominal(P)
    cuerpo["data"]["totalMonthlyInstallment"] = {"amount": "0", "currency": "COP"}
    assert proyectar(P, _todos(P), cuerpo)[0]["cuota_mensual_obligaciones"].valor == Decimal(0)


@pytest.mark.parametrize("valor", [True, -1, 1.5, "0"])
def test_ut_w10_11_entero_invalido(valor: Any) -> None:
    cuerpo = _nominal(H)
    cuerpo["data"]["lateCount"] = valor
    with pytest.raises(RespuestaInvalida):
        proyectar(H, _todos(H), cuerpo)


def test_ut_w10_12_solo_se_proyectan_los_autorizados() -> None:
    campos, descartados, _, _ = proyectar(P, ["cuota_mensual_obligaciones"], _nominal(P))
    assert set(campos) == {"cuota_mensual_obligaciones"}
    assert descartados == 2
    assert "creditorCount" not in repr(campos)
