"""La póliza en el dominio (HU-W17, 17.6; ADR 0014, decisiones 1 a 3): número, vigencia en
America/Bogota, la tabla de ramos y las transiciones de EMITIENDO a ACTIVA con sus puntos de
control. Pura: sin base ni reloj del sistema.
"""

import dataclasses
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from polizas.domain.poliza.errores import (
    HuellaNoCoincide,
    PolizaInvalida,
    ProductoSinRamo,
    TransicionInvalida,
)
from polizas.domain.poliza.modelos import (
    BOGOTA,
    RAMOS,
    Documento,
    EstadoPoliza,
    Firma,
    OrigenPoliza,
    Poliza,
    Producto,
    Ramo,
    Vigencia,
    numero_poliza,
    ramo_de,
    vigencia_de,
)

BOG = timezone(timedelta(hours=-5))
CREADA = datetime(2026, 9, 13, 14, 0, tzinfo=UTC)
DESPUES = CREADA + timedelta(minutes=5)
SIN_ZONA = CREADA.replace(tzinfo=None)
HUELLA = "sha256:" + "a" * 64
OTRA_HUELLA = "sha256:" + "b" * 64
VIDA = Ramo("100", "Vida hipotecario")


def _origen(**cambios: Any) -> OrigenPoliza:
    base: dict[str, Any] = {
        "aceptacion_id": uuid4(),
        "cotizacion_id": uuid4(),
        "cliente_id": uuid4(),
        "producto": Producto("vida-hipotecario", "2026.09.1"),
        # W17-4: aceptada el 12/09/2026 en Bogotá.
        "aceptada_en": datetime(2026, 9, 12, 10, 0, tzinfo=BOG),
    }
    return OrigenPoliza(**(base | cambios))


def _emitiendo(secuencia: int = 1, **cambios: Any) -> Poliza:
    return Poliza.emitiendo(_origen(**cambios), secuencia=secuencia, ahora=CREADA)


def _documento(**cambios: Any) -> Documento:
    base: dict[str, Any] = {
        "clave": "polizas/100-2026-000001.pdf",
        "version_id": "v-1",
        "huella": HUELLA,
        "tamano_bytes": 2048,
        "fecha_documento": datetime(2026, 9, 13, 14, 1, tzinfo=UTC),
    }
    return Documento(**(base | cambios))


FIRMA = Firma("FIR-SINT-C01", datetime(2026, 1, 1, tzinfo=UTC))


def _firmada() -> Poliza:
    return (
        _emitiendo()
        .registrar_documento(_documento(), DESPUES)
        .registrar_firma(FIRMA, HUELLA, DESPUES)
    )


# --- Tabla de ramos -------------------------------------------------------------------------
def test_vida_hipotecario_es_el_ramo_100() -> None:
    assert ramo_de("vida-hipotecario") == VIDA
    assert dict(RAMOS) == {"vida-hipotecario": VIDA}


def test_la_tabla_de_ramos_no_se_cambia_en_ejecucion() -> None:
    with pytest.raises(TypeError):
        RAMOS["otro"] = VIDA  # type: ignore[index]


@pytest.mark.parametrize("producto", ["vida-deudores", "VIDA-HIPOTECARIO", ""])
def test_un_producto_fuera_de_la_tabla_es_producto_sin_ramo(producto: str) -> None:
    with pytest.raises(ProductoSinRamo) as error:
        ramo_de(producto)
    assert error.value.codigo == "PRODUCTO_SIN_RAMO"
    assert error.value.producto_id == producto


def test_sin_ramo_no_se_crea_la_poliza() -> None:
    with pytest.raises(ProductoSinRamo):
        _emitiendo(producto=Producto("vida-deudores", "1"))


# --- Número ---------------------------------------------------------------------------------
def test_el_numero_es_ramo_anio_y_seis_digitos() -> None:
    assert numero_poliza(VIDA, CREADA, 1) == "100-2026-000001"
    assert numero_poliza(VIDA, CREADA, 999_999) == "100-2026-999999"


def test_el_anio_del_numero_es_el_de_bogota() -> None:
    # 1/1/2027 03:00 UTC es todavía el 31/12/2026 en Bogotá; 05:00 UTC ya es 2027.
    assert numero_poliza(VIDA, datetime(2027, 1, 1, 3, 0, tzinfo=UTC), 7) == "100-2026-000007"
    assert numero_poliza(VIDA, datetime(2027, 1, 1, 5, 0, tzinfo=UTC), 7) == "100-2027-000007"


@pytest.mark.parametrize("secuencia", [0, -1, 1_000_000])
def test_una_secuencia_que_no_cabe_en_seis_digitos_es_invalida(secuencia: int) -> None:
    with pytest.raises(PolizaInvalida, match="secuencia"):
        numero_poliza(VIDA, CREADA, secuencia)


def test_el_numero_sin_zona_es_invalido() -> None:
    with pytest.raises(PolizaInvalida, match="zona"):
        numero_poliza(VIDA, SIN_ZONA, 1)


def test_el_numero_se_reserva_al_crear_la_emision() -> None:
    poliza = _emitiendo(secuencia=42)
    assert poliza.numero == "100-2026-000042"
    assert poliza.ramo == VIDA


# --- Vigencia -------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("aceptada_en", "desde", "hasta"),
    [
        # W17-4: 13/09/2026 - 12/09/2027.
        (datetime(2026, 9, 12, 10, 0, tzinfo=BOG), date(2026, 9, 13), date(2027, 9, 12)),
        # 23:30 en Bogotá: en UTC ya es el día siguiente (04:30 del 13); cuenta el día local.
        (datetime(2026, 9, 13, 4, 30, tzinfo=UTC), date(2026, 9, 13), date(2027, 9, 12)),
        (datetime(2026, 9, 12, 23, 30, tzinfo=BOG), date(2026, 9, 13), date(2027, 9, 12)),
        # 31 de diciembre: empieza el 1 de enero y acaba el 31 de diciembre.
        (datetime(2026, 12, 31, 18, 0, tzinfo=BOG), date(2027, 1, 1), date(2027, 12, 31)),
        # 28 de febrero de un año no bisiesto: hasta el 29 de febrero del bisiesto siguiente.
        (datetime(2027, 2, 28, 9, 0, tzinfo=BOG), date(2027, 3, 1), date(2028, 2, 29)),
        # 28 de febrero de un bisiesto: empieza el 29; su aniversario es el 1 de marzo.
        (datetime(2028, 2, 28, 9, 0, tzinfo=BOG), date(2028, 2, 29), date(2029, 2, 28)),
        # 29 de febrero: empieza el 1 de marzo.
        (datetime(2028, 2, 29, 9, 0, tzinfo=BOG), date(2028, 3, 1), date(2029, 2, 28)),
        # Un año bisiesto completo (366 días, con el 29 de febrero dentro).
        (datetime(2027, 3, 15, 9, 0, tzinfo=BOG), date(2027, 3, 16), date(2028, 3, 15)),
        (datetime(2028, 1, 10, 9, 0, tzinfo=BOG), date(2028, 1, 11), date(2029, 1, 10)),
    ],
    ids=[
        "w17_4",
        "2330_bogota_en_utc",
        "2330_bogota",
        "31_diciembre",
        "28_febrero",
        "28_febrero_bisiesto",
        "29_febrero",
        "bisiesto_marzo",
        "bisiesto_enero",
    ],
)
def test_la_vigencia_va_del_dia_siguiente_al_anterior_al_aniversario(
    aceptada_en: datetime, desde: date, hasta: date
) -> None:
    assert vigencia_de(aceptada_en) == Vigencia(desde, hasta)


def test_la_vigencia_sin_zona_es_invalida() -> None:
    with pytest.raises(PolizaInvalida, match="zona"):
        vigencia_de(SIN_ZONA)


def test_bogota_es_utc_menos_cinco() -> None:
    assert BOGOTA.utcoffset(None) == timedelta(hours=-5)


@pytest.mark.parametrize(
    ("desde", "hasta"),
    [(date(2026, 9, 13), date(2026, 9, 13)), (date(2026, 9, 13), date(2026, 9, 1))],
)
def test_una_vigencia_que_no_avanza_es_invalida(desde: date, hasta: date) -> None:
    with pytest.raises(PolizaInvalida, match="vigencia"):
        Vigencia(desde, hasta)


# --- Creación -------------------------------------------------------------------------------
def test_la_emision_nace_emitiendo_con_los_datos_del_origen() -> None:
    origen = _origen()
    poliza = Poliza.emitiendo(origen, secuencia=1, ahora=CREADA)
    assert isinstance(poliza.poliza_id, UUID)
    assert (poliza.aceptacion_id, poliza.cotizacion_id, poliza.cliente_id) == (
        origen.aceptacion_id,
        origen.cotizacion_id,
        origen.cliente_id,
    )
    assert poliza.producto == Producto("vida-hipotecario", "2026.09.1")
    assert poliza.vigencia == Vigencia(date(2026, 9, 13), date(2027, 9, 12))
    assert poliza.estado is EstadoPoliza.EMITIENDO
    assert (poliza.documento, poliza.firma, poliza.emitida_en, poliza.ultimo_fallo) == (
        None,
        None,
        None,
        None,
    )
    assert poliza.creada_en == poliza.actualizada_en == CREADA


def test_el_id_de_la_poliza_se_puede_fijar() -> None:
    poliza_id = uuid4()
    assert Poliza.emitiendo(
        _origen(), secuencia=1, ahora=CREADA, poliza_id=poliza_id
    ).poliza_id == (poliza_id)


INVALIDAS: dict[str, dict[str, Any]] = {
    "aceptacion_no_uuid": {"aceptacion_id": "no-uuid"},
    "producto_vacio": {"producto": Producto(" ", "1")},
    "version_vacia": {"producto": Producto("vida-hipotecario", "")},
    "estado_texto": {"estado": "ACTIVA"},
    "numero_mal_formado": {"numero": "100-2026-1"},
    "numero_de_otro_ramo": {"numero": "200-2026-000001"},
    "creada_sin_zona": {"creada_en": SIN_ZONA},
    "actualizada_sin_zona": {"actualizada_en": SIN_ZONA},
    "fallo_no_es_codigo": {"ultimo_fallo": "se cayó la firma"},
    "firma_sin_documento": {"firma": FIRMA},
    "emitiendo_con_emitida_en": {"emitida_en": CREADA},
    "activa_sin_documento": {"estado": EstadoPoliza.ACTIVA, "emitida_en": CREADA},
}


@pytest.mark.parametrize("cambios", INVALIDAS.values(), ids=list(INVALIDAS))
def test_una_poliza_que_rompe_una_invariante_es_invalida(cambios: dict[str, Any]) -> None:
    with pytest.raises(PolizaInvalida):
        dataclasses.replace(_emitiendo(), **cambios)


def test_activa_sin_emitida_en_es_invalida() -> None:
    firmada = _firmada()
    with pytest.raises(PolizaInvalida, match="emitida_en"):
        dataclasses.replace(firmada, estado=EstadoPoliza.ACTIVA)


def test_emitida_en_sin_zona_es_invalida() -> None:
    activa = _firmada().activar(DESPUES)
    with pytest.raises(PolizaInvalida, match="emitida_en"):
        dataclasses.replace(activa, emitida_en=SIN_ZONA)


DOCUMENTOS_INVALIDOS: dict[str, dict[str, Any]] = {
    "clave_vacia": {"clave": "  "},
    "version_vacia": {"version_id": ""},
    "huella_mayusculas": {"huella": "sha256:" + "A" * 64},
    "huella_sin_prefijo": {"huella": "a" * 64},
    "tamano_cero": {"tamano_bytes": 0},
    "tamano_bool": {"tamano_bytes": True},
    "fecha_sin_zona": {"fecha_documento": SIN_ZONA},
}


@pytest.mark.parametrize("cambios", DOCUMENTOS_INVALIDOS.values(), ids=list(DOCUMENTOS_INVALIDOS))
def test_un_documento_invalido(cambios: dict[str, Any]) -> None:
    with pytest.raises(PolizaInvalida):
        _documento(**cambios)


@pytest.mark.parametrize(
    "cambios", [{"firma_id": " "}, {"sello_tiempo": SIN_ZONA}], ids=["id", "sello"]
)
def test_una_firma_invalida(cambios: dict[str, Any]) -> None:
    with pytest.raises(PolizaInvalida):
        dataclasses.replace(FIRMA, **cambios)


# --- Transiciones ---------------------------------------------------------------------------
def test_registrar_documento_guarda_el_punto_de_control() -> None:
    poliza = _emitiendo().registrar_documento(_documento(), DESPUES)
    assert poliza.documento == _documento()
    assert poliza.estado is EstadoPoliza.EMITIENDO
    assert poliza.actualizada_en == DESPUES


def test_el_documento_se_registra_una_sola_vez() -> None:
    poliza = _emitiendo().registrar_documento(_documento(), DESPUES)
    with pytest.raises(TransicionInvalida):
        poliza.registrar_documento(_documento(version_id="v-2"), DESPUES)


def test_registrar_firma_guarda_el_punto_de_control() -> None:
    poliza = _firmada()
    assert poliza.firma == FIRMA
    assert poliza.estado is EstadoPoliza.EMITIENDO


def test_la_firma_exige_el_documento() -> None:
    with pytest.raises(TransicionInvalida):
        _emitiendo().registrar_firma(FIRMA, HUELLA, DESPUES)


def test_la_firma_se_registra_una_sola_vez() -> None:
    with pytest.raises(TransicionInvalida):
        _firmada().registrar_firma(Firma("FIR-2", FIRMA.sello_tiempo), HUELLA, DESPUES)


def test_la_huella_firmada_debe_ser_la_del_documento() -> None:
    poliza = _emitiendo().registrar_documento(_documento(), DESPUES)
    with pytest.raises(HuellaNoCoincide) as error:
        poliza.registrar_firma(FIRMA, OTRA_HUELLA, DESPUES)
    assert error.value.codigo == "HUELLA_NO_COINCIDE"
    # El mensaje no lleva las huellas.
    assert HUELLA not in str(error.value) and OTRA_HUELLA not in str(error.value)


def test_activar_fija_emitida_en_con_la_fecha_del_documento() -> None:
    activa = _firmada().activar(DESPUES + timedelta(seconds=1))
    assert activa.estado is EstadoPoliza.ACTIVA
    # La fecha impresa en el PDF es emitidaEn (ADR 0014, decisión 4).
    assert activa.emitida_en == _documento().fecha_documento
    assert activa.actualizada_en == DESPUES + timedelta(seconds=1)


def test_activar_exige_el_documento() -> None:
    with pytest.raises(TransicionInvalida):
        _emitiendo().activar(DESPUES)


def test_activar_exige_la_firma() -> None:
    with pytest.raises(TransicionInvalida):
        _emitiendo().registrar_documento(_documento(), DESPUES).activar(DESPUES)


def test_registrar_fallo_deja_emitiendo_con_el_codigo() -> None:
    poliza = _emitiendo().registrar_fallo("TIEMPO_AGOTADO", DESPUES)
    assert (poliza.estado, poliza.ultimo_fallo, poliza.actualizada_en) == (
        EstadoPoliza.EMITIENDO,
        "TIEMPO_AGOTADO",
        DESPUES,
    )
    # Los puntos de control se conservan tras un fallo.
    firmada = _firmada().registrar_fallo("DEPENDENCIA_NO_DISPONIBLE", DESPUES)
    assert (firmada.documento, firmada.firma) == (_documento(), FIRMA)


def test_un_fallo_que_no_es_codigo_es_invalido() -> None:
    with pytest.raises(PolizaInvalida):
        _emitiendo().registrar_fallo("timeout de 6500 ms", DESPUES)


def test_activar_conserva_el_ultimo_fallo() -> None:
    con_fallo = _firmada().registrar_fallo("TIEMPO_AGOTADO", DESPUES)
    assert con_fallo.activar(DESPUES).ultimo_fallo == "TIEMPO_AGOTADO"


@pytest.mark.parametrize(
    "intento",
    [
        lambda p: p.registrar_documento(_documento(version_id="v-2"), DESPUES),
        lambda p: p.registrar_firma(FIRMA, HUELLA, DESPUES),
        lambda p: p.activar(DESPUES),
        lambda p: p.registrar_fallo("TIEMPO_AGOTADO", DESPUES),
    ],
    ids=["documento", "firma", "activar", "fallo"],
)
def test_todo_intento_sobre_una_activa_es_transicion_invalida(intento: Any) -> None:
    activa = _firmada().activar(DESPUES)
    with pytest.raises(TransicionInvalida) as error:
        intento(activa)
    assert error.value.desde is EstadoPoliza.ACTIVA
