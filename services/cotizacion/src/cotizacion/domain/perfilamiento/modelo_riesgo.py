"""Estructura del modelo de riesgo versionado (02, CS-05). El parser y el motor son de W11."""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from cotizacion.domain.perfilamiento.modelos import Direccion, Fuente, NivelRiesgo

Operador = Literal["==", "!=", "<", "<=", ">", ">="]


@dataclass(frozen=True, slots=True)
class Condicion:
    variable: str
    operador: Operador
    valor: Decimal


@dataclass(frozen=True, slots=True)
class ReglaFactor:
    si: tuple[Condicion, ...]
    contribucion: Decimal
    direccion: Direccion


@dataclass(frozen=True, slots=True)
class DefFactor:
    codigo: str
    fuente: str
    peso: Decimal
    reglas: tuple[ReglaFactor, ...]


@dataclass(frozen=True, slots=True)
class DefAjuste:
    codigo: str
    si: tuple[Condicion, ...]
    efecto: Literal["FORZAR_NIVEL"]
    nivel: NivelRiesgo


@dataclass(frozen=True, slots=True)
class Expresion:
    op: Literal["add", "sub", "mul", "div"]
    args: tuple["str | Decimal | Expresion", ...]


@dataclass(frozen=True, slots=True)
class UmbralNivel:
    desde: Decimal
    nivel: NivelRiesgo


@dataclass(frozen=True, slots=True)
class ModeloRiesgo:
    version: str
    hash: str
    producto_id: str
    proposito: str
    fuentes: Mapping[Fuente, tuple[str, ...]]
    declarados: tuple[str, ...]
    obligatorios: frozenset[str]
    derivados: Mapping[str, Expresion]
    factores: tuple[DefFactor, ...]
    ajustes: tuple[DefAjuste, ...]
    niveles: tuple[UmbralNivel, ...]
    penalizacion_no_autorizada: Decimal
    penalizacion_no_obtenida: Decimal

    def fuente_de_campo(self, campo: str) -> Fuente | None:
        """Fuente que aporta el campo; None si es declarado, derivado o desconocido."""
        for fuente, campos in self.fuentes.items():
            if campo in campos:
                return fuente
        return None
