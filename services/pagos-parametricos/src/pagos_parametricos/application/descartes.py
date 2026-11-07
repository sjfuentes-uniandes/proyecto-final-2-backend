"""Ver y resolver la cola de descarte de `parametricos-eventos` (HU-W24, AC3; H10 y H11).

La DLQ es la fuente: los mensajes **se quedan en ella** (14 días) hasta que un analista los
resuelve. SQS no tiene "mirar sin recibir": leer es recibir con una visibilidad corta y, al
terminar, liberar (visibilidad 0) lo que no se va a resolver. Ninguna lectura borra.

- **Listar:** hasta `max_mensajes`, con la causa que anotó el consumidor por `MessageId`.
- **Resolver** (reprocesar, corregir y reprocesar, descartar), por mensaje:
  1. lo toma de la DLQ (si otro analista lo tiene tomado o ya no está: 404 o la resolución previa);
  2. registra la resolución y su `DescarteParametricoResuelto` en el Outbox, en una transacción
     (la auditoría queda antes que el efecto);
  3. reprocesar: lo **reenvía a la cola origen con el mismo `eventId`** (la idempotencia del
     consumidor evita un segundo pago); descartar: nada;
  4. lo borra de la DLQ.
  Si 3 o 4 fallan, se libera: sigue en la DLQ y la resolución ya registrada permite completarla
  (repetir la misma acción es idempotente; otra acción distinta es 409 `DESCARTE_YA_RESUELTO`).
  Si 4 falla tras reenviar, el mensaje queda en las dos colas: el consumidor lo deduplica por
  `eventId` y la siguiente resolución solo lo borra.
- **Verificación antes de reprocesar** (H11: póliza vigente, cobertura activa, pago previo): la
  calcula HU-W21 con su copia de coberturas y sus evaluaciones (`Verificaciones`); sin ella, el
  detalle la devuelve vacía.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from solventa_common.errors import ErrorNegocio
from solventa_common.telemetry import RESULTADO_RECHAZADO, operacion

from pagos_parametricos.domain.descarte import (
    Accion,
    Causa,
    CorreccionInvalida,
    FalloAnotado,
    MensajeDescartado,
    aplicar_correccion,
    interpretar,
    resumen_por_causa,
)

DESCARTE_NO_ENCONTRADO = "DESCARTE_NO_ENCONTRADO"
DESCARTE_YA_RESUELTO = "DESCARTE_YA_RESUELTO"
CORRECCION_INVALIDA = "CORRECCION_INVALIDA"
COLA_NO_DISPONIBLE = "COLA_NO_DISPONIBLE"
MAX_MENSAJES = 100
VISIBILIDAD_LECTURA_S = 30
MAX_INTENTOS = 5


@dataclass(frozen=True)
class MensajeDlq:
    message_id: str
    receipt_handle: str
    cuerpo: str
    tipo_atributo: str | None
    recepciones: int | None
    enviado_en: str | None


@dataclass(frozen=True)
class Resolucion:
    message_id: str
    evento_id: str | None
    accion: Accion
    causa: Causa
    actor_id: str
    motivo: str | None
    original: str
    corregido: str | None
    resuelto_en: datetime


@dataclass(frozen=True)
class Listado:
    mensajes: list[MensajeDescartado]
    por_causa: dict[Causa, int]
    retencion_dias: int
    max_intentos: int


@dataclass(frozen=True)
class Detalle:
    mensaje: MensajeDescartado
    verificacion: Mapping[str, Any] | None
    resolucion: Resolucion | None


# --- Puertos -------------------------------------------------------------------------------------
class Dlq(Protocol):
    async def recibir(self, maximo: int, visibilidad_s: int) -> list[MensajeDlq]:
        """Un lote (hasta 10) sin espera larga, oculto `visibilidad_s`."""
        ...

    async def liberar(self, mensaje: MensajeDlq) -> None: ...

    async def borrar(self, mensaje: MensajeDlq) -> None: ...

    async def reenviar(self, cuerpo: str, tipo: str | None) -> None:
        """A la cola origen (`parametricos-eventos`), con el atributo `eventType`."""
        ...

    async def retencion_dias(self) -> int: ...


class Fallos(Protocol):
    async def anotados(self, message_ids: Sequence[str]) -> dict[str, FalloAnotado]: ...


class Resoluciones(Protocol):
    async def registrar(self, resolucion: Resolucion, correlation_id: str) -> Resolucion:
        """Guarda la resolución y su evento en una transacción; si el mensaje ya tenía una,
        devuelve esa sin escribir nada."""
        ...

    async def de(self, message_id: str) -> Resolucion | None: ...


class Verificaciones(Protocol):
    """HU-W21: póliza vigente, cobertura activa, pago previo por este evento, umbral y valor."""

    async def verificar(self, mensaje: MensajeDescartado) -> Mapping[str, Any] | None: ...


class Reloj(Protocol):
    def ahora(self) -> datetime: ...


class SinVerificaciones:
    """Mientras HU-W21 no entregue la suya, el detalle sale sin verificación."""

    async def verificar(self, mensaje: MensajeDescartado) -> Mapping[str, Any] | None:
        return None


def no_encontrado(message_id: str) -> ErrorNegocio:
    return ErrorNegocio(
        DESCARTE_NO_ENCONTRADO,
        "El mensaje no está en la cola de descarte o lo está revisando otro analista",
        404,
        {"messageId": message_id},
    )


def ya_resuelto(message_id: str, accion: Accion) -> ErrorNegocio:
    return ErrorNegocio(
        DESCARTE_YA_RESUELTO,
        "El mensaje ya tiene otra resolución",
        409,
        {"messageId": message_id, "accion": accion.value},
    )


class ColaDescarte:
    def __init__(
        self,
        dlq: Dlq,
        fallos: Fallos,
        resoluciones: Resoluciones,
        reloj: Reloj,
        verificaciones: Verificaciones | None = None,
        *,
        max_mensajes: int = MAX_MENSAJES,
        visibilidad_s: int = VISIBILIDAD_LECTURA_S,
        max_intentos: int = MAX_INTENTOS,
    ) -> None:
        self._dlq = dlq
        self._fallos = fallos
        self._resoluciones = resoluciones
        self._reloj = reloj
        self._verificaciones = verificaciones or SinVerificaciones()
        self._max_mensajes = max_mensajes
        self._visibilidad_s = visibilidad_s
        self.max_intentos = max_intentos

    # --- Lectura ---------------------------------------------------------------------------------
    async def listar(self) -> Listado:
        with operacion("descarte.listar"):
            vistos, _ = await self._tomar()
            mensajes = await self._interpretar(list(vistos.values()))
            retencion = await self._dlq.retencion_dias()
        mensajes.sort(key=lambda m: (m.recibido_en or "", m.message_id))
        return Listado(mensajes, resumen_por_causa(mensajes), retencion, self.max_intentos)

    async def detalle(self, message_id: str) -> Detalle:
        with operacion("descarte.detalle") as op:
            vistos, _ = await self._tomar()
            if message_id not in vistos:
                op.marcar(RESULTADO_RECHAZADO)
                raise no_encontrado(message_id)
            (mensaje,) = await self._interpretar([vistos[message_id]])
            verificacion = await self._verificaciones.verificar(mensaje)
            previa = await self._resoluciones.de(message_id)
        return Detalle(mensaje, verificacion, previa)

    # --- Resolución ------------------------------------------------------------------------------
    async def reprocesar(
        self, message_ids: Sequence[str], actor: str, correlation_id: str
    ) -> dict[str, Resolucion | None]:
        """Varios a la vez (H10, "Reprocesar seleccionados"): None para los que no se encontraron."""
        with operacion("descarte.reprocesar"):
            _, tomados = await self._tomar(set(message_ids))
            resultados: dict[str, Resolucion | None] = {}
            for message_id in message_ids:
                mensaje = tomados.get(message_id)
                if mensaje is None:
                    resultados[message_id] = await self._resoluciones.de(message_id)
                    continue
                resultados[message_id] = await self._resolver(
                    mensaje, Accion.REPROCESADO, actor, correlation_id
                )
        return resultados

    async def corregir(
        self,
        message_id: str,
        cambios: Mapping[str, object],
        actor: str,
        correlation_id: str,
        motivo: str | None = None,
    ) -> Resolucion:
        with operacion("descarte.corregir") as op:
            mensaje = await self._tomar_uno(message_id)
            if mensaje is None:
                return await self._previa(message_id, Accion.CORREGIDO, op)
            try:
                corregido = aplicar_correccion(mensaje.cuerpo, cambios)
            except CorreccionInvalida as error:
                await self._dlq.liberar(mensaje)
                op.marcar(RESULTADO_RECHAZADO)
                raise ErrorNegocio(
                    CORRECCION_INVALIDA, error.motivo, 422, {"campos": error.campos}
                ) from error
            return await self._resolver(
                mensaje, Accion.CORREGIDO, actor, correlation_id, motivo, corregido
            )

    async def descartar(
        self, message_id: str, actor: str, correlation_id: str, motivo: str | None = None
    ) -> Resolucion:
        with operacion("descarte.descartar") as op:
            mensaje = await self._tomar_uno(message_id)
            if mensaje is None:
                return await self._previa(message_id, Accion.DESCARTADO, op)
            return await self._resolver(mensaje, Accion.DESCARTADO, actor, correlation_id, motivo)

    async def _tomar_uno(self, message_id: str) -> MensajeDlq | None:
        _, tomados = await self._tomar({message_id})
        return tomados.get(message_id)

    async def _previa(self, message_id: str, accion: Accion, op: operacion) -> Resolucion:
        """Sin el mensaje en la DLQ: la resolución previa de la misma acción ya lo completó (la
        repetición es idempotente); si no hay, o es otra, se rechaza."""
        previa = await self._resoluciones.de(message_id)
        if previa is not None and previa.accion is accion:
            return previa
        op.marcar(RESULTADO_RECHAZADO)
        if previa is not None:
            raise ya_resuelto(message_id, previa.accion)
        raise no_encontrado(message_id)

    async def _resolver(
        self,
        mensaje: MensajeDlq,
        accion: Accion,
        actor: str,
        correlation_id: str,
        motivo: str | None = None,
        corregido: str | None = None,
    ) -> Resolucion:
        try:
            (interpretado,) = await self._interpretar([mensaje])
            nueva = Resolucion(
                message_id=mensaje.message_id,
                evento_id=interpretado.evento_id,
                accion=accion,
                causa=interpretado.causa,
                actor_id=actor,
                motivo=motivo,
                original=mensaje.cuerpo,
                corregido=corregido,
                resuelto_en=self._reloj.ahora(),
            )
            resolucion = await self._resoluciones.registrar(nueva, correlation_id)
            if resolucion.accion != accion or resolucion.corregido != corregido:
                raise ya_resuelto(mensaje.message_id, resolucion.accion)
            if accion is not Accion.DESCARTADO:
                await self._dlq.reenviar(
                    resolucion.corregido or mensaje.cuerpo, mensaje.tipo_atributo
                )
        except BaseException:
            await self._dlq.liberar(mensaje)
            raise
        await self._dlq.borrar(mensaje)
        return resolucion

    # --- Apoyo -----------------------------------------------------------------------------------
    async def _tomar(
        self, buscados: set[str] | None = None
    ) -> tuple[dict[str, MensajeDlq], dict[str, MensajeDlq]]:
        """Recibe hasta `max_mensajes` (o hasta encontrar todos los `buscados`) y devuelve
        `(vistos, retenidos)`: retiene los buscados y libera el resto. Sin `buscados`, lo libera
        todo. Si algo falla, libera todo lo tomado."""
        vistos: dict[str, MensajeDlq] = {}
        retenidos: dict[str, MensajeDlq] = {}
        try:
            while len(vistos) < self._max_mensajes:
                if buscados is not None and buscados <= retenidos.keys():
                    break
                lote = await self._dlq.recibir(
                    min(10, self._max_mensajes - len(vistos)), self._visibilidad_s
                )
                nuevos = [m for m in lote if m.message_id not in vistos]
                if not nuevos:
                    break
                for mensaje in nuevos:
                    vistos[mensaje.message_id] = mensaje
                    if buscados is not None and mensaje.message_id in buscados:
                        retenidos[mensaje.message_id] = mensaje
        except BaseException:
            for mensaje in vistos.values():
                await self._dlq.liberar(mensaje)
            raise
        for message_id, mensaje in vistos.items():
            if message_id not in retenidos:
                await self._dlq.liberar(mensaje)
        return vistos, retenidos

    async def _interpretar(self, recibidos: list[MensajeDlq]) -> list[MensajeDescartado]:
        fallos = await self._fallos.anotados([m.message_id for m in recibidos])
        return [
            interpretar(
                m.message_id,
                m.cuerpo,
                recibido_en=m.enviado_en,
                recepciones=m.recepciones,
                fallo=fallos.get(m.message_id),
            )
            for m in recibidos
        ]
