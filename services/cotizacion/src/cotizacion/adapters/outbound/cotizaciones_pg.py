"""RepositorioCotizaciones en PostgreSQL (HU-W03). cotizacion es el único escritor de su base.

Los decimales del JSON van como texto (exactos); `prima_total` también va en su columna numérica
para consultas. La idempotencia es por actor: UNIQUE (actor_id, idempotency_key).

HU-W04: la oferta se guarda en `ofertas` en la misma transacción que la cotización y se lee con
ella (LEFT JOIN: las cotizaciones anteriores a la migración 004 no tienen oferta). El código legible
(COT-AAAA-NNNNN) lo asigna la secuencia `ofertas_codigo_seq` al insertar.

HU-W06: una cotización degradada guarda su decisión de fallback (`decisiones_fallback`) y el evento
CotizacionDegradada (outbox) en la misma transacción; se lee con ella (LEFT JOIN). Una decisión
PENDIENTE no tiene cotización: `registrar_pendiente` la guarda con su evento, una vez por actor y
clave de idempotencia.

Listado del portal ("Cotizaciones abiertas"): `listar` y `contar` leen las cotizaciones con oferta,
de la más reciente a la más antigua, con los índices de la migración 006.
"""

import dataclasses
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from cotizacion.adapters.outbound import outbox_pg
from cotizacion.domain.cotizacion.fallback import (
    AccionFallback,
    DecisionFallback,
    DecisionRegistrada,
    Faltante,
    MensajeFallback,
    MotivoFalla,
    ReglaAplicada,
    TipoDecision,
)
from cotizacion.domain.cotizacion.listado import FiltrosCotizaciones, ResumenCotizacion
from cotizacion.domain.cotizacion.modelos import (
    Actor,
    Cobertura,
    CoberturaOfertada,
    Cotizacion,
    Desglose,
    EntradasPrima,
    EstadoCotizacion,
    EstadoOferta,
    Exclusion,
    FactorAplicado,
    Oferta,
    RecargoAplicado,
    RolActor,
)
from cotizacion.domain.cotizacion.prima import desglose_canonico, entradas_canonicas, texto
from cotizacion.domain.perfilamiento.errores import ConflictoIdempotencia
from cotizacion.domain.perfilamiento.modelos import NivelRiesgo

_RESTRICCION_IDEMPOTENCIA = "cotizaciones_actor_id_idempotency_key_key"
_RESTRICCION_PENDIENTE = "decisiones_fallback_pendiente_idempotencia"

COLUMNAS = (
    "id",
    "cliente_id",
    "socio_id",
    "producto_id",
    "version_producto",
    "moneda",
    "periodicidad",
    "version_reglas",
    "hash_reglas",
    "perfil_id",
    "nivel_riesgo",
    "entradas",
    "coberturas",
    "desglose",
    "prima_total",
    "huella_calculo",
    "estado",
    "idempotency_key",
    "huella_solicitud",
    "calculado_en",
    "duracion_ms",
    "correlation_id",
    "actor_id",
    "actor_rol",
)


# Sin `codigo`: lo calcula el INSERT con la secuencia.
COLUMNAS_OFERTA = (
    "id",
    "cotizacion_id",
    "precio",
    "moneda",
    "periodicidad",
    "coberturas",
    "exclusiones",
    "version",
    "version_reglas",
    "creada_en",
    "vigente_hasta",
)


COLUMNAS_DECISION = (
    "id",
    "cotizacion_id",
    "perfil_id",
    "producto_id",
    "tipo",
    "faltantes",
    "mensaje_codigo",
    "version_reglas",
    "hash_reglas",
    "ajustes",
    "actor_id",
    "idempotency_key",
    "huella_solicitud",
    "correlation_id",
    "decidido_en",
)


def _insertar(columnas: Sequence[str], tabla: str = "cotizaciones") -> str:
    marcas = ", ".join(["%s"] * len(columnas))
    return f"INSERT INTO {tabla} ({', '.join(columnas)}) VALUES ({marcas})"


_INSERTAR = _insertar(COLUMNAS)
_INSERTAR_DECISION = _insertar(COLUMNAS_DECISION, "decisiones_fallback")
_PENDIENTE_POR_IDEMPOTENCIA = (
    f"SELECT {', '.join(COLUMNAS_DECISION)} FROM decisiones_fallback "
    "WHERE cotizacion_id IS NULL AND actor_id = %s AND idempotency_key = %s"
)
# COT-<año UTC de creada_en>-<secuencia, al menos 5 dígitos>.
_INSERTAR_OFERTA = (
    "WITH s AS (SELECT nextval('ofertas_codigo_seq')::text AS n) "
    f"INSERT INTO ofertas (codigo, {', '.join(COLUMNAS_OFERTA)}) "
    "SELECT 'COT-' || to_char(%s::timestamptz AT TIME ZONE 'UTC', 'YYYY') || '-' "
    "|| lpad(s.n, greatest(5, length(s.n)), '0'), "
    f"{', '.join(['%s'] * len(COLUMNAS_OFERTA))} FROM s RETURNING codigo"
)
_SELECT = (
    f"SELECT {', '.join(f'c.{c}' for c in COLUMNAS)}, o.codigo AS oferta_codigo, "
    f"{', '.join(f'o.{c} AS oferta_{c}' for c in COLUMNAS_OFERTA)}, "
    f"{', '.join(f'd.{c} AS decision_{c}' for c in COLUMNAS_DECISION)} "
    "FROM cotizaciones c LEFT JOIN ofertas o ON o.cotizacion_id = c.id "
    "LEFT JOIN decisiones_fallback d ON d.cotizacion_id = c.id"
)
_POR_ID = _SELECT + " WHERE c.id = %s"
_POR_IDEMPOTENCIA = _SELECT + " WHERE c.actor_id = %s AND c.idempotency_key = %s"

# Listado ("Cotizaciones abiertas"): solo las cotizaciones con oferta (HU-W04 en adelante).
_DESDE_LISTADO = (
    "FROM ofertas o JOIN cotizaciones c ON c.id = o.cotizacion_id "
    "LEFT JOIN decisiones_fallback d ON d.cotizacion_id = c.id"
)
_COLUMNAS_LISTADO = (
    "c.id AS cotizacion_id, o.codigo, c.cliente_id, c.socio_id, c.producto_id, "
    "c.version_producto, o.moneda, o.periodicidad, c.entradas->>'sumaAsegurada' AS suma_asegurada, "
    "o.precio, c.nivel_riesgo, d.id IS NOT NULL AS degradada, o.creada_en, o.vigente_hasta"
)


def _escapar_like(valor: str) -> str:
    return valor.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def filtros_listado(filtros: FiltrosCotizaciones, ahora: datetime) -> tuple[list[str], list[Any]]:
    """Condiciones del listado: estado de la oferta en `ahora`, socio y parte del código."""
    condiciones: list[str] = []
    parametros: list[Any] = []
    if filtros.estado is EstadoOferta.VIGENTE:
        condiciones.append("o.vigente_hasta > %s")
        parametros.append(ahora)
    elif filtros.estado is EstadoOferta.VENCIDA:
        condiciones.append("o.vigente_hasta <= %s")
        parametros.append(ahora)
    if filtros.socio_id is not None:
        condiciones.append("c.socio_id = %s")
        parametros.append(filtros.socio_id)
    if filtros.q and filtros.q.strip():
        condiciones.append("o.codigo ILIKE %s")
        parametros.append(f"%{_escapar_like(filtros.q.strip())}%")
    return condiciones, parametros


def _donde(condiciones: list[str]) -> str:
    return f" WHERE {' AND '.join(condiciones)}" if condiciones else ""


def resumen_de(fila: Mapping[str, Any]) -> ResumenCotizacion:
    return ResumenCotizacion(
        cotizacion_id=fila["cotizacion_id"],
        codigo=fila["codigo"],
        cliente_id=fila["cliente_id"],
        socio_id=fila["socio_id"],
        producto_id=fila["producto_id"],
        version_producto=fila["version_producto"],
        moneda=fila["moneda"],
        periodicidad=fila["periodicidad"],
        suma_asegurada=Decimal(fila["suma_asegurada"]),
        prima=_sin_ceros(fila["precio"]),
        nivel_riesgo=NivelRiesgo(fila["nivel_riesgo"]) if fila["nivel_riesgo"] else None,
        degradada=fila["degradada"],
        creada_en=fila["creada_en"],
        vigente_hasta=fila["vigente_hasta"],
    )


def fila_cotizacion(cotizacion: Cotizacion) -> dict[str, Any]:
    entradas = cotizacion.entradas
    return {
        "id": cotizacion.id,
        "cliente_id": cotizacion.cliente_id,
        "socio_id": cotizacion.socio_id,
        "producto_id": cotizacion.producto_id,
        "version_producto": cotizacion.version_producto,
        "moneda": cotizacion.moneda,
        "periodicidad": cotizacion.periodicidad,
        "version_reglas": cotizacion.version_reglas,
        "hash_reglas": cotizacion.hash_reglas,
        "perfil_id": cotizacion.perfil_id,
        "nivel_riesgo": entradas.nivel_riesgo.value if entradas.nivel_riesgo else None,
        "entradas": Jsonb(entradas_canonicas(entradas)),
        "coberturas": Jsonb(
            [
                {"codigo": c.codigo, "nombre": c.nombre, "obligatoria": c.obligatoria}
                for c in cotizacion.coberturas
            ]
        ),
        "desglose": Jsonb(desglose_canonico(cotizacion.desglose)),
        "prima_total": cotizacion.desglose.prima_total,
        "huella_calculo": cotizacion.huella_calculo,
        "estado": cotizacion.estado.value,
        "idempotency_key": cotizacion.idempotency_key,
        "huella_solicitud": cotizacion.huella_solicitud,
        "calculado_en": cotizacion.calculado_en,
        "duracion_ms": cotizacion.duracion_ms,
        "correlation_id": cotizacion.correlation_id,
        "actor_id": cotizacion.actor.id,
        "actor_rol": cotizacion.actor.rol.value,
    }


def fila_oferta(oferta: Oferta) -> dict[str, Any]:
    return {
        "id": oferta.id,
        "cotizacion_id": oferta.cotizacion_id,
        "precio": oferta.precio,
        "moneda": oferta.moneda,
        "periodicidad": oferta.periodicidad,
        "coberturas": Jsonb(
            [
                {"codigo": c.codigo, "nombre": c.nombre, "sumaAsegurada": texto(c.suma_asegurada)}
                for c in oferta.coberturas
            ]
        ),
        "exclusiones": Jsonb(
            [{"codigo": e.codigo, "descripcion": e.descripcion} for e in oferta.exclusiones]
        ),
        "version": oferta.version,
        "version_reglas": oferta.version_reglas,
        "creada_en": oferta.creada_en,
        "vigente_hasta": oferta.vigente_hasta,
    }


def fila_decision(registro: DecisionRegistrada) -> dict[str, Any]:
    decision = registro.decision
    return {
        "id": registro.id,
        "cotizacion_id": registro.cotizacion_id,
        "perfil_id": registro.perfil_id,
        "producto_id": registro.producto_id,
        "tipo": decision.tipo.value,
        "faltantes": Jsonb(
            [
                {
                    "fuente": f.fuente,
                    "dato": f.dato,
                    "motivo": f.motivo.value,
                    "obligatoria": f.obligatoria,
                    "regla": None
                    if f.regla is None
                    else {"id": f.regla.id, "version": f.regla.version, "accion": f.regla.accion},
                }
                for f in decision.faltantes
            ]
        ),
        "mensaje_codigo": decision.mensaje_codigo.value,
        "version_reglas": decision.version_reglas,
        "hash_reglas": decision.hash_reglas,
        "ajustes": Jsonb(
            [
                {"codigo": a.codigo, "valor": a.valor, "factor": texto(a.factor)}
                for a in decision.ajustes
            ]
        ),
        "actor_id": registro.actor_id,
        "idempotency_key": registro.idempotency_key,
        "huella_solicitud": registro.huella_solicitud,
        "correlation_id": registro.correlation_id,
        "decidido_en": registro.decidido_en,
    }


def decision_de(fila: Mapping[str, Any], prefijo: str = "") -> DecisionRegistrada | None:
    """La decisión de la fila (con `prefijo` si viene del LEFT JOIN); None si no hay."""
    if fila.get(f"{prefijo}id") is None:
        return None

    def campo(nombre: str) -> Any:
        return fila[f"{prefijo}{nombre}"]

    return DecisionRegistrada(
        id=campo("id"),
        decision=DecisionFallback(
            tipo=TipoDecision(campo("tipo")),
            faltantes=tuple(
                Faltante(
                    fuente=f["fuente"],
                    dato=f["dato"],
                    motivo=MotivoFalla(f["motivo"]),
                    obligatoria=f["obligatoria"],
                    regla=None
                    if f["regla"] is None
                    else ReglaAplicada(
                        f["regla"]["id"],
                        f["regla"]["version"],
                        AccionFallback(f["regla"]["accion"]),
                    ),
                )
                for f in campo("faltantes")
            ),
            mensaje_codigo=MensajeFallback(campo("mensaje_codigo")),
            version_reglas=campo("version_reglas"),
            hash_reglas=campo("hash_reglas"),
            ajustes=tuple(
                FactorAplicado(a["codigo"], a["valor"], Decimal(a["factor"]))
                for a in campo("ajustes")
            ),
        ),
        perfil_id=campo("perfil_id"),
        producto_id=campo("producto_id"),
        cotizacion_id=campo("cotizacion_id"),
        actor_id=campo("actor_id"),
        idempotency_key=campo("idempotency_key"),
        huella_solicitud=campo("huella_solicitud"),
        correlation_id=campo("correlation_id"),
        decidido_en=campo("decidido_en"),
    )


def _sin_ceros(valor: Decimal) -> Decimal:
    """numeric(20,4) vuelve con 4 decimales (97460.0000): se dejan los del monto (97460)."""
    entero = valor.to_integral_value()
    return entero if valor == entero else valor.normalize()


def oferta_de(fila: Mapping[str, Any]) -> Oferta | None:
    """La oferta de la fila leída con el LEFT JOIN; None si la cotización no tiene."""
    if fila.get("oferta_id") is None:
        return None
    return Oferta(
        id=fila["oferta_id"],
        cotizacion_id=fila["oferta_cotizacion_id"],
        precio=_sin_ceros(fila["oferta_precio"]),
        moneda=fila["oferta_moneda"],
        periodicidad=fila["oferta_periodicidad"],
        coberturas=tuple(
            CoberturaOfertada(c["codigo"], c["nombre"], Decimal(c["sumaAsegurada"]))
            for c in fila["oferta_coberturas"]
        ),
        exclusiones=tuple(
            Exclusion(e["codigo"], e["descripcion"]) for e in fila["oferta_exclusiones"]
        ),
        version=fila["oferta_version"],
        version_reglas=fila["oferta_version_reglas"],
        creada_en=fila["oferta_creada_en"],
        vigente_hasta=fila["oferta_vigente_hasta"],
        codigo=fila["oferta_codigo"],
    )


def _desglose(datos: Mapping[str, Any]) -> Desglose:
    return Desglose(
        prima_base=Decimal(datos["primaBase"]),
        factores=tuple(
            FactorAplicado(f["codigo"], f["valor"], Decimal(f["factor"])) for f in datos["factores"]
        ),
        prima_ajustada=Decimal(datos["primaAjustada"]),
        recargos=tuple(
            RecargoAplicado(r["cobertura"], Decimal(r["tasa"]), Decimal(r["valor"]))
            for r in datos["recargos"]
        ),
        subtotal=Decimal(datos["subtotal"]),
        ajuste_limite=Decimal(datos["ajusteLimite"]),
        prima_total=Decimal(datos["primaTotal"]),
    )


def cotizacion_de(fila: Mapping[str, Any]) -> Cotizacion:
    entradas = fila["entradas"]
    return Cotizacion(
        id=fila["id"],
        cliente_id=fila["cliente_id"],
        socio_id=fila["socio_id"],
        producto_id=fila["producto_id"],
        version_producto=fila["version_producto"],
        moneda=fila["moneda"],
        periodicidad=fila["periodicidad"],
        version_reglas=fila["version_reglas"],
        hash_reglas=fila["hash_reglas"],
        perfil_id=fila["perfil_id"],
        entradas=EntradasPrima(
            suma_asegurada=Decimal(entradas["sumaAsegurada"]),
            plazo_credito_meses=entradas["plazoCreditoMeses"],
            edad=entradas["edad"],
            nivel_riesgo=NivelRiesgo(entradas["nivelRiesgo"]) if entradas["nivelRiesgo"] else None,
            coberturas_opcionales=tuple(entradas["coberturasOpcionales"]),
        ),
        coberturas=tuple(
            Cobertura(c["codigo"], c["nombre"], c["obligatoria"]) for c in fila["coberturas"]
        ),
        desglose=_desglose(fila["desglose"]),
        huella_calculo=fila["huella_calculo"],
        estado=EstadoCotizacion(fila["estado"]),
        idempotency_key=fila["idempotency_key"],
        huella_solicitud=fila["huella_solicitud"],
        calculado_en=fila["calculado_en"],
        duracion_ms=fila["duracion_ms"],
        correlation_id=fila["correlation_id"],
        actor=Actor(fila["actor_id"], RolActor(fila["actor_rol"])),
        oferta=oferta_de(fila),
        decision=decision_de(fila, "decision_"),
    )


class CotizacionesPg:
    """Implementa RepositorioCotizaciones. Toma una conexión del pool solo durante cada operación."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def guardar(self, cotizacion: Cotizacion) -> Cotizacion:
        """Guarda la cotización, su oferta y, si es degradada, su decisión con el evento (HU-W06),
        en una transacción; devuelve la oferta con su código."""
        fila = fila_cotizacion(cotizacion)
        oferta = cotizacion.oferta
        try:
            # El bloque de la conexión es una transacción: confirma al salir, revierte si falla.
            async with self._pool.connection() as conexion:
                await conexion.execute(_INSERTAR, [fila[c] for c in COLUMNAS])
                if cotizacion.decision is not None:
                    datos_decision = fila_decision(cotizacion.decision)
                    await conexion.execute(
                        _INSERTAR_DECISION, [datos_decision[c] for c in COLUMNAS_DECISION]
                    )
                    await outbox_pg.insertar(
                        conexion, [outbox_pg.evento_decision(cotizacion.decision)]
                    )
                if oferta is None:
                    return cotizacion
                datos = fila_oferta(oferta)
                cursor = await conexion.execute(
                    _INSERTAR_OFERTA, [oferta.creada_en, *(datos[c] for c in COLUMNAS_OFERTA)]
                )
                fila_codigo = await cursor.fetchone()
                if fila_codigo is None:  # RETURNING siempre devuelve la fila insertada.
                    raise RuntimeError("el INSERT de la oferta no devolvió su código")
                (codigo,) = fila_codigo
        except psycopg.errors.UniqueViolation as error:
            if error.diag.constraint_name == _RESTRICCION_IDEMPOTENCIA:
                raise ConflictoIdempotencia() from error
            raise
        return dataclasses.replace(cotizacion, oferta=dataclasses.replace(oferta, codigo=codigo))

    async def registrar_pendiente(self, registro: DecisionRegistrada) -> DecisionRegistrada:
        """La decisión PENDIENTE con su evento, en una transacción. Repetida (mismo actor y clave),
        devuelve la guardada sin otro evento; con otra solicitud, ConflictoIdempotencia."""
        datos = fila_decision(registro)
        try:
            async with self._pool.connection() as conexion:
                await conexion.execute(_INSERTAR_DECISION, [datos[c] for c in COLUMNAS_DECISION])
                await outbox_pg.insertar(conexion, [outbox_pg.evento_decision(registro)])
        except psycopg.errors.UniqueViolation as error:
            if error.diag.constraint_name != _RESTRICCION_PENDIENTE:
                raise
            async with self._pool.connection() as conexion:
                cursor = conexion.cursor(row_factory=dict_row)
                await cursor.execute(
                    _PENDIENTE_POR_IDEMPOTENCIA, (registro.actor_id, registro.idempotency_key)
                )
                fila = await cursor.fetchone()
            existente = decision_de(fila) if fila else None
            if existente is None:  # La única fila que choca con la restricción es esa.
                raise
            if existente.huella_solicitud != registro.huella_solicitud:
                raise ConflictoIdempotencia() from error
            return existente
        return registro

    async def por_idempotencia(self, actor_id: str, clave: UUID) -> Cotizacion | None:
        return await self._uno(_POR_IDEMPOTENCIA, (actor_id, clave))

    async def por_id(self, cotizacion_id: UUID) -> Cotizacion | None:
        return await self._uno(_POR_ID, (cotizacion_id,))

    async def listar(
        self,
        filtros: FiltrosCotizaciones,
        ahora: datetime,
        limite: int,
        despues: tuple[datetime, UUID] | None,
    ) -> list[ResumenCotizacion]:
        condiciones, parametros = filtros_listado(filtros, ahora)
        if despues is not None:
            condiciones.append("(o.creada_en, c.id) < (%s, %s)")
            parametros.extend(despues)
        sql = (
            f"SELECT {_COLUMNAS_LISTADO} {_DESDE_LISTADO}{_donde(condiciones)} "
            "ORDER BY o.creada_en DESC, c.id DESC LIMIT %s"
        )
        async with self._pool.connection() as conexion:
            cursor = conexion.cursor(row_factory=dict_row)
            await cursor.execute(sql, [*parametros, limite])
            filas = await cursor.fetchall()
        return [resumen_de(f) for f in filas]

    async def contar(self, filtros: FiltrosCotizaciones, ahora: datetime) -> int:
        condiciones, parametros = filtros_listado(filtros, ahora)
        sql = f"SELECT count(*) {_DESDE_LISTADO}{_donde(condiciones)}"
        async with self._pool.connection() as conexion:
            cursor = await conexion.execute(sql, parametros)
            fila = await cursor.fetchone()
        return int(fila[0]) if fila else 0

    async def _uno(self, sql: str, parametros: tuple[Any, ...]) -> Cotizacion | None:
        async with self._pool.connection() as conexion:
            cursor = conexion.cursor(row_factory=dict_row)
            await cursor.execute(sql, parametros)
            fila = await cursor.fetchone()
        return cotizacion_de(fila) if fila else None
