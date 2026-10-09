"""Proyección y filtrado de la respuesta del proveedor (03 §B.5; W10 AC2 y AC4).

Solo se leen las rutas de los campos autorizados: lo demás nunca se copia ni se devuelve.
"""

from collections.abc import Iterable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from adaptador_datos.domain.consultas.catalogo_fuentes import CATALOGO, DefCampo
from adaptador_datos.domain.consultas.modelos import (
    Aliado,
    Fuente,
    RespuestaInvalida,
    ValorCanonico,
)

# Sobre de cada aliado: (clave de versión, clave de confianza, contenedor de datos).
_SOBRES: Mapping[Aliado, tuple[str, str, str]] = {
    Aliado.OPEN_FINANCE: ("schemaVersion", "confidenceScore", "data"),
    Aliado.DATOS_ABIERTOS: ("version", "nivelConfianza", "registro"),
}
_NO_DISPONIBLE = ValorCanonico(estado="NO_DISPONIBLE", valor=None)
_AUSENTE = object()


def _es_numero(valor: Any) -> bool:
    return isinstance(valor, int | float) and not isinstance(valor, bool)


def _sobre(aliado: Aliado, cuerpo: Any) -> tuple[str, Decimal, Mapping[str, Any]]:
    clave_version, clave_confianza, clave_datos = _SOBRES[aliado]
    if not isinstance(cuerpo, Mapping):
        raise RespuestaInvalida("SOBRE")
    version = cuerpo.get(clave_version)
    confianza = cuerpo.get(clave_confianza)
    contenedor = cuerpo.get(clave_datos)
    if not isinstance(version, str) or not _es_numero(confianza):
        raise RespuestaInvalida("SOBRE")
    assert isinstance(confianza, int | float)
    if not 0 <= confianza <= 1:
        raise RespuestaInvalida("SOBRE")
    if not isinstance(contenedor, Mapping):
        raise RespuestaInvalida("SOBRE")
    return version, Decimal(str(confianza)), contenedor


def _leer(contenedor: Mapping[str, Any], ruta: tuple[str, ...]) -> Any:
    actual: Any = contenedor
    for parte in ruta:
        if actual is None:
            return _AUSENTE
        if not isinstance(actual, Mapping):
            raise RespuestaInvalida("CAMPO")
        actual = actual.get(parte, _AUSENTE)
        if actual is _AUSENTE:
            return _AUSENTE
    return actual


def _dinero(valor: Any) -> Decimal:
    if not isinstance(valor, Mapping) or valor.get("currency") != "COP":
        raise RespuestaInvalida("CAMPO")
    monto = valor.get("amount")
    if not isinstance(monto, str):
        raise RespuestaInvalida("CAMPO")
    try:
        decimal = Decimal(monto)
    except InvalidOperation as error:
        raise RespuestaInvalida("CAMPO") from error
    if not decimal.is_finite():
        raise RespuestaInvalida("CAMPO")
    return decimal


def _validar(definicion: DefCampo, valor: Any) -> Decimal:
    if definicion.validador == "entero":
        if not isinstance(valor, int) or isinstance(valor, bool) or valor < 0:
            raise RespuestaInvalida("CAMPO")
        return Decimal(valor)
    decimal = _dinero(valor)
    minimo_ok = decimal > 0 if definicion.validador == "dinero_positivo" else decimal >= 0
    if not minimo_ok:
        raise RespuestaInvalida("CAMPO")
    return decimal


def proyectar(
    fuente: Fuente, autorizados: Iterable[str], cuerpo: Any
) -> tuple[dict[str, ValorCanonico], int, str, Decimal]:
    """Campos autorizados, conteo de descartados, versión de la fuente y confianza."""
    definicion = CATALOGO[fuente]
    version, confianza, contenedor = _sobre(definicion.aliado, cuerpo)
    campos: dict[str, ValorCanonico] = {}
    claves_autorizadas: set[str] = set()
    for campo in autorizados:
        def_campo = definicion.campos[campo]
        claves_autorizadas.add(def_campo.ruta[0])
        valor = _leer(contenedor, def_campo.ruta)
        if valor is _AUSENTE or valor is None:
            campos[campo] = _NO_DISPONIBLE
        else:
            campos[campo] = ValorCanonico(estado="DISPONIBLE", valor=_validar(def_campo, valor))
    descartados = sum(1 for clave in contenedor if clave not in claves_autorizadas)
    return campos, descartados, version, confianza
