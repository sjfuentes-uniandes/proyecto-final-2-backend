"""Autenticidad del envío (HU-W21, DS3-16): HMAC-SHA256 y desfase de la marca de tiempo.

La firma se calcula sobre `<marca de tiempo>.<cuerpo crudo>`: la marca tal cual llegó en el
encabezado y los bytes recibidos, sin parsear ni reformatear. Funciones puras, sin E/S ni reloj.
"""

import hashlib
import hmac
from datetime import UTC, datetime

PREFIJO_FIRMA = "sha256="


def calcular_firma(secreto: str, marca: str, cuerpo: bytes) -> str:
    """El valor de `X-Telemetria-Firma`: `sha256=<hex>`."""
    mensaje = marca.encode() + b"." + cuerpo
    return PREFIJO_FIRMA + hmac.new(secreto.encode(), mensaje, hashlib.sha256).hexdigest()


def verificar_firma(secreto: str, marca: str, cuerpo: bytes, firma: str | None) -> bool:
    """True si `firma` es la del cuerpo y la marca. Comparación de tiempo constante."""
    if not firma:
        return False
    esperada = calcular_firma(secreto, marca, cuerpo)
    # compare_digest sobre bytes: con str lanzaría TypeError ante caracteres que no son ASCII.
    return hmac.compare_digest(esperada.encode(), firma.encode())


def leer_marca(texto: str | None) -> datetime | None:
    """La marca como instante UTC, o None si falta o no es ISO 8601 con zona horaria."""
    if not texto:
        return None
    try:
        instante = datetime.fromisoformat(texto)
    except ValueError:
        return None
    if instante.tzinfo is None:
        return None
    return instante.astimezone(UTC)


def dentro_de_desfase(marca: datetime, ahora: datetime, maximo_s: int) -> bool:
    """True si la marca está a no más de `maximo_s` del reloj, hacia atrás o hacia adelante."""
    return abs((ahora - marca).total_seconds()) <= maximo_s
