"""El PDF de la póliza con `reportlab` (HU-W17, 17.7; ADR 0014, decisión 4; plan §b, decisión 6).
Implementa GeneradorDocumento.

Determinista: los mismos `DatosDocumento` dan los mismos bytes, en este proceso o en otro.
- `Canvas(..., invariant=1)` fija la marca de tiempo interna y la semilla del `/ID` del archivo.
- La única fecha es `fecha_documento`: la visible (en Bogotá) y la de los metadatos (`CreationDate`
  y `ModDate`, en UTC, por `setDateFormatter`). Ninguna sale del reloj.
- Montos y fechas se escriben a mano, sin `locale`: `$ 180.000.000` y `dd/mm/aaaa`.
- Helvetica es una de las 14 fuentes base del PDF: no se incrusta ni depende de las del sistema.
  reportlab la codifica en WinAnsi, así que las tildes salen bien. Las listas usan guiones: la
  viñeta (U+2022) no tiene código propio en esa codificación.
- Páginas sin comprimir (`pageCompression=0`): el contenido se prueba leyendo los bytes, sin un
  lector de PDF como dependencia, y el resultado no depende de la versión de zlib. Cuesta unos KB en
  un documento de una o dos páginas de texto.
- Ojo: si el proceso define `SOURCE_DATE_EPOCH`, reportlab la usa aun con `invariant` y cambia el
  `/ID`. Ni las tareas de ECS ni las pruebas la definen.

El PDF no lleva su propia huella (se calcula sobre estos bytes): dice que se verifica con el número
y la huella del archivo. Sin nombres (decisión 4): solo los identificadores de `DatosDocumento`.
"""

import io
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Final

from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import simpleSplit
from reportlab.pdfgen.canvas import Canvas

from polizas.domain.poliza.documento import DatosDocumento
from polizas.domain.poliza.modelos import BOGOTA

ANCHO_PAGINA, ALTO_PAGINA = A4
MARGEN: Final = 56.0
ANCHO_TEXTO: Final = ANCHO_PAGINA - 2 * MARGEN
INTERLINEA: Final = 1.35
# Estilo -> (fuente, tamaño en puntos, espacio antes en puntos).
ESTILOS: Final = {
    "marca": ("Helvetica-Bold", 18.0, 0.0),
    "titulo": ("Helvetica", 12.0, 2.0),
    "numero": ("Helvetica-Bold", 14.0, 10.0),
    "seccion": ("Helvetica-Bold", 11.0, 14.0),
    "texto": ("Helvetica", 10.0, 0.0),
}


def fecha_es(valor: date) -> str:
    """`dd/mm/aaaa`; una hora con zona, en su fecha de Bogotá."""
    dia = valor.astimezone(BOGOTA).date() if isinstance(valor, datetime) else valor
    return f"{dia.day:02d}/{dia.month:02d}/{dia.year:04d}"


def monto_es(valor: Decimal, moneda: str) -> str:
    """Punto de miles y coma decimal (es-CO), sin redondear: `$ 64.638`, `$ 64.638,10`."""
    if not valor.is_finite() or valor < 0:
        raise ValueError("monto: se espera un Decimal finito y no negativo")
    entero, _, fraccion = format(valor, "f").partition(".")
    grupos: list[str] = []
    while len(entero) > 3:
        grupos.insert(0, entero[-3:])
        entero = entero[:-3]
    cifra = ".".join([entero, *grupos])
    fraccion = fraccion.rstrip("0")
    if fraccion:
        cifra += "," + fraccion.ljust(2, "0")
    return f"{'$' if moneda == 'COP' else moneda} {cifra}"


def lineas(datos: DatosDocumento) -> list[tuple[str, str]]:
    """(estilo, texto) de cada línea lógica, en orden; las largas se parten al dibujarlas."""
    ramo = datos.ramo
    socio = (
        []
        if datos.socio_id is None
        else [("texto", f"Canal (identificador del socio): {datos.socio_id}")]
    )
    coberturas = [
        ("texto", f"- {c.nombre} ({'obligatoria' if c.obligatoria else 'opcional'})")
        for c in datos.coberturas
    ]
    return [
        ("marca", "Solventa"),
        ("titulo", f"Póliza de seguro · {ramo.nombre}"),
        ("numero", f"Póliza N.º {datos.numero}"),
        ("texto", f"Fecha del documento: {fecha_es(datos.fecha_documento)}"),
        ("seccion", "Datos de la póliza"),
        ("texto", f"Ramo: {ramo.codigo} · {ramo.nombre}"),
        ("texto", f"Producto: {datos.producto.id}, versión {datos.producto.version}"),
        (
            "texto",
            (
                f"Vigencia: del {fecha_es(datos.vigencia.desde)} al "
                f"{fecha_es(datos.vigencia.hasta)} (hora de Colombia)"
            ),
        ),
        ("texto", f"Asegurado (identificador del cliente): {datos.cliente_id}"),
        *socio,
        ("seccion", "Coberturas"),
        *coberturas,
        ("seccion", f"Valores en {datos.moneda}"),
        ("texto", f"Suma asegurada: {monto_es(datos.suma_asegurada, datos.moneda)}"),
        (
            "texto",
            f"Prima {datos.periodicidad.lower()}: {monto_es(datos.prima, datos.moneda)}",
        ),
        ("seccion", "Contratación"),
        ("texto", f"Oferta aceptada el {fecha_es(datos.aceptada_en)}"),
        ("texto", f"Primera prima recaudada: transacción {datos.transaccion_id}"),
        ("texto", f"Reglas de prima: versión {datos.version_reglas_prima}"),
        ("texto", f"Reglas de suscripción: versión {datos.version_reglas_suscripcion}"),
        ("seccion", "Verificación"),
        (
            "texto",
            (
                "Este documento se verifica con su número de póliza y con la huella sha256 de "
                "este archivo,"
            ),
        ),
        ("texto", "que Solventa firmó al emitirlo."),
    ]


def _fecha_metadatos(fecha: datetime) -> Callable[..., str]:
    sello = f"D:{fecha.astimezone(UTC):%Y%m%d%H%M%S}+00'00'"
    return lambda *_: sello


class GeneradorPdf:
    """Implementa GeneradorDocumento: una o más páginas A4 de texto."""

    def generar(self, datos: DatosDocumento) -> bytes:
        salida = io.BytesIO()
        lienzo = Canvas(salida, pagesize=A4, invariant=1, pageCompression=0, lang="es-CO")
        lienzo.setDateFormatter(_fecha_metadatos(datos.fecha_documento))
        lienzo.setTitle(f"Póliza {datos.numero}")
        lienzo.setSubject(f"Póliza de seguro · {datos.ramo.nombre}")
        lienzo.setAuthor("Solventa")
        lienzo.setCreator("Solventa polizas")
        arriba = ALTO_PAGINA - MARGEN
        y, fuente_actual = arriba, None
        for estilo, texto in lineas(datos):
            fuente, tamano, antes = ESTILOS[estilo]
            alto = tamano * INTERLINEA
            y -= antes
            for parte in simpleSplit(texto, fuente, tamano, ANCHO_TEXTO):
                if y - alto < MARGEN:
                    lienzo.showPage()
                    y, fuente_actual = arriba, None
                y -= alto
                if fuente_actual != (fuente, tamano):
                    lienzo.setFont(fuente, tamano)
                    fuente_actual = (fuente, tamano)
                lienzo.drawString(MARGEN, y, parte)
        lienzo.showPage()
        lienzo.save()
        return salida.getvalue()
