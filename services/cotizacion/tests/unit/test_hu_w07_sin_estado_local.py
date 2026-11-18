"""HU-W07 N1: cotizacion no guarda estado de módulo mutable, así que cualquier tarea atiende igual.

N1a recorre con `ast` todos los .py de src/cotizacion y falla si:
- hay una sentencia `global` o `nonlocal`;
- una asignación de módulo crea un valor mutable (dict, list, set, sus comprensiones o una llamada
  a dict(), list(), set(), defaultdict()…) o un objeto construido por una llamada que no es
  inmutable conocida, y su nombre no está en PERMITIDOS;
- una función muta un nombre de módulo, permitido o no: asignación por subíndice o atributo,
  `del`, o un método mutador (.append, .update, .setdefault, .pop…);
- se usa un atributo de CONTEXTO (prima.py), getcontext o setcontext, o localcontext con un
  argumento que no sea CONTEXTO.

Límite: el análisis estático no ve la mutación que hace una función que recibe el objeto como
argumento (decimal escribe las flags del Context que recibe). Por eso todo objeto de módulo que no
sea inmutable tiene que estar en PERMITIDOS con su justificación (docs/hu-w07-replicas.md).

N1b: las cachés que ya existen (CatalogoHttp y ProductosCatalogo) son de cada instancia.
"""

import ast
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from cotizacion.adapters.outbound.catalogo_http import CatalogoHttp
from cotizacion.adapters.outbound.productos_catalogo import ProductosCatalogo

SRC = Path(__file__).resolve().parents[2] / "src" / "cotizacion"

# Archivo relativo a src/cotizacion y nombre → por qué se acepta como objeto de módulo.
PERMITIDOS: dict[str, str] = {
    # Constantes: tablas que solo se leen (la N1a comprueba que ninguna función las muta).
    "adapters/inbound/http/cotizaciones.py:_ERRORES_ACTOR": (
        "constante: respuestas 401/403 de OpenAPI para APIRouter(responses=...); nadie la muta"
    ),
    "adapters/inbound/http/perfilamiento.py:_ERRORES_ACTOR": (
        "constante: respuestas 401/403 de OpenAPI para APIRouter(responses=...); nadie la muta"
    ),
    "adapters/outbound/adaptador_datos_http.py:_ESTADO_POR_CODIGO": (
        "constante: estado de la fuente por código HTTP (504, 502); nadie la muta"
    ),
    "adapters/outbound/eventos.py:ESQUEMAS": (
        "constante: archivo de esquema → modelo del evento; solo se recorre; nadie la muta"
    ),
    "application/cotizacion/calcular_prima.py:PROVEEDORES": (
        "constante: dependencia → proveedor para la telemetría; nadie la muta"
    ),
    "application/perfilamiento/crear_perfil.py:_RESULTADO_POR_ESTADO": (
        "constante: estado del perfil → resultado de la operación; nadie la muta"
    ),
    "application/perfilamiento/crear_perfil.py:_RESULTADO_FUENTE": (
        "constante: estado de la fuente → resultado de la dependencia; nadie la muta"
    ),
    "application/perfilamiento/ejecutor_fuentes.py:_RESULTADO_RAMA": (
        "constante: estado de la fuente → resultado de la rama; nadie la muta"
    ),
    "domain/cotizacion/fallback.py:_NO_OBTENIDA": (
        "constante: estado de la fuente → motivo de la falla; nadie la muta"
    ),
    "domain/perfilamiento/motor.py:_MOTIVO_POR_ESTADO": (
        "constante: estado de la fuente → motivo del dato; nadie la muta"
    ),
    "domain/perfilamiento/motor.py:_SEVERIDAD": (
        "constante: orden de los niveles de riesgo; nadie la muta"
    ),
    "domain/perfilamiento/motor.py:_COMPARAR": (
        "constante: operador → función de comparación pura; nadie la muta"
    ),
    # Objetos que se construyen una vez al importar o al arrancar.
    "adapters/inbound/http/cotizaciones.py:router": (
        "APIRouter: las rutas se registran al importar; no cambia al atender solicitudes"
    ),
    "adapters/inbound/http/cotizaciones.py:router_productos": (
        "APIRouter: las rutas se registran al importar; no cambia al atender solicitudes"
    ),
    "adapters/inbound/http/perfilamiento.py:router": (
        "APIRouter: las rutas se registran al importar; no cambia al atender solicitudes"
    ),
    "adapters/inbound/http/suscripcion.py:router": (
        "APIRouter: las rutas se registran al importar; no cambia al atender solicitudes"
    ),
    "adapters/outbound/cotizaciones_pg.py:_INSERTAR": "texto SQL (str) armado una vez por _insertar",
    "adapters/outbound/cotizaciones_pg.py:_INSERTAR_DECISION": (
        "texto SQL (str) armado una vez por _insertar"
    ),
    "adapters/outbound/perfiles_pg.py:_INSERTAR_PERFIL": "texto SQL (str) armado una vez por _insertar",
    "adapters/outbound/perfiles_pg.py:_INSERTAR_LINAJE": "texto SQL (str) armado una vez por _insertar",
    "adapters/outbound/perfiles_pg.py:_PAQUETE": (
        "Traversable del paquete para leer las migraciones; solo lectura"
    ),
    "application/cotizacion/calcular_prima.py:logger": "logging.Logger del proceso; no guarda datos",
    "application/perfilamiento/crear_perfil.py:logger": "logging.Logger del proceso; no guarda datos",
    "application/perfilamiento/ejecutor_fuentes.py:logger": (
        "logging.Logger del proceso; no guarda datos"
    ),
    "config.py:settings": "Settings leídos del entorno una vez; nadie los modifica",
    "domain/cotizacion/prima.py:CONTEXTO": (
        "Context de decimal; solo se acumulan sus flags informativas; ningún resultado depende de "
        "ellas, porque los traps se evalúan por operación"
    ),
    "main.py:app": "la app de FastAPI que arranca uvicorn; su estado por tarea vive en app.state",
}

LITERALES_MUTABLES = (ast.Dict, ast.List, ast.Set, ast.DictComp, ast.ListComp, ast.SetComp)
CONSTRUCTORES_MUTABLES = {
    "dict", "list", "set", "defaultdict", "OrderedDict", "Counter", "deque", "bytearray",
}  # fmt: skip
LLAMADAS_INMUTABLES = {"frozenset", "tuple", "re.compile", "Decimal", "TypeVar", "MappingProxyType"}
MUTADORES = {
    "append", "extend", "update", "setdefault", "pop", "popitem", "clear", "add", "remove",
    "discard", "insert", "sort", "reverse",
}  # fmt: skip
ATRIBUTOS_CONTEXTO = {
    "flags", "traps", "prec", "rounding", "Emin", "Emax", "capitals", "clamp",
    "clear_flags", "clear_traps",
}  # fmt: skip
FUNCIONES_CONTEXTO_GLOBAL = {"getcontext", "setcontext"}


@dataclass(frozen=True)
class Hallazgo:
    archivo: str
    linea: int
    motivo: str

    def __str__(self) -> str:
        return f"{self.archivo}:{self.linea}: {self.motivo}"


def _archivos() -> Iterator[tuple[str, ast.Module]]:
    for ruta in sorted(SRC.rglob("*.py")):
        yield ruta.relative_to(SRC).as_posix(), ast.parse(ruta.read_text(encoding="utf-8"))


def _es_bloque_principal(sentencia: ast.stmt) -> bool:
    """`if __name__ == "__main__":` no corre al importar (p. ej. el generador de eventos.py)."""
    return isinstance(sentencia, ast.If) and ast.unparse(sentencia.test) in (
        "__name__ == '__main__'",
        "'__main__' == __name__",
    )


def _sentencias_de_modulo(cuerpo: list[ast.stmt]) -> Iterator[ast.stmt]:
    """Las sentencias que corren al importar, también dentro de if, try, with o for."""
    for sentencia in cuerpo:
        if isinstance(
            sentencia, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
        ) or _es_bloque_principal(sentencia):
            continue
        yield sentencia
        for campo in ("body", "orelse", "finalbody"):
            yield from _sentencias_de_modulo(getattr(sentencia, campo, []))
        for manejador in getattr(sentencia, "handlers", []):
            yield from _sentencias_de_modulo(manejador.body)


def _asignaciones_de_modulo(modulo: ast.Module) -> Iterator[tuple[str, int, ast.expr | None]]:
    for sentencia in _sentencias_de_modulo(modulo.body):
        objetivos: list[ast.expr] = []
        valor: ast.expr | None = None
        if isinstance(sentencia, ast.Assign):
            objetivos, valor = sentencia.targets, sentencia.value
        elif isinstance(sentencia, ast.AnnAssign | ast.AugAssign):
            objetivos, valor = [sentencia.target], sentencia.value
        for objetivo in objetivos:
            for nodo in ast.walk(objetivo):
                if isinstance(nodo, ast.Name):
                    yield nodo.id, sentencia.lineno, valor


def _clase_de_valor(valor: ast.expr | None) -> str | None:
    """'mutable', 'objeto' (lo construye una llamada no inmutable) o None (inmutable)."""
    if valor is None or isinstance(valor, LITERALES_MUTABLES):
        return None if valor is None else "mutable"
    if isinstance(valor, ast.Tuple | ast.BinOp | ast.BoolOp | ast.IfExp):
        partes = [_clase_de_valor(hijo) for hijo in ast.iter_child_nodes(valor)]
        return "mutable" if "mutable" in partes else ("objeto" if "objeto" in partes else None)
    if isinstance(valor, ast.Call):
        funcion = ast.unparse(valor.func)
        if funcion.split(".")[-1] in CONSTRUCTORES_MUTABLES:
            return "mutable"
        return None if funcion in LLAMADAS_INMUTABLES else "objeto"
    return None


def _nombres_de_modulo(modulo: ast.Module) -> set[str]:
    """Lo que una función puede alcanzar sin recibirlo: asignaciones e imports del módulo."""
    nombres = {nombre for nombre, _, _ in _asignaciones_de_modulo(modulo)}
    for sentencia in _sentencias_de_modulo(modulo.body):
        if isinstance(sentencia, ast.Import | ast.ImportFrom):
            nombres |= {(a.asname or a.name).split(".")[0] for a in sentencia.names}
    return nombres


def _raiz(nodo: ast.expr) -> str | None:
    """El nombre del que cuelga una cadena de atributos y subíndices (None si pasa por una llamada)."""
    while isinstance(nodo, ast.Attribute | ast.Subscript):
        nodo = nodo.value
    return nodo.id if isinstance(nodo, ast.Name) else None


def _locales(funcion: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) -> set[str]:
    """Parámetros y nombres que la función enlaza (sin entrar en funciones anidadas)."""
    argumentos = funcion.args
    nombres = {
        a.arg
        for a in [*argumentos.posonlyargs, *argumentos.args, *argumentos.kwonlyargs]
        + [a for a in (argumentos.vararg, argumentos.kwarg) if a is not None]
    }
    pendientes: list[ast.AST] = (
        [funcion.body] if isinstance(funcion, ast.Lambda) else list(funcion.body)
    )
    while pendientes:
        nodo = pendientes.pop()
        if isinstance(nodo, ast.Name) and isinstance(nodo.ctx, ast.Store):
            nombres.add(nodo.id)
        elif isinstance(nodo, ast.ExceptHandler) and nodo.name:
            nombres.add(nodo.name)
        elif isinstance(nodo, ast.Import | ast.ImportFrom):
            nombres |= {(a.asname or a.name).split(".")[0] for a in nodo.names}
        if isinstance(nodo, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            nombres.add(nodo.name)
            continue
        if isinstance(nodo, ast.Lambda):
            continue
        pendientes.extend(ast.iter_child_nodes(nodo))
    return nombres


class _Mutaciones(ast.NodeVisitor):
    """Mutaciones de nombres de módulo dentro de funciones, con los locales de cada ámbito."""

    def __init__(self, archivo: str, de_modulo: set[str]) -> None:
        self.archivo = archivo
        self.de_modulo = de_modulo
        self.ocultos: list[set[str]] = []
        self.hallazgos: list[Hallazgo] = []

    def _en_funcion(self, nodo: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) -> None:
        anterior = self.ocultos[-1] if self.ocultos else set()
        self.ocultos.append(anterior | _locales(nodo))
        self.generic_visit(nodo)
        self.ocultos.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_Lambda = _en_funcion

    def _de_modulo(self, nodo: ast.expr) -> str | None:
        raiz = _raiz(nodo)
        if not self.ocultos or raiz is None:
            return None  # A nivel de módulo (al importar) o sin nombre raíz.
        return raiz if raiz in self.de_modulo and raiz not in self.ocultos[-1] else None

    def _objetivo(self, objetivo: ast.expr, linea: int, accion: str) -> None:
        for nodo in ast.walk(objetivo):
            if isinstance(nodo, ast.Attribute | ast.Subscript) and isinstance(
                nodo.ctx, ast.Store | ast.Del
            ):
                nombre = self._de_modulo(nodo)
                if nombre:
                    self.hallazgos.append(Hallazgo(self.archivo, linea, f"{accion} {nombre}"))

    def visit_Assign(self, nodo: ast.Assign) -> None:
        for objetivo in nodo.targets:
            self._objetivo(objetivo, nodo.lineno, "asigna en el nombre de módulo")
        self.generic_visit(nodo)

    def visit_AugAssign(self, nodo: ast.AugAssign) -> None:
        self._objetivo(nodo.target, nodo.lineno, "asigna en el nombre de módulo")
        self.generic_visit(nodo)

    def visit_AnnAssign(self, nodo: ast.AnnAssign) -> None:
        self._objetivo(nodo.target, nodo.lineno, "asigna en el nombre de módulo")
        self.generic_visit(nodo)

    def visit_Delete(self, nodo: ast.Delete) -> None:
        for objetivo in nodo.targets:
            self._objetivo(objetivo, nodo.lineno, "borra en el nombre de módulo")
        self.generic_visit(nodo)

    def visit_Call(self, nodo: ast.Call) -> None:
        if isinstance(nodo.func, ast.Attribute) and nodo.func.attr in MUTADORES:
            nombre = self._de_modulo(nodo.func.value)
            if nombre:
                self.hallazgos.append(
                    Hallazgo(self.archivo, nodo.lineno, f".{nodo.func.attr}() sobre {nombre}")
                )
        self.generic_visit(nodo)


def _es_contexto(nodo: ast.expr) -> bool:
    return (isinstance(nodo, ast.Name) and nodo.id == "CONTEXTO") or (
        isinstance(nodo, ast.Attribute) and nodo.attr == "CONTEXTO"
    )


def _usos_del_contexto(archivo: str, modulo: ast.Module) -> Iterator[Hallazgo]:
    llamadas_validas: set[int] = set()
    for nodo in ast.walk(modulo):
        if isinstance(nodo, ast.Call) and ast.unparse(nodo.func).split(".")[-1] == "localcontext":
            if len(nodo.args) == 1 and not nodo.keywords and _es_contexto(nodo.args[0]):
                llamadas_validas.add(id(nodo.func))
            else:
                yield Hallazgo(
                    archivo, nodo.lineno, "localcontext con un argumento distinto de CONTEXTO"
                )
    for nodo in ast.walk(modulo):
        if (
            isinstance(nodo, ast.Attribute)
            and nodo.attr in ATRIBUTOS_CONTEXTO
            and _es_contexto(nodo.value)
        ):
            yield Hallazgo(archivo, nodo.lineno, f"usa CONTEXTO.{nodo.attr}")
        nombre = (
            nodo.id
            if isinstance(nodo, ast.Name)
            else nodo.attr
            if isinstance(nodo, ast.Attribute)
            else None
        )
        if nombre in FUNCIONES_CONTEXTO_GLOBAL:
            yield Hallazgo(archivo, nodo.lineno, f"usa {nombre} de decimal")
        elif nombre == "localcontext" and id(nodo) not in llamadas_validas:
            yield Hallazgo(
                archivo, nodo.lineno, "localcontext fuera de with localcontext(CONTEXTO)"
            )
        if isinstance(nodo, ast.ImportFrom) and nodo.module == "decimal":
            for alias in nodo.names:
                if alias.name in FUNCIONES_CONTEXTO_GLOBAL:
                    yield Hallazgo(archivo, nodo.lineno, f"importa {alias.name} de decimal")


def test_hay_codigo_que_revisar() -> None:
    archivos = [archivo for archivo, _ in _archivos()]
    assert "main.py" in archivos and "domain/cotizacion/prima.py" in archivos


def test_n1a_sin_global_ni_nonlocal() -> None:
    hallazgos = [
        Hallazgo(archivo, nodo.lineno, f"{type(nodo).__name__.lower()} {', '.join(nodo.names)}")
        for archivo, modulo in _archivos()
        for nodo in ast.walk(modulo)
        if isinstance(nodo, ast.Global | ast.Nonlocal)
    ]
    assert not hallazgos, "\n".join(map(str, hallazgos))


def test_n1a_objetos_de_modulo_no_inmutables_estan_permitidos() -> None:
    hallazgos = [
        Hallazgo(archivo, linea, f"{nombre}: valor {clase} de módulo fuera de PERMITIDOS")
        for archivo, modulo in _archivos()
        for nombre, linea, valor in _asignaciones_de_modulo(modulo)
        if (clase := _clase_de_valor(valor)) and f"{archivo}:{nombre}" not in PERMITIDOS
    ]
    assert not hallazgos, "\n".join(map(str, hallazgos))


def test_n1a_cada_permitido_sigue_existiendo_y_tiene_justificacion() -> None:
    existentes = {
        f"{archivo}:{nombre}": _clase_de_valor(valor)
        for archivo, modulo in _archivos()
        for nombre, _, valor in _asignaciones_de_modulo(modulo)
    }
    sobrantes = sorted(clave for clave in PERMITIDOS if existentes.get(clave) is None)
    assert not sobrantes, f"Permitidos que ya no existen o ya son inmutables: {sobrantes}"
    assert all(justificacion.strip() for justificacion in PERMITIDOS.values())


def test_n1a_ninguna_funcion_muta_un_nombre_de_modulo() -> None:
    hallazgos: list[Hallazgo] = []
    for archivo, modulo in _archivos():
        visitante = _Mutaciones(archivo, _nombres_de_modulo(modulo))
        visitante.visit(modulo)
        hallazgos += visitante.hallazgos
    assert not hallazgos, "\n".join(map(str, hallazgos))


def test_n1a_contexto_decimal_solo_con_localcontext_y_sin_tocar_sus_atributos() -> None:
    hallazgos = [h for archivo, modulo in _archivos() for h in _usos_del_contexto(archivo, modulo)]
    assert not hallazgos, "\n".join(map(str, hallazgos))


# --- N1b: las cachés existentes son de cada instancia ---------------------------------------
# Qué guardan y cuánto duran ya lo prueban: test_catalogo_http.py (UT-W11-13 a 15, sin TTL y
# versión cerrada), test_productos_catalogo.py (memoria con TTL, en frío, Valkey sin escribir) y
# test_hu_w06_fallback.py (reglas de fallback en memoria aun con Valkey, mientras rigen).

AHORA = datetime(2026, 10, 15, 15, 0, tzinfo=UTC)


class Reloj:
    def ahora(self) -> datetime:
        return AHORA


class Pedidos:
    """`pedir` de las cachés: devuelve un valor nuevo y distinto cada vez y cuenta las llamadas."""

    def __init__(self) -> None:
        self.valores: list[object] = []

    async def __call__(self) -> Any:
        self.valores.append(object())
        return self.valores[-1]


def _cliente() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500)))


def test_n1b_las_cachés_son_atributos_de_instancia_y_no_de_clase() -> None:
    for clase, atributos in (
        (CatalogoHttp, ("_cache", "_candados")),
        (ProductosCatalogo, ("_memoria", "_candados")),
    ):
        assert not set(atributos) & set(vars(clase)), clase.__name__


@pytest.mark.anyio
async def test_n1b_dos_catalogo_http_no_comparten_el_modelo_guardado() -> None:
    a = CatalogoHttp(_cliente(), "http://catalogo:8080", 60, 150, Reloj())
    b = CatalogoHttp(_cliente(), "http://catalogo:8080", 60, 150, Reloj())
    assert a._cache is not b._cache and a._candados is not b._candados
    pedidos_a, pedidos_b = Pedidos(), Pedidos()

    guardado_a = await a._con_cache("vigente:vida-hipotecario", timedelta(seconds=60), pedidos_a)
    assert await a._con_cache("vigente:vida-hipotecario", timedelta(seconds=60), pedidos_a) is (
        guardado_a
    )
    guardado_b = await b._con_cache("vigente:vida-hipotecario", timedelta(seconds=60), pedidos_b)

    assert (len(pedidos_a.valores), len(pedidos_b.valores)) == (1, 1)
    assert guardado_b is not guardado_a
    assert list(a._candados) == list(b._candados) == ["vigente:vida-hipotecario"]
    assert a._candados["vigente:vida-hipotecario"] is not b._candados["vigente:vida-hipotecario"]


@pytest.mark.anyio
async def test_n1b_dos_productos_catalogo_no_comparten_la_memoria() -> None:
    a = ProductosCatalogo(_cliente(), "http://catalogo:8080", 150, Reloj(), ttl_s=60)
    b = ProductosCatalogo(_cliente(), "http://catalogo:8080", 150, Reloj(), ttl_s=60)
    assert a._memoria is not b._memoria and a._candados is not b._candados
    pedidos_a, pedidos_b = Pedidos(), Pedidos()
    clave = "catalogo:v1:reglas-fallback:vida-hipotecario:vigente"

    guardado_a = await a._resolver(clave, pedidos_a, bytes.decode, solo_memoria=True)
    assert (await a._resolver(clave, pedidos_a, bytes.decode, solo_memoria=True)).valor is (
        guardado_a.valor
    )
    guardado_b = await b._resolver(clave, pedidos_b, bytes.decode, solo_memoria=True)

    assert (len(pedidos_a.valores), len(pedidos_b.valores)) == (1, 1)
    assert guardado_b.valor is not guardado_a.valor
    assert a._memoria[clave][0] is guardado_a.valor and b._memoria[clave][0] is guardado_b.valor
