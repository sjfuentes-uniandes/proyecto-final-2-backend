"""Cálculo determinista de la prima (HU-W03 AC1 y AC3). Todo en Decimal, nunca float.

    prima_base     = R(suma_asegurada × tasa_base)
    prima_ajustada = R(prima_base × factor_nivel × Π factores por variable)
    recargo_c      = R(prima_ajustada × tasa_c)          por cada cobertura opcional elegida
    subtotal       = prima_ajustada + Σ recargo_c
    prima_total    = min(max(subtotal, R(prima_minima)), R(prima_maxima))
    ajuste_limite  = prima_total − subtotal

R redondea a la escala y con el modo de las reglas (p. ej. 0 y ROUND_HALF_EVEN en COP). Cada línea
del desglose queda redondeada y las sumas son exactas, de modo que el desglose cuadra con la prima.
El contexto decimal es propio (34 dígitos, ROUND_HALF_EVEN): el resultado no depende del contexto
global del proceso. Misma entrada y mismas reglas (versión y hash) dan el mismo desglose y la misma
huella.
"""

import hashlib
import json
from datetime import date
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from typing import Any

from cotizacion.domain.cotizacion.errores import CoberturaNoDisponible, DatosFueraDeRango
from cotizacion.domain.cotizacion.modelos import (
    SIN_PERFIL,
    Desglose,
    EntradasPrima,
    FactorAplicado,
    FactorVariable,
    Producto,
    RecargoAplicado,
    ReglasPrima,
)

CONTEXTO = Context(prec=34, rounding=ROUND_HALF_EVEN)
FACTOR_NIVEL = "NIVEL_RIESGO"

# Rutas de los campos de la solicitud (detalles.campos), nunca sus valores.
CAMPO_SUMA = "datosAsegurado.sumaAsegurada"
CAMPO_PLAZO = "datosAsegurado.plazoCreditoMeses"
CAMPO_NACIMIENTO = "datosAsegurado.fechaNacimiento"
CAMPO_COBERTURAS = "coberturasOpcionales"


def texto(valor: Decimal) -> str:
    """Decimal sin notación científica (estable para JSON, el hash y la base)."""
    return format(valor, "f")


def edad_cumplida(nacimiento: date, en: date) -> int:
    """Años cumplidos en la fecha de la cotización (DT-27)."""
    return en.year - nacimiento.year - ((en.month, en.day) < (nacimiento.month, nacimiento.day))


def validar_entradas(producto: Producto, reglas: ReglasPrima, entradas: EntradasPrima) -> None:
    """Coberturas opcionales del producto y datos dentro de los límites de las reglas."""
    if set(entradas.coberturas_opcionales) - producto.opcionales:
        raise CoberturaNoDisponible((CAMPO_COBERTURAS,))
    limites = reglas.limites
    fuera = [
        campo
        for campo, rango, valor in (
            (CAMPO_SUMA, limites.suma_asegurada, entradas.suma_asegurada),
            (CAMPO_PLAZO, limites.plazo_credito_meses, Decimal(entradas.plazo_credito_meses)),
            (CAMPO_NACIMIENTO, limites.edad, Decimal(entradas.edad)),
        )
        if not rango.contiene(valor)
    ]
    if fuera:
        raise DatosFueraDeRango(tuple(fuera))


def _redondear(valor: Decimal, reglas: ReglasPrima) -> Decimal:
    exponente = Decimal(1).scaleb(-reglas.redondeo_escala, CONTEXTO)
    return valor.quantize(exponente, rounding=reglas.redondeo_modo, context=CONTEXTO)


def _factor_variable(factor: FactorVariable, valor: Decimal) -> FactorAplicado:
    for tramo in factor.tramos:
        if tramo.hasta is None or valor <= tramo.hasta:
            return FactorAplicado(factor.codigo, texto(valor), tramo.factor)
    raise ValueError(f"el factor {factor.codigo} no tiene un tramo abierto")


def calcular_prima(reglas: ReglasPrima, entradas: EntradasPrima) -> Desglose:
    variables = {
        "edad": Decimal(entradas.edad),
        "plazo_credito_meses": Decimal(entradas.plazo_credito_meses),
        "suma_asegurada": entradas.suma_asegurada,
    }
    nivel = entradas.nivel_riesgo.value if entradas.nivel_riesgo else SIN_PERFIL
    with localcontext(CONTEXTO):
        prima_base = _redondear(entradas.suma_asegurada * reglas.tasa_base, reglas)
        factores = (
            FactorAplicado(FACTOR_NIVEL, nivel, reglas.factores_nivel[nivel]),
            *(_factor_variable(f, variables[f.variable]) for f in reglas.factores_variables),
        )
        producto_factores = Decimal(1)
        for factor in factores:
            producto_factores *= factor.factor
        prima_ajustada = _redondear(prima_base * producto_factores, reglas)
        recargos = tuple(
            RecargoAplicado(
                c, reglas.recargos[c], _redondear(prima_ajustada * reglas.recargos[c], reglas)
            )
            for c in sorted(entradas.coberturas_opcionales)
        )
        subtotal = prima_ajustada + sum((r.valor for r in recargos), Decimal(0))
        minima = _redondear(reglas.prima_minima, reglas)
        maxima = _redondear(reglas.prima_maxima, reglas)
        prima_total = min(max(subtotal, minima), maxima)
    return Desglose(
        prima_base=prima_base,
        factores=factores,
        prima_ajustada=prima_ajustada,
        recargos=recargos,
        subtotal=subtotal,
        ajuste_limite=prima_total - subtotal,
        prima_total=prima_total,
    )


def entradas_canonicas(entradas: EntradasPrima) -> dict[str, Any]:
    return {
        "sumaAsegurada": texto(entradas.suma_asegurada),
        "plazoCreditoMeses": entradas.plazo_credito_meses,
        "edad": entradas.edad,
        "nivelRiesgo": entradas.nivel_riesgo.value if entradas.nivel_riesgo else None,
        "coberturasOpcionales": sorted(entradas.coberturas_opcionales),
    }


def desglose_canonico(desglose: Desglose) -> dict[str, Any]:
    return {
        "primaBase": texto(desglose.prima_base),
        "factores": [
            {"codigo": f.codigo, "valor": f.valor, "factor": texto(f.factor)}
            for f in desglose.factores
        ],
        "primaAjustada": texto(desglose.prima_ajustada),
        "recargos": [
            {"cobertura": r.cobertura, "tasa": texto(r.tasa), "valor": texto(r.valor)}
            for r in desglose.recargos
        ],
        "subtotal": texto(desglose.subtotal),
        "ajusteLimite": texto(desglose.ajuste_limite),
        "primaTotal": texto(desglose.prima_total),
    }


def huella_calculo(reglas: ReglasPrima, entradas: EntradasPrima, desglose: Desglose) -> str:
    """sha256 del JSON canónico de lo que determina la prima (AC3): producto, versión y hash de
    las reglas, entradas y desglose. No incluye identificadores ni instantes."""
    contenido = {
        "productoId": reglas.producto_id,
        "versionReglas": reglas.version,
        "hashReglas": reglas.hash,
        "moneda": reglas.moneda,
        "entradas": entradas_canonicas(entradas),
        "desglose": desglose_canonico(desglose),
    }
    canonico = json.dumps(contenido, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonico.encode("utf-8")).hexdigest()
