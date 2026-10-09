"""Catálogo de fuentes: aliado, ruta y correspondencia canónica → proveedor (03 §B.5)."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from adaptador_datos.domain.consultas.modelos import Aliado, Fuente, TipoFuente

Validador = Literal["entero", "dinero_no_negativo", "dinero_positivo"]

PROVEEDOR: Mapping[Aliado, str] = MappingProxyType(
    {Aliado.OPEN_FINANCE: "SIM-OPEN-FINANCE", Aliado.DATOS_ABIERTOS: "SIM-DATOS-ABIERTOS"}
)


@dataclass(frozen=True, slots=True)
class DefCampo:
    clave_proveedor: str
    ruta: tuple[str, ...]
    validador: Validador


@dataclass(frozen=True, slots=True)
class DefFuente:
    fuente: Fuente
    aliado: Aliado
    tipo: TipoFuente
    ruta: str
    parametro: Literal["fields", "campos"]
    extras: Mapping[str, str]
    campos: Mapping[str, DefCampo]


def _campo(clave: str, validador: Validador) -> DefCampo:
    return DefCampo(clave_proveedor=clave, ruta=tuple(clave.split(".")), validador=validador)


CATALOGO: Mapping[Fuente, DefFuente] = MappingProxyType(
    {
        Fuente.FA_PRODUCTOS_VIGENTES: DefFuente(
            fuente=Fuente.FA_PRODUCTOS_VIGENTES,
            aliado=Aliado.OPEN_FINANCE,
            tipo="FINANZAS_ABIERTAS",
            ruta="/v3/customers/{cliente_id}/active-products",
            parametro="fields",
            extras=MappingProxyType({}),
            campos=MappingProxyType(
                {
                    "antiguedad_productos_anios": _campo("oldestProductYears", "entero"),
                    "cuota_mensual_obligaciones": _campo(
                        "totalMonthlyInstallment", "dinero_no_negativo"
                    ),
                    "entidades_con_deuda": _campo("creditorCount", "entero"),
                }
            ),
        ),
        Fuente.FA_HISTORIAL_PAGOS_12M: DefFuente(
            fuente=Fuente.FA_HISTORIAL_PAGOS_12M,
            aliado=Aliado.OPEN_FINANCE,
            tipo="FINANZAS_ABIERTAS",
            ruta="/v3/customers/{cliente_id}/payment-history",
            parametro="fields",
            extras=MappingProxyType({"months": "12"}),
            campos=MappingProxyType({"moras_12m": _campo("lateCount", "entero")}),
        ),
        Fuente.FA_INGRESOS_AGREGADOS: DefFuente(
            fuente=Fuente.FA_INGRESOS_AGREGADOS,
            aliado=Aliado.OPEN_FINANCE,
            tipo="FINANZAS_ABIERTAS",
            ruta="/v3/customers/{cliente_id}/aggregated-income",
            parametro="fields",
            extras=MappingProxyType({}),
            campos=MappingProxyType(
                {"ingreso_mensual_estimado": _campo("estimatedMonthlyIncome", "dinero_positivo")}
            ),
        ),
        Fuente.DA_REGISTROS_PUBLICOS: DefFuente(
            fuente=Fuente.DA_REGISTROS_PUBLICOS,
            aliado=Aliado.DATOS_ABIERTOS,
            tipo="DATOS_ABIERTOS",
            ruta="/v1/registros/{cliente_id}",
            parametro="campos",
            extras=MappingProxyType({}),
            campos=MappingProxyType(
                {
                    "coincidencias_listas_restrictivas": _campo(
                        "listasRestrictivas.coincidencias", "entero"
                    )
                }
            ),
        ),
    }
)


def claves_proveedor(fuente: Fuente, campos: Iterable[str]) -> tuple[str, ...]:
    """Claves de primer nivel del proveedor, ordenadas y sin duplicados (KeyError si no existe)."""
    definicion = CATALOGO[fuente]
    return tuple(sorted({definicion.campos[campo].ruta[0] for campo in campos}))
