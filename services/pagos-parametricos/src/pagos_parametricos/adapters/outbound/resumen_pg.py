"""Lectura del seguimiento de H09 en PostgreSQL (HU-W22, W22-B09). Solo lectura, por los índices
`eventos_evaluados_por_origen` y `activaciones_con_siniestro` de la migración 003. Cada método toma
una conexión del pool solo mientras dura su consulta."""

from datetime import datetime
from typing import Any

from pagos_parametricos.application.parametrico.resumen import (
    MAX_ULTIMOS,
    DatosDeOrigen,
    OrigenReciente,
    PagoActivado,
)
from pagos_parametricos.domain.parametrico.activacion import (
    ESTADO_SINIESTRO_CREADO,
    Disparador,
)

_MAS_RECIENTE = "SELECT evento_origen FROM eventos_evaluados ORDER BY evaluado_en DESC LIMIT 1"
_CONTEOS = (
    "SELECT count(*), COALESCE(sum(duplicados), 0), min(evaluado_en) "
    "FROM eventos_evaluados WHERE evento_origen = %s"
)
_ACTIVADOS = (
    "SELECT count(*) FROM activaciones a JOIN eventos_evaluados e USING (evento_id) "
    "WHERE e.evento_origen = %s"
)
_RITMO = (
    "SELECT date_trunc('minute', evaluado_en) AS minuto, count(*) FROM eventos_evaluados "
    "WHERE evento_origen = %s AND evaluado_en >= %s GROUP BY 1"
)
_ULTIMOS = (
    "SELECT a.siniestro_id, a.siniestro_codigo, a.poliza_id, c.numero, a.disparador, a.valor_pago, "
    "a.moneda, a.estado FROM activaciones a JOIN eventos_evaluados e USING (evento_id) "
    "LEFT JOIN coberturas_activas c USING (poliza_id) "
    f"WHERE e.evento_origen = %s AND a.estado = '{ESTADO_SINIESTRO_CREADO}' "
    f"ORDER BY a.creada_en DESC, a.poliza_id LIMIT {MAX_ULTIMOS}"
)
_ORIGENES = (
    "SELECT evento_origen, min(evaluado_en), max(evaluado_en), count(*) FROM eventos_evaluados "
    "GROUP BY evento_origen ORDER BY max(evaluado_en) DESC, evento_origen LIMIT %s"
)


def pago_de(fila: tuple[Any, ...]) -> PagoActivado:
    siniestro_id, codigo, poliza_id, numero, disparador, valor, moneda, estado = fila
    return PagoActivado(
        siniestro_id=siniestro_id,
        siniestro_codigo=codigo,
        poliza_id=poliza_id,
        poliza_numero=numero,
        disparador=Disparador(
            tipo_evento=disparador["tipoEvento"],
            valor_observado=disparador["valorObservado"],
            umbral=disparador["umbral"],
            unidad=disparador["unidad"],
        ),
        valor=format(valor, "f"),
        moneda=moneda.strip(),
        estado_activacion=estado,
    )


class ResumenPg:
    """Implementa `LecturaResumen` sobre el pool del servicio."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def origen_mas_reciente(self) -> str | None:
        async with self._pool.connection() as conexion:
            fila = await (await conexion.execute(_MAS_RECIENTE)).fetchone()
        return str(fila[0]) if fila else None

    async def datos(self, evento_origen: str, desde_ritmo: datetime) -> DatosDeOrigen:
        async with self._pool.connection() as conexion:
            conteos = await (await conexion.execute(_CONTEOS, (evento_origen,))).fetchone()
            activados = await (await conexion.execute(_ACTIVADOS, (evento_origen,))).fetchone()
            ritmo = await (await conexion.execute(_RITMO, (evento_origen, desde_ritmo))).fetchall()
            ultimos = await (await conexion.execute(_ULTIMOS, (evento_origen,))).fetchall()
        assert conteos is not None and activados is not None
        return DatosDeOrigen(
            desde=conteos[2],
            procesados=int(conteos[0]),
            duplicados=int(conteos[1]),
            activados=int(activados[0]),
            por_minuto={fila[0]: int(fila[1]) for fila in ritmo},
            ultimos_pagos=tuple(pago_de(fila) for fila in ultimos),
        )

    async def origenes(self, limite: int) -> list[OrigenReciente]:
        async with self._pool.connection() as conexion:
            filas = await (await conexion.execute(_ORIGENES, (limite,))).fetchall()
        return [OrigenReciente(f[0], f[1], f[2], int(f[3])) for f in filas]
