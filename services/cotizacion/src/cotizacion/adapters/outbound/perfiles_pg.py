"""RepositorioPerfiles en PostgreSQL. cotizacion es el único escritor de su base.

`guardar` escribe el perfil, su linaje (una fila por fuente del modelo) y sus dos eventos de
outbox en una sola transacción. El linaje guarda solo los nombres de los campos; los valores
disponibles viven en `insumos` (columna marcada 'sensible').
"""

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from importlib import resources
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from cotizacion.adapters.outbound import outbox_pg
from cotizacion.domain.perfilamiento.errores import ConflictoIdempotencia
from cotizacion.domain.perfilamiento.modelos import (
    TIPO_DE_FUENTE,
    AccionSugerida,
    AjusteAplicado,
    DatoRequerido,
    DatosDeclarados,
    DecisionConsentimiento,
    Direccion,
    EstadoCampo,
    EstadoFactor,
    EstadoFuente,
    EstadoPerfil,
    FactorEvaluado,
    Fuente,
    MotivoDato,
    NivelRiesgo,
    Perfil,
    ResultadoEvaluacion,
    ResultadoFuente,
    ValorCampo,
    VerificacionConsentimiento,
)

_PAQUETE = resources.files("cotizacion.adapters.outbound")
_LOCK = "SELECT pg_advisory_lock(hashtext('cotizacion.migraciones'))"
_UNLOCK = "SELECT pg_advisory_unlock(hashtext('cotizacion.migraciones'))"
_RESTRICCION_IDEMPOTENCIA = "perfiles_riesgo_cliente_id_idempotency_key_key"

COLUMNAS_PERFIL = (
    "id",
    "cliente_id",
    "producto_id",
    "idempotency_key",
    "huella_solicitud",
    "datos_declarados",
    "insumos",
    "estado",
    "nivel_riesgo",
    "puntaje",
    "confianza",
    "factores",
    "ajustes",
    "datos_requeridos",
    "accion_sugerida",
    "consentimiento",
    "version_modelo",
    "hash_modelo",
    "perfil_anterior_id",
    "calculado_en",
    "duracion_ms",
    "correlation_id",
    "actor_id",
    "comprobacion_final",
)
COLUMNAS_LINAJE = (
    "perfil_id",
    "orden",
    "fuente",
    "tipo",
    "estado",
    "proveedor",
    "version_fuente",
    "confianza",
    "consultada_en",
    "duracion_ms",
    "campos_solicitados",
    "campos_disponibles",
)


def _insertar(tabla: str, columnas: Sequence[str]) -> str:
    marcas = ", ".join(["%s"] * len(columnas))
    return f"INSERT INTO {tabla} ({', '.join(columnas)}) VALUES ({marcas})"


_INSERTAR_PERFIL = _insertar("perfiles_riesgo", COLUMNAS_PERFIL)
_INSERTAR_LINAJE = _insertar("linaje_fuentes", COLUMNAS_LINAJE)
_SELECT = f"SELECT {', '.join(COLUMNAS_PERFIL)} FROM perfiles_riesgo"
_POR_ID = _SELECT + " WHERE id = %s"
_POR_IDEMPOTENCIA = _SELECT + " WHERE cliente_id = %s AND idempotency_key = %s"
_POR_CLIENTE = _SELECT + " WHERE cliente_id = %s ORDER BY calculado_en DESC, id LIMIT %s"
_LINAJE = (
    f"SELECT {', '.join(COLUMNAS_LINAJE)} FROM linaje_fuentes "
    "WHERE perfil_id = ANY(%s) ORDER BY perfil_id, orden"
)


def migrar(url: str) -> None:
    """Aplica los .sql en orden. Idempotente y serializado entre réplicas."""
    archivos = sorted(
        (f for f in _PAQUETE.joinpath("migraciones").iterdir() if f.name.endswith(".sql")),
        key=lambda f: f.name,
    )
    with psycopg.connect(url, autocommit=True) as conexion:
        conexion.execute(_LOCK)
        try:
            for archivo in archivos:
                conexion.execute(archivo.read_text(encoding="utf-8"))
        finally:
            conexion.execute(_UNLOCK)


# --- Perfil → filas ------------------------------------------------------------------------


def _texto(valor: Decimal | None) -> str | None:
    return None if valor is None else str(valor)


def _iso(instante: datetime | None) -> str | None:
    return None if instante is None else instante.isoformat()


def _verificacion(verificacion: VerificacionConsentimiento | None) -> Jsonb | None:
    """`consentimiento` y `comprobacion_final` comparten formato."""
    if verificacion is None:
        return None
    return Jsonb(
        {
            "decision": verificacion.decision.value,
            "consentimiento_id": _str(verificacion.consentimiento_id),
            "vigente_hasta": _iso(verificacion.vigente_hasta),
            "alcances_permitidos": sorted(a.value for a in verificacion.alcances_permitidos),
            "causa": verificacion.causa,
        }
    )


def fila_perfil(perfil: Perfil) -> dict[str, Any]:
    """Columnas de perfiles_riesgo. Los decimales del JSON van como texto (exactos)."""
    r = perfil.resultado
    declarados = perfil.datos_declarados
    return {
        "id": perfil.id,
        "cliente_id": perfil.cliente_id,
        "producto_id": perfil.producto_id,
        "idempotency_key": perfil.idempotency_key,
        "huella_solicitud": perfil.huella_solicitud,
        "datos_declarados": Jsonb(
            {
                "fecha_nacimiento": declarados.fecha_nacimiento.isoformat(),
                "plazo_credito_meses": declarados.plazo_credito_meses,
                "suma_asegurada": str(declarados.suma_asegurada),
            }
        ),
        "insumos": Jsonb({k: str(v) for k, v in perfil.insumos.items()}),
        "estado": r.estado.value,
        "nivel_riesgo": r.nivel.value if r.nivel else None,
        "puntaje": r.puntaje,
        "confianza": r.confianza,
        "factores": Jsonb(
            [
                {
                    "codigo": f.codigo,
                    "fuente": f.fuente,
                    "peso": str(f.peso),
                    "estado": f.estado.value,
                    "contribucion": _texto(f.contribucion),
                    "direccion": f.direccion.value if f.direccion else None,
                    "motivo": f.motivo.value if f.motivo else None,
                }
                for f in r.factores
            ]
        ),
        "ajustes": Jsonb(
            [
                {
                    "codigo": a.codigo,
                    "efecto": a.efecto,
                    "nivel": a.nivel.value if a.nivel else None,
                }
                for a in r.ajustes
            ]
        ),
        "datos_requeridos": Jsonb(
            [
                {
                    "campo": d.campo,
                    "fuente": d.fuente.value if d.fuente else None,
                    "motivo": d.motivo.value,
                }
                for d in r.datos_requeridos
            ]
        ),
        "accion_sugerida": r.accion.value if r.accion else None,
        "consentimiento": _verificacion(perfil.consentimiento),
        "version_modelo": perfil.version_modelo,
        "hash_modelo": perfil.hash_modelo,
        "perfil_anterior_id": perfil.perfil_anterior_id,
        "calculado_en": perfil.calculado_en,
        "duracion_ms": perfil.duracion_ms,
        "correlation_id": perfil.correlation_id,
        "actor_id": perfil.actor_id,
        "comprobacion_final": _verificacion(perfil.comprobacion_final),
    }


def _str(valor: UUID | None) -> str | None:
    return None if valor is None else str(valor)


def filas_linaje(perfil: Perfil) -> list[dict[str, Any]]:
    """Una fila por fuente del modelo, en su orden; solo los nombres de los campos."""
    return [
        {
            "perfil_id": perfil.id,
            "orden": orden,
            "fuente": r.fuente.value,
            "tipo": TIPO_DE_FUENTE[r.fuente].value,
            "estado": r.estado.value,
            "proveedor": r.proveedor,
            "version_fuente": r.version_fuente,
            "confianza": r.confianza,
            "consultada_en": r.consultada_en,
            "duracion_ms": r.duracion_ms,
            "campos_solicitados": list(r.campos_solicitados),
            "campos_disponibles": [
                c for c, v in r.campos.items() if v.estado is EstadoCampo.DISPONIBLE
            ],
        }
        for orden, r in enumerate(perfil.fuentes)
    ]


# --- Filas → Perfil ------------------------------------------------------------------------


def _decimal(valor: str | None) -> Decimal | None:
    return None if valor is None else Decimal(valor)


def _fuente(fila: Mapping[str, Any], insumos: Mapping[str, Decimal]) -> ResultadoFuente:
    estado = EstadoFuente(fila["estado"])
    disponibles = set(fila["campos_disponibles"])
    campos: dict[str, ValorCampo] = {}
    if estado is EstadoFuente.CONSULTADA:
        campos = {
            c: ValorCampo(EstadoCampo.DISPONIBLE, insumos.get(c))
            if c in disponibles
            else ValorCampo(EstadoCampo.NO_DISPONIBLE, None)
            for c in fila["campos_solicitados"]
        }
    return ResultadoFuente(
        fuente=Fuente(fila["fuente"]),
        estado=estado,
        campos_solicitados=tuple(fila["campos_solicitados"]),
        campos=campos,
        version_fuente=fila["version_fuente"],
        confianza=fila["confianza"],
        consultada_en=fila["consultada_en"],
        duracion_ms=fila["duracion_ms"],
        proveedor=fila["proveedor"],
    )


def _consentimiento(datos: Mapping[str, Any] | None) -> VerificacionConsentimiento | None:
    if datos is None:
        return None
    return VerificacionConsentimiento(
        decision=DecisionConsentimiento(datos["decision"]),
        consentimiento_id=UUID(datos["consentimiento_id"]) if datos["consentimiento_id"] else None,
        vigente_hasta=datetime.fromisoformat(datos["vigente_hasta"])
        if datos["vigente_hasta"]
        else None,
        alcances_permitidos=frozenset(Fuente(a) for a in datos["alcances_permitidos"]),
        causa=datos["causa"],
    )


def perfil_de(fila: Mapping[str, Any], linaje: Sequence[Mapping[str, Any]]) -> Perfil:
    """Reconstruye el Perfil desde su fila y su linaje (ordenado por `orden`)."""
    insumos = {k: Decimal(v) for k, v in fila["insumos"].items()}
    declarados = fila["datos_declarados"]
    resultado = ResultadoEvaluacion(
        estado=EstadoPerfil(fila["estado"]),
        nivel=NivelRiesgo(fila["nivel_riesgo"]) if fila["nivel_riesgo"] else None,
        puntaje=fila["puntaje"],
        confianza=fila["confianza"],
        factores=tuple(
            FactorEvaluado(
                codigo=f["codigo"],
                fuente=f["fuente"],
                peso=Decimal(f["peso"]),
                estado=EstadoFactor(f["estado"]),
                contribucion=_decimal(f["contribucion"]),
                direccion=Direccion(f["direccion"]) if f["direccion"] else None,
                motivo=MotivoDato(f["motivo"]) if f["motivo"] else None,
            )
            for f in fila["factores"]
        ),
        ajustes=tuple(
            AjusteAplicado(
                a["codigo"], a["efecto"], NivelRiesgo(a["nivel"]) if a["nivel"] else None
            )
            for a in fila["ajustes"]
        ),
        datos_requeridos=tuple(
            DatoRequerido(
                d["campo"], Fuente(d["fuente"]) if d["fuente"] else None, MotivoDato(d["motivo"])
            )
            for d in fila["datos_requeridos"]
        ),
        accion=AccionSugerida(fila["accion_sugerida"]) if fila["accion_sugerida"] else None,
    )
    return Perfil(
        id=fila["id"],
        cliente_id=fila["cliente_id"],
        producto_id=fila["producto_id"],
        idempotency_key=fila["idempotency_key"],
        huella_solicitud=fila["huella_solicitud"],
        datos_declarados=DatosDeclarados(
            fecha_nacimiento=date.fromisoformat(declarados["fecha_nacimiento"]),
            plazo_credito_meses=declarados["plazo_credito_meses"],
            suma_asegurada=Decimal(declarados["suma_asegurada"]),
        ),
        insumos=insumos,
        resultado=resultado,
        fuentes=tuple(_fuente(f, insumos) for f in linaje),
        consentimiento=_consentimiento(fila["consentimiento"]),
        version_modelo=fila["version_modelo"],
        hash_modelo=fila["hash_modelo"],
        perfil_anterior_id=fila["perfil_anterior_id"],
        calculado_en=fila["calculado_en"],
        duracion_ms=fila["duracion_ms"],
        correlation_id=fila["correlation_id"],
        actor_id=fila["actor_id"],
        comprobacion_final=_consentimiento(fila["comprobacion_final"]),
    )


class PerfilesPg:
    """Implementa RepositorioPerfiles. Toma una conexión del pool solo durante cada operación."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def guardar(self, perfil: Perfil) -> None:
        fila = fila_perfil(perfil)
        try:
            async with self._pool.connection() as conexion, conexion.transaction():
                await conexion.execute(_INSERTAR_PERFIL, [fila[c] for c in COLUMNAS_PERFIL])
                for linaje in filas_linaje(perfil):
                    await conexion.execute(_INSERTAR_LINAJE, [linaje[c] for c in COLUMNAS_LINAJE])
                await outbox_pg.insertar(
                    conexion, outbox_pg.eventos_de(perfil, perfil.correlation_id)
                )
        except psycopg.errors.UniqueViolation as error:
            if error.diag.constraint_name == _RESTRICCION_IDEMPOTENCIA:
                raise ConflictoIdempotencia() from error
            raise

    async def por_idempotencia(self, cliente_id: UUID, clave: UUID) -> Perfil | None:
        perfiles = await self._leer(_POR_IDEMPOTENCIA, (cliente_id, clave))
        return perfiles[0] if perfiles else None

    async def por_id(self, perfil_id: UUID) -> Perfil | None:
        perfiles = await self._leer(_POR_ID, (perfil_id,))
        return perfiles[0] if perfiles else None

    async def por_cliente(self, cliente_id: UUID, limite: int) -> list[Perfil]:
        return await self._leer(_POR_CLIENTE, (cliente_id, limite))

    async def _leer(self, sql: str, parametros: tuple[Any, ...]) -> list[Perfil]:
        async with self._pool.connection() as conexion:
            cursor = conexion.cursor(row_factory=dict_row)
            await cursor.execute(sql, parametros)
            filas = await cursor.fetchall()
            if not filas:
                return []
            await cursor.execute(_LINAJE, ([f["id"] for f in filas],))
            linajes = await cursor.fetchall()
        por_perfil: dict[UUID, list[dict[str, Any]]] = {f["id"]: [] for f in filas}
        for linaje in linajes:
            por_perfil[linaje["perfil_id"]].append(linaje)
        return [perfil_de(f, por_perfil[f["id"]]) for f in filas]
