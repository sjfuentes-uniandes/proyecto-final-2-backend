"""El PDF de la póliza (HU-W17, 17.7; ADR 0014, decisión 4; plan §b, decisión 6): `reportlab` con
`invariant=1`, determinista y sin depender del reloj ni del locale.

Las páginas van sin comprimir, así que el texto se lee de los bytes sin un lector de PDF: cada
`(...) Tj` del contenido, con los escapes de PDF y en WinAnsi (cp1252), que es la codificación de
Helvetica en reportlab.
"""

import dataclasses
import hashlib
import os
import re
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from polizas.adapters.outbound.documento_pdf import GeneradorPdf, fecha_es, monto_es
from polizas.domain.poliza.documento import DatosDocumento
from polizas.domain.poliza.modelos import Producto, Ramo, Vigencia
from polizas.domain.suscripcion.modelos import Cobertura

BOG = timezone(timedelta(hours=-5))
CLIENTE = UUID("00000000-0000-4000-8000-000000000001")
# 09:05 del 13/09/2026 en Bogotá.
FECHA_DOCUMENTO = datetime(2026, 9, 13, 14, 5, tzinfo=UTC)
DATOS = DatosDocumento(
    numero="100-2026-000001",
    ramo=Ramo("100", "Vida hipotecario"),
    producto=Producto("vida-hipotecario", "2026.09.1"),
    vigencia=Vigencia(date(2026, 9, 13), date(2027, 9, 12)),
    coberturas=(
        Cobertura("MUERTE", "Muerte por cualquier causa", True),
        Cobertura("INCAPACIDAD_TOTAL_PERMANENTE", "Incapacidad total y permanente", True),
    ),
    suma_asegurada=Decimal(180000000),
    prima=Decimal(64638),
    periodicidad="MENSUAL",
    moneda="COP",
    cliente_id=CLIENTE,
    socio_id=None,
    aceptada_en=datetime(2026, 9, 12, 10, 0, tzinfo=BOG),
    transaccion_id="TX-SINTETICA-0001",
    version_reglas_prima="2026.09.1",
    version_reglas_suscripcion="2026.10.1",
    fecha_documento=FECHA_DOCUMENTO,
)
LINEAS = [
    "Solventa",
    "Póliza de seguro · Vida hipotecario",
    "Póliza N.º 100-2026-000001",
    "Fecha del documento: 13/09/2026",
    "Datos de la póliza",
    "Ramo: 100 · Vida hipotecario",
    "Producto: vida-hipotecario, versión 2026.09.1",
    "Vigencia: del 13/09/2026 al 12/09/2027 (hora de Colombia)",
    "Asegurado (identificador del cliente): 00000000-0000-4000-8000-000000000001",
    "Coberturas",
    "- Muerte por cualquier causa (obligatoria)",
    "- Incapacidad total y permanente (obligatoria)",
    "Valores en COP",
    "Suma asegurada: $ 180.000.000",
    "Prima mensual: $ 64.638",
    "Contratación",
    "Oferta aceptada el 12/09/2026",
    "Primera prima recaudada: transacción TX-SINTETICA-0001",
    "Reglas de prima: versión 2026.09.1",
    "Reglas de suscripción: versión 2026.10.1",
    "Verificación",
    "Este documento se verifica con su número de póliza y con la huella sha256 de este archivo,",
    "que Solventa firmó al emitirlo.",
]
_TEXTO = re.compile(rb"\(((?:\\.|[^\\()])*)\)\s*Tj")
_ESCAPE = re.compile(rb"\\([0-7]{1,3}|.)")
_ESCAPES = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f"}


def _cadena(escapada: bytes) -> str:
    def reemplazar(m: re.Match[bytes]) -> bytes:
        valor = m.group(1)
        if valor.isdigit():
            return bytes([int(valor, 8)])
        return _ESCAPES.get(valor, valor)

    return _ESCAPE.sub(reemplazar, escapada).decode("cp1252")


def lineas(pdf: bytes) -> list[str]:
    """El texto de cada `Tj`, en orden."""
    return [_cadena(m.group(1)) for m in _TEXTO.finditer(pdf)]


def huella(pdf: bytes) -> str:
    return hashlib.sha256(pdf).hexdigest()


def generar(datos: DatosDocumento = DATOS) -> bytes:
    return GeneradorPdf().generar(datos)


# --- Determinismo ------------------------------------------------------------------------------
def test_mismas_entradas_mismos_bytes_en_el_mismo_proceso() -> None:
    primero, segundo = generar(), generar()
    assert primero.startswith(b"%PDF-")
    assert huella(primero) == huella(segundo)
    assert primero == segundo


def test_mismas_entradas_mismos_bytes_en_otro_proceso() -> None:
    # Otro proceso, con otra zona horaria, otro locale y otra semilla de hash. Sin SOURCE_DATE_EPOCH:
    # reportlab la usa aun con invariant (ver documento_pdf).
    entorno = {k: v for k, v in os.environ.items() if k != "SOURCE_DATE_EPOCH"}
    entorno |= {"TZ": "Asia/Tokyo", "LC_ALL": "en_US.UTF-8", "LANG": "en_US.UTF-8"}
    entorno["PYTHONHASHSEED"] = "4242"
    codigo = (
        "import runpy, sys; m = runpy.run_path(sys.argv[1]); "
        "sys.stdout.write(m['huella'](m['generar']()))"
    )
    resultado = subprocess.run(
        [sys.executable, "-c", codigo, __file__],
        capture_output=True,
        check=True,
        env=entorno,
        text=True,
        timeout=60,
    )
    assert resultado.stdout == huella(generar())


@pytest.mark.parametrize(
    "cambios",
    [
        {"numero": "100-2026-000002"},
        {"prima": Decimal(64639)},
        {"suma_asegurada": Decimal(180000001)},
        {"vigencia": Vigencia(date(2026, 9, 14), date(2027, 9, 13))},
        {"coberturas": (Cobertura("MUERTE", "Muerte por cualquier causa", True),)},
        {"cliente_id": UUID("00000000-0000-4000-8000-000000000002")},
        {"socio_id": "SOC-000123"},
        {"transaccion_id": "TX-SINTETICA-0002"},
        {"version_reglas_suscripcion": "2026.10.2"},
        {"fecha_documento": FECHA_DOCUMENTO + timedelta(days=1)},
        # Sin cambiar la fecha visible (13/09 en Bogotá), cambia la de los metadatos.
        {"fecha_documento": FECHA_DOCUMENTO + timedelta(minutes=1)},
    ],
    ids=lambda c: next(iter(c)),
)
def test_otras_entradas_dan_otros_bytes(cambios: dict[str, Any]) -> None:
    assert huella(generar(dataclasses.replace(DATOS, **cambios))) != huella(generar())


# --- Contenido ---------------------------------------------------------------------------------
def test_el_contenido_es_el_de_la_poliza_con_tildes_y_montos_es_co() -> None:
    assert lineas(generar()) == LINEAS


def test_no_lleva_la_huella_ni_nombres_ni_datos_sensibles() -> None:
    pdf = generar()
    texto = "\n".join(lineas(pdf))
    # La huella se calcula sobre estos bytes: no puede ir dentro.
    assert "sha256:" not in texto
    assert re.search(r"[0-9a-f]{64}", texto) is None
    assert re.search(rb"[0-9a-f]{64}", pdf) is None
    # Solo identificadores: ni nombre del titular o del canal, ni la edad o el perfil.
    for ausente in ("Nombre", "nombre", "Titular", "Edad", "edad", "perfil", "Banco"):
        assert ausente not in texto


def test_con_socio_lo_lleva_como_identificador() -> None:
    texto = lineas(generar(dataclasses.replace(DATOS, socio_id="SOC-000123")))
    i = texto.index("Asegurado (identificador del cliente): 00000000-0000-4000-8000-000000000001")
    assert texto[i + 1] == "Canal (identificador del socio): SOC-000123"


def test_la_fecha_visible_es_la_del_documento_en_bogota_y_la_de_los_metadatos_tambien() -> None:
    # 23:30 del 13/09 en Bogotá ya es el 14/09 en UTC: se imprime el 13/09.
    tarde = datetime(2026, 9, 14, 4, 30, tzinfo=UTC)
    pdf = generar(dataclasses.replace(DATOS, fecha_documento=tarde))
    assert "Fecha del documento: 13/09/2026" in lineas(pdf)
    assert b"/CreationDate (D:20260914043000+00'00')" in pdf
    assert b"/ModDate (D:20260914043000+00'00')" in pdf
    # Ni la fecha fija de invariant ni la del reloj.
    assert b"D:2000" not in pdf
    assert datetime.now(UTC).strftime("D:%Y%m%d").encode() not in pdf


def test_metadatos_idioma_y_paginas_sin_comprimir() -> None:
    pdf = generar()
    assert b"/Title (P\\363liza 100-2026-000001)" in pdf
    assert b"/Author (Solventa)" in pdf
    assert b"/Lang (es-CO)" in pdf
    assert b"/Count 1 " in pdf
    assert b"/FlateDecode" not in pdf
    assert b"/F1 2 0 R" in pdf and b"/BaseFont /Helvetica " in pdf


def test_muchas_coberturas_pasan_a_otra_pagina_sin_perder_ninguna() -> None:
    coberturas = tuple(Cobertura(f"C{i:02d}", f"Cobertura {i:02d}", i % 2 == 0) for i in range(80))
    pdf = generar(dataclasses.replace(DATOS, coberturas=coberturas))
    texto = lineas(pdf)
    assert b"/Count 3 " in pdf
    assert [t for t in texto if t.startswith("- Cobertura ")] == [
        f"- Cobertura {i:02d} ({'obligatoria' if i % 2 == 0 else 'opcional'})" for i in range(80)
    ]
    assert texto[-1] == "que Solventa firmó al emitirlo."


def test_una_linea_larga_se_parte_sin_perder_palabras() -> None:
    nombre = " ".join(["Cobertura adicional de protección extendida"] * 6)
    pdf = generar(dataclasses.replace(DATOS, coberturas=(Cobertura("LARGA", nombre, False),)))
    texto = lineas(pdf)
    inicio = texto.index("Coberturas") + 1
    fin = texto.index("Valores en COP")
    partes = texto[inicio:fin]
    assert len(partes) > 1
    assert " ".join(partes) == f"- {nombre} (opcional)"


# --- Formato sin locale ------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("valor", "moneda", "esperado"),
    [
        ("180000000", "COP", "$ 180.000.000"),
        ("64638", "COP", "$ 64.638"),
        ("1000", "COP", "$ 1.000"),
        ("999", "COP", "$ 999"),
        ("0", "COP", "$ 0"),
        ("64638.00", "COP", "$ 64.638"),
        ("64638.10", "COP", "$ 64.638,10"),
        ("64638.5", "COP", "$ 64.638,50"),
        ("1234567.891", "COP", "$ 1.234.567,891"),
        ("1E+3", "COP", "$ 1.000"),
        ("1500", "USD", "USD 1.500"),
    ],
)
def test_monto_es_co(valor: str, moneda: str, esperado: str) -> None:
    assert monto_es(Decimal(valor), moneda) == esperado


def test_monto_negativo_no_se_formatea() -> None:
    with pytest.raises(ValueError, match="monto"):
        monto_es(Decimal(-1), "COP")


@pytest.mark.parametrize(
    ("valor", "esperado"),
    [
        (date(2026, 9, 3), "03/09/2026"),
        (date(2027, 12, 31), "31/12/2027"),
        # 23:30 del 12/09 en Bogotá.
        (datetime(2026, 9, 13, 4, 30, tzinfo=UTC), "12/09/2026"),
        (datetime(2026, 9, 12, 10, 0, tzinfo=BOG), "12/09/2026"),
    ],
)
def test_fecha_es_co(valor: date, esperado: str) -> None:
    assert fecha_es(valor) == esperado
