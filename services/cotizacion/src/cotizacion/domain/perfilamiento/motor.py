"""Motor de evaluación del perfil de riesgo (04 §B.6.2).

Función pura y determinista: sin reloj, sin E/S y sin float. Todo el cálculo usa Decimal y
los redondeos son a 0,01 con ROUND_HALF_EVEN.
"""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal

from cotizacion.domain.perfilamiento.modelo_riesgo import (
    Condicion,
    DefFactor,
    Expresion,
    ModeloRiesgo,
)
from cotizacion.domain.perfilamiento.modelos import (
    AccionSugerida,
    AjusteAplicado,
    DatoRequerido,
    EstadoFactor,
    EstadoFuente,
    EstadoPerfil,
    FactorEvaluado,
    Fuente,
    MotivoDato,
    NivelRiesgo,
    ResultadoEvaluacion,
)


@dataclass(frozen=True, slots=True)
class FuenteEvaluada:
    estado: EstadoFuente
    confianza: Decimal | None


CENTESIMA = Decimal("0.01")
CIEN = Decimal(100)

# NO_CONSULTADA no tiene motivo: la fuente estaba autorizada pero no se invocó (DT-07).
_MOTIVO_POR_ESTADO: Mapping[EstadoFuente, MotivoDato] = {
    EstadoFuente.CONSULTADA: MotivoDato.NO_DISPONIBLE_EN_FUENTE,
    EstadoFuente.NO_AUTORIZADA: MotivoDato.NO_AUTORIZADO,
    EstadoFuente.TIEMPO_AGOTADO: MotivoDato.TIEMPO_AGOTADO,
    EstadoFuente.NO_DISPONIBLE: MotivoDato.FUENTE_NO_DISPONIBLE,
    EstadoFuente.RESPUESTA_INVALIDA: MotivoDato.RESPUESTA_INVALIDA,
}
_SIN_PERMISO = frozenset({EstadoFuente.NO_AUTORIZADA, EstadoFuente.NO_CONSULTADA})
_NO_OBTENIDA = frozenset(
    {EstadoFuente.TIEMPO_AGOTADO, EstadoFuente.NO_DISPONIBLE, EstadoFuente.RESPUESTA_INVALIDA}
)
_MOTIVOS_RECALCULO = frozenset(
    {MotivoDato.TIEMPO_AGOTADO, MotivoDato.FUENTE_NO_DISPONIBLE, MotivoDato.RESPUESTA_INVALIDA}
)
_SEVERIDAD = {NivelRiesgo.BAJO: 0, NivelRiesgo.MEDIO: 1, NivelRiesgo.ALTO: 2}
_COMPARAR = {
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
}


def evaluar(
    modelo: ModeloRiesgo,
    variables: Mapping[str, Decimal],
    fuentes: Mapping[Fuente, FuenteEvaluada],
) -> ResultadoEvaluacion:
    """Evalúa el modelo con las variables disponibles y el estado de cada fuente del modelo."""
    contexto = _Contexto(modelo, variables, fuentes)
    confianza = _confianza(modelo, fuentes)

    # Paso 4: un obligatorio faltante impide clasificar (AC3).
    faltantes = [v for v in _orden_obligatorios(modelo) if v not in contexto.valores]
    if faltantes:
        return _insuficiente(contexto.requeridos(faltantes), confianza)

    # Paso 5: factores en el orden del modelo; sin datos quedan NO_EVALUADO (DT-08).
    factores = tuple(contexto.factor(f) for f in modelo.factores)
    evaluados = [f for f in factores if f.estado is EstadoFactor.EVALUADO]
    peso_total = sum((f.peso for f in evaluados), Decimal(0))
    if peso_total == 0:
        return _insuficiente(contexto.requeridos(contexto.faltantes_de_factores()), confianza)

    # Paso 6: puntaje sobre el peso evaluado y nivel por umbral.
    suma = sum(
        (f.peso * f.contribucion for f in evaluados if f.contribucion is not None), Decimal(0)
    )
    puntaje = (suma / peso_total * CIEN).quantize(CENTESIMA, rounding=ROUND_HALF_EVEN)
    nivel = next(u.nivel for u in modelo.niveles if puntaje >= u.desde)

    # Paso 7: los ajustes solo suben la severidad.
    ajustes = tuple(
        AjusteAplicado(a.codigo, a.efecto, a.nivel) for a in modelo.ajustes if contexto.cumple(a.si)
    )
    for ajuste in ajustes:
        if ajuste.nivel is not None and _SEVERIDAD[ajuste.nivel] > _SEVERIDAD[nivel]:
            nivel = ajuste.nivel
    degradado = any(f.estado in _NO_OBTENIDA for f in fuentes.values())
    estado = EstadoPerfil.DEGRADADO if degradado else EstadoPerfil.CALCULADO
    return ResultadoEvaluacion(
        estado=estado,
        nivel=nivel,
        puntaje=puntaje,
        confianza=confianza,
        factores=factores,
        ajustes=ajustes,
        datos_requeridos=(),
        accion=_accion(estado, ()),
    )


def _orden_obligatorios(modelo: ModeloRiesgo) -> list[str]:
    """Los obligatorios en el orden en que el modelo declara sus variables.

    `ModeloRiesgo.obligatorios` es un frozenset (CS-05): el orden sale de `declarados` y
    de los campos de cada fuente, en el orden del modelo. Así el resultado es determinista.
    """
    orden = [*modelo.declarados, *(c for campos in modelo.fuentes.values() for c in campos)]
    conocidos = [v for v in dict.fromkeys(orden) if v in modelo.obligatorios]
    return conocidos + sorted(modelo.obligatorios - set(conocidos))


def _insuficiente(requeridos: tuple[DatoRequerido, ...], confianza: Decimal) -> ResultadoEvaluacion:
    estado = EstadoPerfil.INFORMACION_INSUFICIENTE
    return ResultadoEvaluacion(
        estado=estado,
        nivel=None,
        puntaje=None,
        confianza=confianza,
        factores=(),
        ajustes=(),
        datos_requeridos=requeridos,
        accion=_accion(estado, requeridos),
    )


def _confianza(modelo: ModeloRiesgo, fuentes: Mapping[Fuente, FuenteEvaluada]) -> Decimal:
    consultadas = [
        f.confianza
        for f in fuentes.values()
        if f.estado is EstadoFuente.CONSULTADA and f.confianza is not None
    ]
    if not consultadas:
        return Decimal("0.00")
    n_sin_permiso = sum(1 for f in fuentes.values() if f.estado in _SIN_PERMISO)
    n_no_obtenida = sum(1 for f in fuentes.values() if f.estado in _NO_OBTENIDA)
    promedio = sum(consultadas, Decimal(0)) / len(consultadas)
    factor = (
        1
        - modelo.penalizacion_no_autorizada * n_sin_permiso
        - modelo.penalizacion_no_obtenida * n_no_obtenida
    )
    valor = min(max(promedio * factor, Decimal(0)), Decimal(1))
    return valor.quantize(CENTESIMA, rounding=ROUND_HALF_EVEN)


def _accion(estado: EstadoPerfil, requeridos: tuple[DatoRequerido, ...]) -> AccionSugerida | None:
    motivos = {r.motivo for r in requeridos}
    if MotivoDato.NO_AUTORIZADO in motivos:
        return AccionSugerida.COMPLETAR_CONSENTIMIENTO
    if estado is EstadoPerfil.DEGRADADO or motivos & _MOTIVOS_RECALCULO:
        return AccionSugerida.RECALCULAR
    return None


class _Contexto:
    """Variables disponibles (base y derivadas) y el motivo de las que faltan."""

    def __init__(
        self,
        modelo: ModeloRiesgo,
        variables: Mapping[str, Decimal],
        fuentes: Mapping[Fuente, FuenteEvaluada],
    ) -> None:
        self.modelo = modelo
        self.fuentes = fuentes
        self.valores: dict[str, Decimal] = dict(variables)
        # Paso 1: derivados en el orden del modelo; sin argumentos o con divisor 0, faltan.
        for nombre, expresion in modelo.derivados.items():
            valor = self._calcular(expresion)
            if valor is not None:
                self.valores[nombre] = valor

    def _calcular(self, expresion: Expresion) -> Decimal | None:
        argumentos: list[Decimal] = []
        for arg in expresion.args:
            if isinstance(arg, Expresion):
                valor = self._calcular(arg)
            elif isinstance(arg, Decimal):
                valor = arg
            else:
                valor = self.valores.get(arg)
            if valor is None:
                return None
            argumentos.append(valor)
        primero, *resto = argumentos
        for valor in resto:
            if expresion.op == "add":
                primero += valor
            elif expresion.op == "sub":
                primero -= valor
            elif expresion.op == "mul":
                primero *= valor
            elif valor == 0:
                return None
            else:
                primero /= valor
        return primero

    def _base_faltante(self, variable: str) -> str:
        """La variable base que explica la falta: para un derivado, su primer argumento base
        faltante en el orden de sus args; si ninguno falta (divisor 0), el propio derivado."""
        expresion = self.modelo.derivados.get(variable)
        if expresion is None:
            return variable
        for nombre in _variables_de(expresion):
            if nombre not in self.valores:
                return self._base_faltante(nombre)
        return variable

    def _motivo(self, variable: str) -> tuple[str, Fuente | None, MotivoDato | None]:
        """(campo, fuente, motivo) de una variable faltante; motivo None si no se consultó."""
        base = self._base_faltante(variable)
        fuente = self.modelo.fuente_de_campo(base)
        if fuente is None:
            # Declarado o derivado con divisor 0: el dato existe pero no sirve para el cálculo.
            return base, None, MotivoDato.NO_DISPONIBLE_EN_FUENTE
        return base, fuente, _MOTIVO_POR_ESTADO.get(self.fuentes[fuente].estado)

    def requeridos(self, faltantes: list[str]) -> tuple[DatoRequerido, ...]:
        """Datos requeridos sin repetir y sin los de fuentes NO_CONSULTADA."""
        requeridos: dict[str, DatoRequerido] = {}
        for variable in faltantes:
            campo, fuente, motivo = self._motivo(variable)
            if motivo is not None and campo not in requeridos:
                requeridos[campo] = DatoRequerido(campo, fuente, motivo)
        return tuple(requeridos.values())

    def faltantes_de_factores(self) -> list[str]:
        return [
            v for f in self.modelo.factores for v in _variables_factor(f) if v not in self.valores
        ]

    def cumple(self, condiciones: tuple[Condicion, ...]) -> bool:
        if any(c.variable not in self.valores for c in condiciones):
            return False
        return all(_COMPARAR[c.operador](self.valores[c.variable], c.valor) for c in condiciones)

    def factor(self, definicion: DefFactor) -> FactorEvaluado:
        faltante = next((v for v in _variables_factor(definicion) if v not in self.valores), None)
        if faltante is not None:
            return FactorEvaluado(
                codigo=definicion.codigo,
                fuente=definicion.fuente,
                peso=definicion.peso,
                estado=EstadoFactor.NO_EVALUADO,
                contribucion=None,
                direccion=None,
                motivo=self._motivo(faltante)[2],
            )
        for regla in definicion.reglas:
            if self.cumple(regla.si):
                return FactorEvaluado(
                    codigo=definicion.codigo,
                    fuente=definicion.fuente,
                    peso=definicion.peso,
                    estado=EstadoFactor.EVALUADO,
                    contribucion=regla.contribucion,
                    direccion=regla.direccion,
                    motivo=None,
                )
        raise ValueError(f"el factor {definicion.codigo} no tiene regla por defecto")


def _variables_factor(definicion: DefFactor) -> list[str]:
    """Variables de las condiciones del factor, sin repetir y en orden de aparición."""
    return list(dict.fromkeys(c.variable for r in definicion.reglas for c in r.si))


def _variables_de(expresion: Expresion) -> Iterator[str]:
    for arg in expresion.args:
        if isinstance(arg, Expresion):
            yield from _variables_de(arg)
        elif isinstance(arg, str):
            yield arg
