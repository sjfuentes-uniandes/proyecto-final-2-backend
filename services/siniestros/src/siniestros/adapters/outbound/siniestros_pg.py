"""SiniestrosRepositorio en PostgreSQL (HU-W19). `siniestros` es el único escritor de su base.

- **Lecturas** (`bandeja`, `indicadores`, `detalle`, `evidencia`): cada una toma una conexión del pool
  solo mientras dura su consulta y la devuelve antes de que nadie espere a S3 u a otro servicio.
- **Escrituras** (`transaccion`): una transacción corta con el bloqueo, el cambio, el historial, el
  Outbox y la idempotencia, juntos.
- **Tiempo restante en SQL.** Filtrar por prioridad y ordenar la bandeja por tiempo restante exige
  calcularlo en la consulta, con la misma fórmula del dominio (`plazos.tiempo_restante`):
  `floor((reportado_en + pausa + compromiso − COALESCE(pausado_desde, decidido_en, corte)) / 60 s)`.
  Las pruebas de módulo comparan ambos cálculos. Un reloj atrasado respecto al reporte (que el
  dominio acota) no se considera aquí.
- **Cursor.** `base64url(JSON)` con la clave del último elemento y el `corte` de la primera página:
  así el orden por tiempo restante no se mueve entre páginas.
"""

import base64
import binascii
import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg
from psycopg import AsyncConnection, AsyncCursor
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from siniestros.adapters.outbound import outbox_pg
from siniestros.adapters.outbound.mapeo import (
    copia_a_json,  # noqa: F401 - la semilla y las pruebas arman filas con el mismo mapeo
    copia_de_json,
    ubicacion_de_json,
)
from siniestros.application.siniestro.errores import SolicitudInvalida
from siniestros.application.siniestro.puertos import DecisionFinalDuplicada, RegistroIdempotencia
from siniestros.application.siniestro.vistas import (
    AccesoDenegado,
    DecisionRegistrada,
    DetalleLeido,
    FiltroBandeja,
    Indicadores,
    LiquidacionAprobada,
    PaginaSiniestros,
    TipoBandeja,
)
from siniestros.domain.siniestro.modelos import (
    CanalSoporte,
    CausalRechazo,
    EntradaHistorial,
    EstadoSiniestro,
    Evidencia,
    OrigenSiniestro,
    PrioridadSiniestro,
    Siniestro,
    SoporteSolicitable,
    TipoEvidencia,
    TipoSoporte,
    Ubicacion,
)
from siniestros.domain.siniestro.pago import EstadoPago, PagoSiniestro, ResultadoPago
from siniestros.domain.siniestro.transiciones import referencia_pago

COLUMNAS = (
    "id, codigo, origen, estado, poliza_id, cliente_id, copia_poliza, causa, fecha_evento, "
    "ubicacion_declarada, reportado_en, analista_id, asignado_en, pausado_desde, "
    "pausa_acumulada_s, plazo_soporte_vence_en, decidido_en, version, correlation_id, "
    "valor_reclamado, moneda, deducible, valor_indemnizar"
)
_COLUMNAS_EVIDENCIA = (
    "id, siniestro_id, nombre_archivo, tipo, soporte_tipo, clave_objeto, sha256, capturada_en, "
    "ubicacion, tamano_bytes, tipo_contenido"
)
_COLUMNAS_PAGO = (
    "pago_estado, pago_referencia, pago_transaccion_id, pago_motivo, pago_intentos, "
    "pago_duplicados_descartados"
)
_COLUMNAS_HISTORIAL = (
    "id, siniestro_id, accion, estado_anterior, estado_nuevo, actor_id, actor_rol, actor_nombre, "
    "justificacion, detalles, ocurrido_en"
)

# Tiempo restante en minutos (negativo si venció); ver el docstring del módulo.
RESTANTE_MIN = (
    "floor(extract(epoch FROM (reportado_en + make_interval(secs => pausa_acumulada_s) "
    "+ make_interval(hours => %(compromiso_h)s) "
    "- COALESCE(pausado_desde, decidido_en, %(corte)s))) / 60)::int"
)
_LIMITE_TEXTO = 64


class _ConDict:
    """Ejecuta con filas en diccionario **sin cambiar** la fábrica de filas de la conexión: el pool
    la comparte con el relay del Outbox, que lee tuplas."""

    def __init__(self, conexion: AsyncConnection[Any]) -> None:
        self._conexion = conexion

    async def execute(self, consulta: str, parametros: Any = None) -> AsyncCursor[dict[str, Any]]:
        cursor = self._conexion.cursor(row_factory=dict_row)
        await cursor.execute(consulta, parametros)
        return cursor


def siniestro_de(fila: Mapping[str, Any]) -> Siniestro:
    return Siniestro(
        id=fila["id"],
        codigo=fila["codigo"],
        origen=OrigenSiniestro(fila["origen"]),
        estado=EstadoSiniestro(fila["estado"]),
        poliza_id=fila["poliza_id"],
        cliente_id=fila["cliente_id"],
        copia_poliza=copia_de_json(fila["copia_poliza"]),
        causa=fila["causa"],
        fecha_evento=fila["fecha_evento"],
        ubicacion_declarada=ubicacion_de_json(fila["ubicacion_declarada"]) or Ubicacion(),
        reportado_en=fila["reportado_en"],
        correlation_id=fila["correlation_id"],
        analista_id=fila["analista_id"],
        asignado_en=fila["asignado_en"],
        pausado_desde=fila["pausado_desde"],
        pausa_acumulada_s=fila["pausa_acumulada_s"],
        plazo_soporte_vence_en=fila["plazo_soporte_vence_en"],
        decidido_en=fila["decidido_en"],
        version=fila["version"],
        valor_reclamado=fila["valor_reclamado"],
        moneda=fila["moneda"].strip() if fila["moneda"] else None,
        deducible=fila["deducible"],
        valor_indemnizar=fila["valor_indemnizar"],
    )


def evidencia_de(fila: Mapping[str, Any]) -> Evidencia:
    return Evidencia(
        id=fila["id"],
        siniestro_id=fila["siniestro_id"],
        nombre_archivo=fila["nombre_archivo"],
        tipo=TipoEvidencia(fila["tipo"]),
        soporte_tipo=TipoSoporte(fila["soporte_tipo"]) if fila["soporte_tipo"] else None,
        clave_objeto=fila["clave_objeto"],
        sha256=fila["sha256"],
        capturada_en=fila["capturada_en"],
        ubicacion=ubicacion_de_json(fila["ubicacion"]),
        tamano_bytes=fila["tamano_bytes"],
        tipo_contenido=fila["tipo_contenido"],
    )


def entrada_de(fila: Mapping[str, Any]) -> EntradaHistorial:
    anterior, nuevo = fila["estado_anterior"], fila["estado_nuevo"]
    return EntradaHistorial(
        id=fila["id"],
        siniestro_id=fila["siniestro_id"],
        accion=fila["accion"],
        ocurrido_en=fila["ocurrido_en"],
        estado_anterior=EstadoSiniestro(anterior) if anterior else None,
        estado_nuevo=EstadoSiniestro(nuevo) if nuevo else None,
        actor_id=fila["actor_id"],
        actor_rol=fila["actor_rol"],
        actor_nombre=fila["actor_nombre"],
        justificacion=fila["justificacion"],
        detalles=fila["detalles"],
    )


def pago_de_fila(fila: Mapping[str, Any]) -> PagoSiniestro | None:
    """El pago que informó `dinero` (HU-W23); null si `pago_estado` es nulo (SIN_PAGO)."""
    if fila["pago_estado"] is None:
        return None
    return PagoSiniestro(
        estado=EstadoPago(fila["pago_estado"]),
        referencia=fila["pago_referencia"],
        transaccion_id=fila["pago_transaccion_id"],
        motivo=fila["pago_motivo"],
        intentos=fila["pago_intentos"],
        duplicados_descartados=fila["pago_duplicados_descartados"],
    )


def liquidacion_de(
    siniestro: Siniestro, decision: Mapping[str, Any] | None
) -> LiquidacionAprobada | None:
    """La liquidación de un caso aprobado. Sale de las columnas del siniestro y, si la decisión del
    analista existe, de su detalle (de ahí el valor reclamado que usó). Un paramétrico (W22) no tiene
    decisión: solo trae el valor a indemnizar."""
    if siniestro.estado is not EstadoSiniestro.APROBADO or siniestro.valor_indemnizar is None:
        return None
    reclamado = siniestro.valor_reclamado
    moneda = siniestro.moneda or siniestro.copia_poliza.moneda
    aprobada_en = siniestro.decidido_en
    if decision is not None:
        liquidada = decision["detalle"].get("liquidacion", {})
        if liquidada.get("valorReclamado") is not None:
            reclamado = Decimal(liquidada["valorReclamado"])
        moneda = liquidada.get("moneda", moneda)
        aprobada_en = decision["registrada_en"]
    assert aprobada_en is not None  # un caso aprobado tiene decidido_en
    return LiquidacionAprobada(
        moneda=moneda,
        valor_reclamado=reclamado,
        deducible=siniestro.deducible,
        valor_indemnizar=siniestro.valor_indemnizar,
        referencia_pago=referencia_pago(siniestro.codigo),
        aprobada_en=aprobada_en,
    )


# --- Cursor ---------------------------------------------------------------------------------------
def codificar_cursor(clave: str | int, identificador: UUID, corte: datetime) -> str:
    cuerpo = json.dumps({"k": clave, "i": str(identificador), "t": corte.isoformat()})
    return base64.urlsafe_b64encode(cuerpo.encode()).decode().rstrip("=")


def decodificar_cursor(cursor: str) -> tuple[str | int, UUID, datetime]:
    try:
        relleno = "=" * (-len(cursor) % 4)
        datos = json.loads(base64.urlsafe_b64decode(cursor + relleno))
        clave, identificador, corte = (
            datos["k"],
            UUID(datos["i"]),
            datetime.fromisoformat(datos["t"]),
        )
        if not isinstance(clave, str | int) or isinstance(clave, bool) or corte.utcoffset() is None:
            raise ValueError
    except (ValueError, KeyError, TypeError, binascii.Error) as error:
        raise SolicitudInvalida(["cursor"]) from error
    return clave, identificador, corte


def _patron_like(texto: str) -> str:
    escapado = texto.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escapado[:_LIMITE_TEXTO]}%"


def _filtros(filtro: FiltroBandeja, corte: datetime) -> tuple[list[str], dict[str, Any]]:
    condiciones: list[str] = []
    parametros: dict[str, Any] = {
        "corte": corte,
        "compromiso_h": filtro.plazos.compromiso_horas,
        "alta": filtro.plazos.alta_min,
        "media": filtro.plazos.media_min,
    }
    if filtro.bandeja is TipoBandeja.MIA:
        condiciones.append("analista_id = %(actor)s")
        parametros["actor"] = filtro.actor_id
    else:
        condiciones.append("estado = 'REPORTADO' AND analista_id IS NULL")
    if filtro.estado is not None:
        condiciones.append("estado = %(estado)s")
        parametros["estado"] = filtro.estado.value
    if filtro.origen is not None:
        condiciones.append("origen = %(origen)s")
        parametros["origen"] = filtro.origen.value
    if filtro.prioridad is PrioridadSiniestro.ALTA:
        condiciones.append(f"{RESTANTE_MIN} < %(alta)s")
    elif filtro.prioridad is PrioridadSiniestro.MEDIA:
        condiciones.append(f"{RESTANTE_MIN} >= %(alta)s AND {RESTANTE_MIN} < %(media)s")
    elif filtro.prioridad is PrioridadSiniestro.BAJA:
        condiciones.append(f"{RESTANTE_MIN} >= %(media)s")
    if filtro.texto:
        condiciones.append("(codigo ILIKE %(texto)s OR copia_poliza->>'numero' ILIKE %(texto)s)")
        parametros["texto"] = _patron_like(filtro.texto)
    return condiciones, parametros


class SiniestrosPg:
    def __init__(self, pool: "AsyncConnectionPool[Any]") -> None:
        self._pool = pool

    @asynccontextmanager
    async def transaccion(self) -> AsyncIterator["TxSiniestrosPg"]:
        async with self._pool.connection() as conexion, conexion.transaction():
            yield TxSiniestrosPg(conexion)

    async def bandeja(self, filtro: FiltroBandeja) -> PaginaSiniestros:
        corte = filtro.corte
        cursor = decodificar_cursor(filtro.cursor) if filtro.cursor else None
        if cursor is not None:
            corte = cursor[2]
        condiciones, parametros = _filtros(filtro, corte)
        donde = " AND ".join(condiciones)

        por_restante = filtro.bandeja is TipoBandeja.MIA
        clave_orden = RESTANTE_MIN if por_restante else "reportado_en"
        paginada = list(condiciones)
        if cursor is not None:
            paginada.append(f"({clave_orden}, id) > (%(clave)s, %(id)s)")
            parametros_pagina = {**parametros, "clave": cursor[0], "id": cursor[1]}
            if not por_restante:
                parametros_pagina["clave"] = datetime.fromisoformat(str(cursor[0]))
        else:
            parametros_pagina = dict(parametros)
        parametros_pagina["limite"] = filtro.limite + 1

        consulta = (
            f"SELECT {COLUMNAS}, {clave_orden} AS clave_orden FROM siniestros "
            f"WHERE {' AND '.join(paginada)} ORDER BY {clave_orden}, id LIMIT %(limite)s"
        )
        async with self._pool.connection() as bruta:
            conexion = _ConDict(bruta)
            filas = await (await conexion.execute(consulta, parametros_pagina)).fetchall()
            total_fila = await (
                await conexion.execute(
                    f"SELECT count(*) AS total FROM siniestros WHERE {donde}", parametros
                )
            ).fetchone()
        assert total_fila is not None

        hay_mas = len(filas) > filtro.limite
        pagina = filas[: filtro.limite]
        siguiente: str | None = None
        if hay_mas:
            ultima = pagina[-1]
            clave = ultima["clave_orden"] if por_restante else ultima["clave_orden"].isoformat()
            siguiente = codificar_cursor(clave, ultima["id"], corte)
        return PaginaSiniestros(
            items=tuple(siniestro_de(f) for f in pagina),
            total=int(total_fila["total"]),
            siguiente_cursor=siguiente,
        )

    async def indicadores(
        self,
        actor_id: UUID,
        desde: datetime,
        hasta: datetime,
        corte: datetime,
        compromiso_horas: int,
    ) -> Indicadores:
        consulta = """
        SELECT
          count(*) FILTER (WHERE estado = 'EN_EVALUACION' AND analista_id = %(actor)s) AS pendientes,
          count(*) FILTER (WHERE estado = 'EN_EVALUACION' AND analista_id = %(actor)s
                             AND vence >= %(desde)s AND vence < %(hasta)s) AS vencen_hoy,
          count(*) FILTER (WHERE estado = 'EN_EVALUACION' AND analista_id = %(actor)s
                             AND vence < %(corte)s) AS fuera_de_compromiso,
          count(*) FILTER (WHERE estado IN ('APROBADO', 'RECHAZADO') AND analista_id = %(actor)s
                             AND decidido_en >= %(hasta)s - interval '7 days'
                             AND decidido_en < %(hasta)s) AS resueltos_semana,
          count(*) FILTER (WHERE estado IN ('APROBADO', 'RECHAZADO') AND analista_id = %(actor)s
                             AND decidido_en >= %(hasta)s - interval '14 days'
                             AND decidido_en < %(hasta)s - interval '7 days') AS resueltos_previa,
          avg(extract(epoch FROM decidido_en - reportado_en) / 3600.0) FILTER (
                WHERE estado IN ('APROBADO', 'RECHAZADO') AND analista_id = %(actor)s
                  AND decidido_en >= %(hasta)s - interval '7 days'
                  AND decidido_en < %(hasta)s) AS tiempo_medio_h,
          count(*) FILTER (WHERE estado IN ('EN_EVALUACION', 'FALTA_SOPORTE')
                             AND analista_id = %(actor)s) AS asignados,
          count(*) FILTER (WHERE estado = 'REPORTADO' AND analista_id IS NULL) AS sin_asignar
        FROM (
          SELECT *, reportado_en + make_interval(secs => pausa_acumulada_s)
                    + make_interval(hours => %(compromiso_h)s) AS vence
          FROM siniestros
        ) s
        """
        async with self._pool.connection() as bruta:
            conexion = _ConDict(bruta)
            fila = await (
                await conexion.execute(
                    consulta,
                    {
                        "actor": actor_id,
                        "desde": desde,
                        "hasta": hasta,
                        "corte": corte,
                        "compromiso_h": compromiso_horas,
                    },
                )
            ).fetchone()
        assert fila is not None
        medio = fila["tiempo_medio_h"]
        return Indicadores(
            pendientes=fila["pendientes"],
            vencen_hoy=fila["vencen_hoy"],
            fuera_de_compromiso=fila["fuera_de_compromiso"],
            resueltos_semana=fila["resueltos_semana"],
            resueltos_semana_previa=fila["resueltos_previa"],
            tiempo_medio_decision_horas=round(float(medio), 2) if medio is not None else None,
            compromiso_horas=compromiso_horas,
            asignados_al_actor=fila["asignados"],
            sin_asignar=fila["sin_asignar"],
        )

    async def detalle(self, siniestro_id: UUID) -> DetalleLeido | None:
        async with self._pool.connection() as bruta, bruta.transaction():
            conexion = _ConDict(bruta)
            fila = await (
                await conexion.execute(
                    f"SELECT {COLUMNAS} FROM siniestros WHERE id = %s", (siniestro_id,)
                )
            ).fetchone()
            if fila is None:
                return None
            evidencias = await (
                await conexion.execute(
                    f"SELECT {_COLUMNAS_EVIDENCIA} FROM evidencias WHERE siniestro_id = %s "
                    "ORDER BY capturada_en NULLS LAST, nombre_archivo, id",
                    (siniestro_id,),
                )
            ).fetchall()
            historial = await (
                await conexion.execute(
                    f"SELECT {_COLUMNAS_HISTORIAL} FROM historial_siniestro WHERE siniestro_id = %s "
                    "ORDER BY ocurrido_en, id",
                    (siniestro_id,),
                )
            ).fetchall()
            caso = siniestro_de(fila)
            decision = None
            pago = None
            if caso.estado is EstadoSiniestro.APROBADO:
                decision = await (
                    await conexion.execute(
                        "SELECT detalle, registrada_en FROM decisiones_siniestro "
                        "WHERE siniestro_id = %s AND tipo = 'APROBAR'",
                        (siniestro_id,),
                    )
                ).fetchone()
                pago_fila = await (
                    await conexion.execute(
                        f"SELECT {_COLUMNAS_PAGO} FROM siniestros WHERE id = %s", (siniestro_id,)
                    )
                ).fetchone()
                assert pago_fila is not None
                pago = pago_de_fila(pago_fila)
        return DetalleLeido(
            siniestro=caso,
            evidencias=tuple(evidencia_de(f) for f in evidencias),
            historial=tuple(entrada_de(f) for f in historial),
            liquidacion=liquidacion_de(caso, decision),
            pago=pago,
        )

    async def evidencia(
        self, siniestro_id: UUID, evidencia_id: UUID
    ) -> tuple[bool, Evidencia | None]:
        async with self._pool.connection() as bruta:
            conexion = _ConDict(bruta)
            existe = await (
                await conexion.execute(
                    "SELECT 1 AS existe FROM siniestros WHERE id = %s", (siniestro_id,)
                )
            ).fetchone()
            if existe is None:
                return False, None
            fila = await (
                await conexion.execute(
                    f"SELECT {_COLUMNAS_EVIDENCIA} FROM evidencias WHERE id = %s AND siniestro_id = %s",
                    (evidencia_id, siniestro_id),
                )
            ).fetchone()
        return True, evidencia_de(fila) if fila else None


class TxSiniestrosPg:
    """Implementa `TxSiniestros` sobre la transacción abierta de `SiniestrosPg.transaccion`."""

    def __init__(self, conexion: AsyncConnection[Any]) -> None:
        self._bruta = conexion
        self._conexion = _ConDict(conexion)

    async def adquirir_idempotencia(self, clave: str) -> RegistroIdempotencia | None:
        # Serializa las solicitudes con la misma clave: la segunda espera al commit de la primera.
        await self._conexion.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (clave,))
        fila = await (
            await self._conexion.execute(
                "SELECT huella_solicitud, respuesta FROM idempotencia WHERE clave = %s", (clave,)
            )
        ).fetchone()
        if fila is None:
            return None
        return RegistroIdempotencia(fila["huella_solicitud"], fila["respuesta"])

    async def guardar_idempotencia(
        self, clave: str, huella: str, respuesta: dict[str, Any], creada_en: datetime
    ) -> None:
        await self._conexion.execute(
            "INSERT INTO idempotencia (clave, huella_solicitud, respuesta, creada_en) "
            "VALUES (%s, %s, %s, %s)",
            (clave, huella, Jsonb(respuesta), creada_en),
        )

    async def siniestro(self, siniestro_id: UUID) -> Siniestro | None:
        fila = await (
            await self._conexion.execute(
                f"SELECT {COLUMNAS} FROM siniestros WHERE id = %s", (siniestro_id,)
            )
        ).fetchone()
        return siniestro_de(fila) if fila else None

    async def reservar_siguiente(self) -> Siniestro | None:
        fila = await (
            await self._conexion.execute(
                f"SELECT {COLUMNAS} FROM siniestros "
                "WHERE estado = 'REPORTADO' AND analista_id IS NULL "
                "ORDER BY reportado_en, id FOR UPDATE SKIP LOCKED LIMIT 1"
            )
        ).fetchone()
        return siniestro_de(fila) if fila else None

    async def guardar_asignacion(self, siniestro: Siniestro, version_anterior: int) -> None:
        cursor = await self._conexion.execute(
            "UPDATE siniestros SET estado = %s, analista_id = %s, asignado_en = %s, version = %s "
            "WHERE id = %s AND version = %s",
            (
                siniestro.estado.value,
                siniestro.analista_id,
                siniestro.asignado_en,
                siniestro.version,
                siniestro.id,
                version_anterior,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("guardar_asignacion: el siniestro cambió de versión")

    async def agregar_historial(self, entrada: EntradaHistorial) -> None:
        await self._conexion.execute(
            "INSERT INTO historial_siniestro (id, siniestro_id, accion, estado_anterior, "
            "estado_nuevo, actor_id, actor_rol, actor_nombre, justificacion, "
            "evidencias_consideradas, detalles, ocurrido_en) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                entrada.id,
                entrada.siniestro_id,
                entrada.accion,
                entrada.estado_anterior.value if entrada.estado_anterior else None,
                entrada.estado_nuevo.value if entrada.estado_nuevo else None,
                entrada.actor_id,
                entrada.actor_rol,
                entrada.actor_nombre,
                entrada.justificacion,
                Jsonb([str(e) for e in entrada.evidencias_consideradas])
                if entrada.evidencias_consideradas
                else None,
                Jsonb(dict(entrada.detalles)) if entrada.detalles is not None else None,
                entrada.ocurrido_en,
            ),
        )

    async def estado_pago(self, siniestro_id: UUID) -> EstadoPago | None:
        fila = await (
            await self._conexion.execute(
                "SELECT pago_estado FROM siniestros WHERE id = %s", (siniestro_id,)
            )
        ).fetchone()
        return EstadoPago(fila["pago_estado"]) if fila and fila["pago_estado"] else None

    async def guardar_pago(self, resultado: ResultadoPago) -> None:
        cursor = await self._conexion.execute(
            "UPDATE siniestros SET pago_estado = %s, pago_referencia = %s, "
            "pago_transaccion_id = %s, pago_motivo = %s, pago_intentos = %s, "
            "pago_duplicados_descartados = %s WHERE id = %s AND pago_estado IS NULL",
            (
                resultado.estado.value,
                resultado.referencia,
                resultado.transaccion_id,
                resultado.motivo,
                resultado.intentos,
                resultado.duplicados_descartados,
                resultado.siniestro_id,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("guardar_pago: el siniestro ya tenía un resultado de pago")

    async def bloquear_siniestro(self, siniestro_id: UUID) -> Siniestro | None:
        fila = await (
            await self._conexion.execute(
                f"SELECT {COLUMNAS} FROM siniestros WHERE id = %s FOR UPDATE", (siniestro_id,)
            )
        ).fetchone()
        return siniestro_de(fila) if fila else None

    async def ids_evidencias(self, siniestro_id: UUID) -> frozenset[UUID]:
        filas = await (
            await self._conexion.execute(
                "SELECT id FROM evidencias WHERE siniestro_id = %s", (siniestro_id,)
            )
        ).fetchall()
        return frozenset(f["id"] for f in filas)

    async def reservar_plazos_vencidos(self, ahora: datetime, lote: int) -> list[Siniestro]:
        filas = await (
            await self._conexion.execute(
                f"SELECT {COLUMNAS} FROM siniestros "
                "WHERE estado = 'FALTA_SOPORTE' AND plazo_soporte_vence_en < %s "
                "ORDER BY plazo_soporte_vence_en, id FOR UPDATE SKIP LOCKED LIMIT %s",
                (ahora, lote),
            )
        ).fetchall()
        return [siniestro_de(f) for f in filas]

    async def guardar_transicion(self, siniestro: Siniestro, version_anterior: int) -> None:
        cursor = await self._conexion.execute(
            "UPDATE siniestros SET estado = %s, pausado_desde = %s, pausa_acumulada_s = %s, "
            "plazo_soporte_vence_en = %s, decidido_en = %s, deducible = %s, "
            "valor_indemnizar = %s, version = %s WHERE id = %s AND version = %s",
            (
                siniestro.estado.value,
                siniestro.pausado_desde,
                siniestro.pausa_acumulada_s,
                siniestro.plazo_soporte_vence_en,
                siniestro.decidido_en,
                siniestro.deducible,
                siniestro.valor_indemnizar,
                siniestro.version,
                siniestro.id,
                version_anterior,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("guardar_transicion: el siniestro cambió de versión")

    async def registrar_decision(self, decision: DecisionRegistrada) -> None:
        try:
            await self._conexion.execute(
                "INSERT INTO decisiones_siniestro (id, siniestro_id, tipo, idempotency_key, "
                "huella_solicitud, detalle, actor_id, registrada_en) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    decision.id,
                    decision.siniestro_id,
                    decision.tipo,
                    decision.idempotency_key,
                    decision.huella,
                    Jsonb(decision.detalle),
                    decision.actor_id,
                    decision.registrada_en,
                ),
            )
        except psycopg.errors.UniqueViolation as error:
            if error.diag.constraint_name == "decisiones_una_final":
                raise DecisionFinalDuplicada from error
            raise

    async def evento_en_evaluacion(
        self, siniestro: Siniestro, ocurrido_en: datetime, motivo: str = "ASIGNACION"
    ) -> None:
        await outbox_pg.insertar(
            self._bruta, outbox_pg.en_evaluacion(siniestro, ocurrido_en, motivo)
        )

    async def evento_aprobado(
        self, siniestro: Siniestro, referencia_pago: str, decidido_por: UUID, aprobado_en: datetime
    ) -> None:
        await outbox_pg.insertar(
            self._bruta, outbox_pg.aprobado(siniestro, referencia_pago, decidido_por, aprobado_en)
        )

    async def evento_rechazado(
        self, siniestro: Siniestro, causal: CausalRechazo, decidido_por: UUID, decidido_en: datetime
    ) -> None:
        await outbox_pg.insertar(
            self._bruta, outbox_pg.rechazado(siniestro, causal, decidido_por, decidido_en)
        )

    async def evento_soporte_solicitado(
        self,
        siniestro: Siniestro,
        documentos: tuple[SoporteSolicitable, ...],
        canal: CanalSoporte,
        decidido_por: UUID,
        ocurrido_en: datetime,
    ) -> None:
        await outbox_pg.insertar(
            self._bruta,
            outbox_pg.soporte_solicitado(siniestro, documentos, canal, decidido_por, ocurrido_en),
        )

    async def evento_consultado(
        self, siniestro: Siniestro, actor_id: str, ocurrido_en: datetime
    ) -> None:
        await outbox_pg.insertar(
            self._bruta, outbox_pg.consultado(siniestro, actor_id, ocurrido_en)
        )

    async def evento_evidencia_consultada(
        self, evidencia: Evidencia, actor_id: str, expira_en: datetime, ocurrido_en: datetime
    ) -> None:
        await outbox_pg.insertar(
            self._bruta,
            outbox_pg.evidencia_consultada(evidencia, actor_id, expira_en, ocurrido_en),
        )

    async def evento_acceso_denegado(self, acceso: AccesoDenegado, ocurrido_en: datetime) -> None:
        await outbox_pg.insertar(self._bruta, outbox_pg.acceso_denegado(acceso, ocurrido_en))
