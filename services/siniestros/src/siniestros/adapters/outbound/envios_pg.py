"""Conversion atomica del borrador, usando la idempotencia y el outbox del servicio."""

import hashlib
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from solventa_common import correlation

from siniestros.adapters.outbound import outbox_pg
from siniestros.adapters.outbound.codigos import siguiente_en_transaccion
from siniestros.adapters.outbound.mapeo import copia_a_json, ubicacion_de_json
from siniestros.adapters.outbound.siniestros_pg import TxSiniestrosPg, _ConDict
from siniestros.application.siniestro.envio import (
    BOGOTA,
    BorradorEnvio,
    EvidenciaEnvio,
    PolizaReporte,
    clave_permanente,
    id_siniestro_movil,
    validar_reporte,
)
from siniestros.application.siniestro.errores import (
    BorradorModificado,
    BorradorNoEncontrado,
    IdempotenciaConflicto,
)
from siniestros.application.siniestro.ubicacion import reevaluar_ubicacion
from siniestros.config import Settings
from siniestros.domain.siniestro.modelos import (
    EntradaHistorial,
    EstadoSiniestro,
    OrigenSiniestro,
    Siniestro,
    TipoEvidencia,
    Ubicacion,
)
from siniestros.domain.siniestro.tramite import ACCION_EVIDENCIAS_VALIDADAS, ACCION_REPORTADO


def _huella(actor_id: str, borrador_id: UUID) -> str:
    return hashlib.sha256(f"{actor_id}:{borrador_id}".encode()).hexdigest()


async def _leer(conexion: _ConDict, actor_id: str, borrador_id: UUID) -> BorradorEnvio:
    fila = await (
        await conexion.execute(
            "SELECT * FROM borradores WHERE id = %s AND actor_id = %s FOR UPDATE",
            (borrador_id, actor_id),
        )
    ).fetchone()
    if fila is None:
        raise BorradorNoEncontrado
    evidencias = await (
        await conexion.execute(
            "SELECT * FROM borrador_evidencias WHERE borrador_id = %s ORDER BY id",
            (borrador_id,),
        )
    ).fetchall()
    return BorradorEnvio(
        id=fila["id"],
        actor_id=fila["actor_id"],
        cliente_id=fila["cliente_id"],
        poliza_id=fila["poliza_id"],
        descripcion=fila["descripcion"],
        causa=fila["causa"],
        fecha_evento=fila["fecha_evento"],
        version=fila["version"],
        resultado=fila["envio_resultado"],
        ubicacion=fila["ubicacion"],
        evidencias=tuple(
            EvidenciaEnvio(
                id=e["id"],
                nombre_archivo=e["nombre_archivo"],
                tipo=TipoEvidencia(e["tipo"]),
                clave_objeto=e["clave_objeto"],
                sha256=e["sha256"],
                capturada_en=e["capturada_en"],
                tamano_bytes=e["tamano_bytes"],
                tipo_contenido=e["tipo_contenido"],
                duracion_segundos=e["duracion_segundos"],
                estado_carga=e["estado_carga"],
            )
            for e in evidencias
        ),
    )


class EnviosPg:
    def __init__(self, pool: AsyncConnectionPool[Any], settings: Settings) -> None:
        self._pool = pool
        self._settings = settings

    async def preparar(self, actor_id: str, borrador_id: UUID, clave: UUID) -> BorradorEnvio:
        async with self._pool.connection() as conexion, conexion.transaction():
            previo = await TxSiniestrosPg(conexion).adquirir_idempotencia(f"envio:{clave}")
            borrador = await _leer(_ConDict(conexion), actor_id, borrador_id)
            if previo is not None and previo.huella != _huella(actor_id, borrador_id):
                raise IdempotenciaConflicto
            return borrador

    async def convertir(
        self, borrador: BorradorEnvio, clave: UUID, poliza: PolizaReporte | None, ahora: datetime
    ) -> dict[str, Any]:
        clave_envio = f"envio:{clave}"
        huella = _huella(borrador.actor_id, borrador.id)
        async with self._pool.connection() as conexion, conexion.transaction():
            tx, sql = TxSiniestrosPg(conexion), _ConDict(conexion)
            previo = await tx.adquirir_idempotencia(clave_envio)
            actual = await _leer(sql, borrador.actor_id, borrador.id)
            if previo is not None:
                if previo.huella != huella:
                    raise IdempotenciaConflicto
                return previo.respuesta
            if actual.resultado is not None:
                await tx.guardar_idempotencia(clave_envio, huella, actual.resultado, ahora)
                return actual.resultado
            if actual.version != borrador.version:
                raise BorradorModificado
            validar_reporte(actual, poliza, ahora, self._settings)
            ubicacion = reevaluar_ubicacion(
                actual.ubicacion, self._settings.ubicacion_precision_max_m
            )
            ubicacion_json = ubicacion.model_dump(by_alias=True, mode="json") if ubicacion else None
            assert (
                poliza is not None and actual.causa is not None and actual.fecha_evento is not None
            )
            codigo = await siguiente_en_transaccion(conexion, ahora.astimezone(BOGOTA).year)
            caso = Siniestro(
                id=id_siniestro_movil(actual.id),
                codigo=codigo,
                origen=OrigenSiniestro.MOVIL,
                estado=EstadoSiniestro.REPORTADO,
                poliza_id=actual.poliza_id,
                cliente_id=actual.cliente_id,
                copia_poliza=poliza.copia,
                causa=actual.causa,
                fecha_evento=actual.fecha_evento,
                ubicacion_declarada=ubicacion_de_json(ubicacion_json) or Ubicacion(),
                reportado_en=ahora,
                correlation_id=correlation.obtener() or str(uuid4()),
            )
            await sql.execute(
                "INSERT INTO siniestros "
                "(id, codigo, origen, estado, poliza_id, cliente_id, copia_poliza, causa, "
                "fecha_evento, ubicacion_declarada, reportado_en, correlation_id, descripcion) "
                "VALUES (%s, %s, 'MOVIL', 'REPORTADO', %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    caso.id,
                    caso.codigo,
                    caso.poliza_id,
                    caso.cliente_id,
                    Jsonb(copia_a_json(caso.copia_poliza)),
                    caso.causa,
                    caso.fecha_evento,
                    Jsonb(ubicacion_json or {}),
                    caso.reportado_en,
                    caso.correlation_id,
                    actual.descripcion,
                ),
            )
            for evidencia in actual.evidencias:
                permanente = clave_permanente(actual.id, evidencia.id)
                await sql.execute(
                    "INSERT INTO evidencias "
                    "(id, siniestro_id, nombre_archivo, tipo, clave_objeto, sha256, capturada_en, "
                    "tamano_bytes, tipo_contenido, duracion_segundos) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        evidencia.id,
                        caso.id,
                        evidencia.nombre_archivo,
                        evidencia.tipo.value,
                        permanente,
                        evidencia.sha256,
                        evidencia.capturada_en,
                        evidencia.tamano_bytes,
                        evidencia.tipo_contenido,
                        evidencia.duracion_segundos,
                    ),
                )
                await sql.execute(
                    "UPDATE borrador_evidencias SET clave_objeto = %s WHERE id = %s",
                    (permanente, evidencia.id),
                )
            for accion in (ACCION_REPORTADO, ACCION_EVIDENCIAS_VALIDADAS):
                await tx.agregar_historial(
                    EntradaHistorial(
                        id=uuid4(),
                        siniestro_id=caso.id,
                        accion=accion,
                        ocurrido_en=ahora,
                        estado_nuevo=EstadoSiniestro.REPORTADO
                        if accion == ACCION_REPORTADO
                        else None,
                        actor_id=actual.actor_id,
                        actor_rol="CLIENTE",
                        evidencias_consideradas=tuple(e.id for e in actual.evidencias),
                    )
                )
            await outbox_pg.insertar(conexion, outbox_pg.reportado(caso))
            resultado = {
                "borradorId": str(actual.id),
                "siniestroId": str(caso.id),
                "codigo": codigo,
                "estado": "REPORTADO",
                "reportadoEn": ahora.isoformat(),
            }
            await sql.execute(
                "UPDATE borradores SET estado = 'CONVERTIDO', siniestro_id = %s, "
                "envio_resultado = %s, ubicacion = %s, version = version + 1 WHERE id = %s",
                (
                    caso.id,
                    Jsonb(resultado),
                    Jsonb(ubicacion_json) if ubicacion_json is not None else None,
                    actual.id,
                ),
            )
            await tx.guardar_idempotencia(clave_envio, huella, resultado, ahora)
        return resultado
