"""HU-W24: la cobertura paramétrica de la cotización (regla del producto de HU-W21 + zona del riesgo).

Un producto paramétrico exige `datosAsegurado.zona` (ZONA_REQUERIDA) y la cotización guarda la
instantánea de la regla y la zona; un producto que no es paramétrico la ignora. La huella de una
solicitud sin zona no cambia, la fila la conserva y los insumos de suscripción la entregan a
`polizas`.
"""

from dataclasses import replace
from typing import Any

import pytest
from cotizacion.adapters.inbound.http.cotizaciones import error_cotizacion
from cotizacion.adapters.inbound.http.suscripcion import _parametrica
from cotizacion.adapters.outbound.cotizaciones_pg import COLUMNAS, cotizacion_de, fila_cotizacion
from cotizacion.adapters.outbound.productos_catalogo import ProductoDto, a_producto
from cotizacion.application.cotizacion.calcular_prima import huella_solicitud
from cotizacion.domain.cotizacion.errores import ZonaRequerida
from cotizacion.domain.cotizacion.modelos import CoberturaParametrica, ReglaParametrica
from cotizacion.domain.cotizacion.parametrico import CAMPO_ZONA, cobertura_parametrica

pytestmark = pytest.mark.anyio

REGLA = ReglaParametrica(
    tipo_evento="LLUVIA_ACUMULADA",
    unidad="mm",
    umbral="120",
    valor_pago="1400000.00",
    version_regla="2026.11.1",
)
ESPERADA = CoberturaParametrica(
    tipo_evento="LLUVIA_ACUMULADA",
    zona="11001",
    umbral="120",
    unidad="mm",
    valor_pago="1400000.00",
    moneda="COP",
    version_regla="2026.11.1",
)


def _parametrico(escenario: Any) -> None:
    productos = escenario.productos.productos
    productos["vida-hipotecario"] = replace(productos["vida-hipotecario"], parametrico=REGLA)


# --- Dominio --------------------------------------------------------------------------------
def test_sin_regla_no_hay_cobertura_aunque_venga_zona(semilla_w03: Any) -> None:
    producto = semilla_w03[0]("vida-hipotecario")
    assert cobertura_parametrica(producto, None) is None
    assert cobertura_parametrica(producto, "11001") is None


@pytest.mark.parametrize("zona", [None, ""])
def test_un_producto_parametrico_exige_la_zona(semilla_w03: Any, zona: str | None) -> None:
    producto = replace(semilla_w03[0]("vida-hipotecario"), parametrico=REGLA)
    with pytest.raises(ZonaRequerida) as error:
        cobertura_parametrica(producto, zona)
    assert error.value.campos == (CAMPO_ZONA,)


def test_la_instantanea_lleva_la_regla_la_zona_y_la_moneda_del_producto(semilla_w03: Any) -> None:
    producto = replace(semilla_w03[0]("vida-hipotecario"), parametrico=REGLA)
    assert cobertura_parametrica(producto, "11001") == ESPERADA


# --- Caso de uso ----------------------------------------------------------------------------
async def test_la_cotizacion_de_un_producto_parametrico_guarda_su_cobertura(
    escenario_cotizacion: Any, nueva_cotizacion: Any
) -> None:
    _parametrico(escenario_cotizacion)
    cotizacion, _, creada = await escenario_cotizacion.caso().ejecutar(
        nueva_cotizacion(zona="11001")
    )
    assert creada
    assert cotizacion.cobertura_parametrica == ESPERADA
    assert escenario_cotizacion.repo.cotizaciones[cotizacion.id].cobertura_parametrica == ESPERADA


async def test_sin_zona_un_producto_parametrico_no_se_cotiza(
    escenario_cotizacion: Any, nueva_cotizacion: Any
) -> None:
    _parametrico(escenario_cotizacion)
    with pytest.raises(ZonaRequerida):
        await escenario_cotizacion.caso().ejecutar(nueva_cotizacion())
    assert escenario_cotizacion.repo.cotizaciones == {}


async def test_un_producto_no_parametrico_ignora_la_zona(
    escenario_cotizacion: Any, nueva_cotizacion: Any
) -> None:
    cotizacion, _, _ = await escenario_cotizacion.caso().ejecutar(nueva_cotizacion(zona="11001"))
    assert cotizacion.cobertura_parametrica is None


def test_la_huella_sin_zona_no_cambia_y_con_zona_si(nueva_cotizacion: Any) -> None:
    sin_zona = nueva_cotizacion()
    assert huella_solicitud(sin_zona) == huella_solicitud(nueva_cotizacion(zona=None))
    assert huella_solicitud(nueva_cotizacion(zona="11001")) != huella_solicitud(sin_zona)
    assert huella_solicitud(nueva_cotizacion(zona="11001")) != huella_solicitud(
        nueva_cotizacion(zona="05001")
    )


def test_zona_requerida_es_un_422_con_el_campo() -> None:
    error = error_cotizacion(ZonaRequerida((CAMPO_ZONA,)))
    assert (error.codigo, error.estado_http) == ("ZONA_REQUERIDA", 422)
    assert error.detalles == {"campos": ["datosAsegurado.zona"]}


# --- Adaptadores ----------------------------------------------------------------------------
def test_el_producto_del_catalogo_trae_su_regla_parametrica() -> None:
    base = {
        "id": "clima-lluvia",
        "version": "2026.11.1",
        "nombre": "Clima lluvia",
        "ramo": "250",
        "pais": "CO",
        "moneda": "COP",
        "estado": "ACTIVO",
        "vigenteDesde": "2026-09-01T00:00:00Z",
        "vigenteHasta": None,
        "coberturas": [{"codigo": "LLUVIA_ACUMULADA", "nombre": "Lluvia", "obligatoria": True}],
    }
    parametrico = {
        "tipoEvento": "LLUVIA_ACUMULADA",
        "unidad": "mm",
        "umbral": "120",
        "valorPago": "1400000.00",
        "versionRegla": "2026.11.1",
    }
    assert a_producto(ProductoDto.model_validate(base)).parametrico is None
    con_regla = a_producto(ProductoDto.model_validate({**base, "parametrico": parametrico}))
    assert con_regla.parametrico == REGLA


async def test_la_fila_conserva_la_cobertura_parametrica(
    escenario_cotizacion: Any, nueva_cotizacion: Any
) -> None:
    _parametrico(escenario_cotizacion)
    cotizacion, _, _ = await escenario_cotizacion.caso().ejecutar(nueva_cotizacion(zona="11001"))
    fila = fila_cotizacion(cotizacion)
    assert set(fila) == set(COLUMNAS)
    assert fila["cobertura_parametrica"].obj == {
        "tipoEvento": "LLUVIA_ACUMULADA",
        "zona": "11001",
        "umbral": "120",
        "unidad": "mm",
        "valorPago": "1400000.00",
        "moneda": "COP",
        "versionRegla": "2026.11.1",
    }
    leida = {k: (v.obj if hasattr(v, "obj") else v) for k, v in fila.items()}
    leida |= {"oferta_id": None, "decision_id": None}
    assert cotizacion_de(leida).cobertura_parametrica == ESPERADA


async def test_una_fila_anterior_sin_la_columna_vuelve_sin_cobertura(
    escenario_cotizacion: Any, nueva_cotizacion: Any
) -> None:
    cotizacion, _, _ = await escenario_cotizacion.caso().ejecutar(nueva_cotizacion())
    fila = {k: (v.obj if hasattr(v, "obj") else v) for k, v in fila_cotizacion(cotizacion).items()}
    fila.pop("cobertura_parametrica")
    fila |= {"oferta_id": None, "decision_id": None}
    assert cotizacion_de(fila).cobertura_parametrica is None


def test_los_insumos_entregan_la_cobertura_a_polizas() -> None:
    dto = _parametrica(ESPERADA)
    assert dto is not None
    assert dto.model_dump(by_alias=True) == {
        "tipoEvento": "LLUVIA_ACUMULADA",
        "zona": "11001",
        "umbral": "120",
        "unidad": "mm",
        "valorPago": "1400000.00",
        "moneda": "COP",
        "versionRegla": "2026.11.1",
    }
    assert _parametrica(None) is None
