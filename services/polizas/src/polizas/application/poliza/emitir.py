"""La tarea emisora (HU-W17, 17.8; plan §d; ADR 0014, decisiones 1, 5 y 6). Es también la
reconciliación: nada más procesa una póliza EMITIENDO.

- Cada ciclo lee, sin bloquear filas, hasta `lote` pólizas EMITIENDO sin una reserva vigente: las
  sin fallo de inmediato, y las fallidas cuando su fallo tiene más de `reintento_s`.
- Reserva (17.8, ajuste): antes de procesar una, la reserva por `reserva_s` en una transacción
  corta; si otra tarea la tiene, sigue con la siguiente (sin medirla). Al terminar, con éxito o con
  fallo, la libera (si no puede, vence sola). Así una sola tarea sube el PDF de una clave: dos
  subidas dejarían referenciada una versión que deja de ser la actual, y el bucket expira las
  versiones no actuales (infra/platform/storage.tf:132-133).
- Por póliza, sin bloqueos durante las llamadas, y cada escritura en su transacción corta con la
  guarda de `PolizasRepositorio.actualizar`:
  1. sin documento: los datos con `fechaDocumento` = `creada_en` (17.7) → el PDF → la subida →
     `registrar_documento`;
  2. sin firma: la firma de la huella **registrada** → `registrar_firma`;
  3. `activar` (`emitidaEn` = `fechaDocumento`) y `PolizaEmitida` v1 en el outbox, en la misma
     transacción.
  Si la guarda no cambia nada, otra tarea avanzó: se relee y se sigue desde lo registrado. Si ya
  está ACTIVA, el evento lo escribió esa otra tarea.
- Cualquier fallo (datos, PDF, S3 o firma) deja EMITIENDO con su código (`registrar_fallo`, con la
  guarda) y se sigue con la siguiente póliza. Un fallo inesperado (p. ej., la base) también, con
  `EMISION_ERROR_INESPERADO` si se puede escribir. Nunca lanza; la parada se atiende entre pólizas.
- Por póliza, la operación `emision` (contracts/operaciones.md), desde que se toma hasta el commit:
  `ok` (ACTIVA), `ya_emitida` (otra tarea la activó), `pendiente` (sigue EMITIENDO; no suma en
  `OperationErrors`) y `error` (fallo inesperado, o pendiente con más de `alerta_tras_s` desde
  `creada_en`). La pendiente vencida se sigue reintentando: no hay tope ni compensación.
- Los logs llevan `poliza_id`, `clave_recaudo`, el resultado y el código, con la correlación del
  recaudo; nunca el cliente, la huella ni montos.
"""

import asyncio
import hashlib
import logging
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

from solventa_common import correlation
from solventa_common.telemetry import operacion

from polizas.application.poliza.documento import ReunirDatosDocumento
from polizas.application.poliza.puertos import (
    AlmacenDocumentos,
    EmisionesRepositorio,
    EventosPoliza,
    FirmaPuerto,
    GeneradorDocumento,
    SolicitudFirma,
    Transacciones,
)
from polizas.application.suscripcion.puertos import Reloj
from polizas.domain.poliza.documento import clave_documento
from polizas.domain.poliza.errores import (
    DatosDocumentoNoDisponibles,
    FalloAlmacenDocumentos,
    FalloFirma,
    HuellaNoCoincide,
    PolizaInvalida,
)
from polizas.domain.poliza.modelos import Documento, EstadoPoliza, Firma, Poliza

OPERACION = "emision"
# Códigos propios de la emisión; los demás son los de cada fallo (DATOS_DOCUMENTO_NO_DISPONIBLES,
# ALMACEN_*, FIRMA_* y HUELLA_NO_COINCIDE).
CODIGO_DATOS_INVALIDOS = "DATOS_DOCUMENTO_INVALIDOS"
CODIGO_DOCUMENTO_NO_GENERADO = "DOCUMENTO_NO_GENERADO"
CODIGO_INESPERADO = "EMISION_ERROR_INESPERADO"
MENSAJE_EMITIDA = "emision: póliza procesada"
MENSAJE_FALLIDA = "emision: la emisión falló; sigue EMITIENDO y se reintenta"
MENSAJE_VENCIDA = "emision: póliza EMITIENDO vencida; se sigue reintentando"
MENSAJE_INESPERADO = "emision: fallo inesperado; sigue EMITIENDO y se reintenta"
MENSAJE_CICLO_FALLIDO = "emision: el ciclo falló; se reintenta en el siguiente"
MENSAJE_SIN_LIBERAR = "emision: no se pudo liberar la reserva; vence sola"

logger = logging.getLogger("polizas.emision")


class Resultado(StrEnum):
    """El resultado de la operación `emision` por póliza."""

    OK = "ok"
    # La guarda no cambió nada y la póliza ya estaba ACTIVA: otra tarea la emitió (sin evento).
    YA_EMITIDA = "ya_emitida"
    PENDIENTE = "pendiente"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class Emision:
    poliza_id: UUID
    clave_recaudo: UUID
    resultado: Resultado
    # El código del fallo; nulo si quedó ACTIVA.
    codigo: str | None
    # Pendiente con más de `alerta_tras_s` desde `creada_en`: se midió como `error`.
    vencida: bool = False


class _Fallo(Exception):
    """Un fallo conocido de la emisión, ya con su código."""

    def __init__(self, codigo: str, error_s3: str | None = None) -> None:
        super().__init__(codigo)
        self.codigo = codigo
        self.error_s3 = error_s3


def _huella(contenido: bytes) -> str:
    return f"sha256:{hashlib.sha256(contenido).hexdigest()}"


def _ids(poliza: Poliza) -> dict[str, str]:
    return {"poliza_id": str(poliza.poliza_id), "clave_recaudo": str(poliza.aceptacion_id)}


class EmitirPolizas[T]:
    def __init__(
        self,
        *,
        polizas: EmisionesRepositorio[T],
        transacciones: Transacciones[T],
        datos: ReunirDatosDocumento[T],
        generador: GeneradorDocumento,
        almacen: AlmacenDocumentos,
        firma: FirmaPuerto,
        eventos: EventosPoliza[T],
        reloj: Reloj,
        intervalo_s: float,
        reintento_s: float,
        reserva_s: float,
        lote: int,
        alerta_tras_s: float,
    ) -> None:
        self._polizas = polizas
        self._transacciones = transacciones
        self._datos = datos
        self._generador = generador
        self._almacen = almacen
        self._firma = firma
        self._eventos = eventos
        self._reloj = reloj
        self._intervalo_s = intervalo_s
        self._reintento = timedelta(seconds=reintento_s)
        self._reserva = timedelta(seconds=reserva_s)
        self._lote = lote
        self._alerta_tras = timedelta(seconds=alerta_tras_s)

    async def ejecutar(self, parada: asyncio.Event) -> None:
        """Espera el intervalo y emite, hasta la parada."""
        while not await _parada_dentro_de(parada, self._intervalo_s):
            try:
                await self.emitir_una_vez(parada)
            except Exception as error:  # noqa: BLE001 - el mensaje puede traer datos
                logger.error(MENSAJE_CICLO_FALLIDO, extra={"error_type": type(error).__name__})

    async def emitir_una_vez(self, parada: asyncio.Event | None = None) -> list[Emision]:
        """Un ciclo. Lo invocan el bucle y las pruebas."""
        ahora = self._reloj.ahora()
        async with self._transacciones.transaccion() as tx:
            pendientes = await self._polizas.pendientes(
                ahora, ahora - self._reintento, self._lote, tx
            )
        emisiones: list[Emision] = []
        for poliza in pendientes:
            if parada is not None and parada.is_set():
                break
            emision = await self._emitir_reservada(poliza.poliza_id)
            if emision is not None:
                emisiones.append(emision)
        return emisiones

    async def _emitir_reservada(self, poliza_id: UUID) -> Emision | None:
        """Reserva, emite y libera. None si otra tarea la tiene reservada."""
        ahora = self._reloj.ahora()
        hasta = ahora + self._reserva
        async with self._transacciones.transaccion() as tx:
            # La póliza al día: la lista del ciclo pudo quedar vieja.
            poliza = await self._polizas.reservar(poliza_id, ahora, hasta, tx)
        if poliza is None:
            return None
        correlation_id = poliza.correlation_id or str(poliza.poliza_id)
        token = correlation.establecer(correlation_id)
        try:
            return await self._emitir(poliza, correlation_id)
        finally:
            await self._liberar(poliza, hasta)
            correlation.restablecer(token)

    async def _liberar(self, poliza: Poliza, hasta: datetime) -> None:
        """Solo la propia; si ya está ACTIVA, `actualizar` la liberó al activarla."""
        try:
            async with self._transacciones.transaccion() as tx:
                await self._polizas.liberar(poliza.poliza_id, hasta, tx)
        except Exception as error:  # noqa: BLE001 - el mensaje puede traer datos
            logger.warning(
                MENSAJE_SIN_LIBERAR, extra={**_ids(poliza), "error_type": type(error).__name__}
            )

    async def _emitir(self, poliza: Poliza, correlation_id: str) -> Emision:
        actual = poliza
        with operacion(OPERACION) as medicion:
            try:
                if actual.documento is None:
                    actual = await self._registrar_documento(actual)
                if actual.estado is EstadoPoliza.EMITIENDO and actual.firma is None:
                    actual = await self._registrar_firma(actual, correlation_id)
                emitida_aqui = False
                if actual.estado is EstadoPoliza.EMITIENDO:
                    actual, emitida_aqui = await self._activar(actual, correlation_id)
            except _Fallo as fallo:
                await self._registrar_fallo(actual, fallo.codigo)
                return self._pendiente(poliza, fallo.codigo, fallo.error_s3, medicion)
            except Exception as error:  # noqa: BLE001 - una póliza no detiene a las demás
                medicion.marcar(Resultado.ERROR.value)
                logger.error(
                    MENSAJE_INESPERADO,
                    extra={**_ids(poliza), "error_type": type(error).__name__},
                )
                await self._registrar_fallo(actual, CODIGO_INESPERADO)
                return Emision(
                    poliza.poliza_id, poliza.aceptacion_id, Resultado.ERROR, CODIGO_INESPERADO
                )
            if actual.estado is EstadoPoliza.ACTIVA:
                resultado = Resultado.OK if emitida_aqui else Resultado.YA_EMITIDA
                codigo = None
            else:
                # La guarda de ACTIVA no cambió nada y la póliza sigue EMITIENDO: otra tarea
                # registró algo entre tanto. El siguiente ciclo la retoma.
                resultado, codigo = Resultado.PENDIENTE, actual.ultimo_fallo
            medicion.marcar(resultado.value)
        logger.info(
            MENSAJE_EMITIDA, extra={**_ids(poliza), "resultado": resultado.value, "codigo": codigo}
        )
        return Emision(poliza.poliza_id, poliza.aceptacion_id, resultado, codigo)

    def _pendiente(
        self, poliza: Poliza, codigo: str, error_s3: str | None, medicion: operacion
    ) -> Emision:
        antiguedad = self._reloj.ahora() - poliza.creada_en
        # Solo `error` y `timeout` suman en OperationErrors (telemetry.registrar), que vigila la
        # alarma de tasa de error de polizas. Sigue EMITIENDO: sin tope.
        vencida = antiguedad > self._alerta_tras
        resultado = Resultado.ERROR if vencida else Resultado.PENDIENTE
        medicion.marcar(resultado.value)
        extra = {**_ids(poliza), "codigo": codigo}
        if error_s3 is not None:
            extra["error_s3"] = error_s3
        if vencida:
            logger.error(
                MENSAJE_VENCIDA, extra={**extra, "antiguedad_s": int(antiguedad.total_seconds())}
            )
        else:
            logger.warning(MENSAJE_FALLIDA, extra=extra)
        return Emision(poliza.poliza_id, poliza.aceptacion_id, resultado, codigo, vencida=vencida)

    async def _registrar_documento(self, poliza: Poliza) -> Poliza:
        # La fecha del documento es la de la creación (17.7): un reintento genera los mismos bytes.
        fecha = poliza.creada_en
        try:
            async with self._transacciones.transaccion() as tx:
                datos = await self._datos.ejecutar(poliza, fecha, tx)
        except DatosDocumentoNoDisponibles as error:
            raise _Fallo(error.codigo) from error
        except PolizaInvalida as error:
            raise _Fallo(CODIGO_DATOS_INVALIDOS) from error
        try:
            # CPU de milisegundos: no hace falta un hilo.
            pdf = self._generador.generar(datos)
        except Exception as error:
            raise _Fallo(CODIGO_DOCUMENTO_NO_GENERADO) from error
        clave = clave_documento(poliza)
        try:
            version_id = await self._almacen.guardar(clave, pdf)
        except FalloAlmacenDocumentos as error:
            raise _Fallo(error.codigo, error.error_s3) from error
        documento = Documento(clave, version_id, _huella(pdf), len(pdf), fecha)
        async with self._transacciones.transaccion() as tx:
            registrada = poliza.registrar_documento(documento, self._reloj.ahora())
            if await self._polizas.actualizar(registrada, tx):
                return registrada
            # Otra tarea registró antes su versión: se sigue con la registrada (una sola
            # referenciada; esta queda huérfana y vence con el ciclo de vida del bucket).
            return await self._releer(poliza, tx)

    async def _registrar_firma(self, poliza: Poliza, correlation_id: str) -> Poliza:
        documento = poliza.documento
        if documento is None:
            raise RuntimeError("emision: firma sin documento registrado")
        try:
            firmada = await self._firma.firmar(
                SolicitudFirma(
                    documento_id=poliza.poliza_id,
                    huella=documento.huella,
                    firmante_ref=poliza.cliente_id,
                    correlation_id=correlation_id,
                )
            )
        except FalloFirma as error:
            raise _Fallo(error.codigo) from error
        async with self._transacciones.transaccion() as tx:
            try:
                registrada = poliza.registrar_firma(
                    Firma(firmada.firma_id, firmada.sello_tiempo),
                    firmada.huella,
                    self._reloj.ahora(),
                )
            except HuellaNoCoincide as error:
                raise _Fallo(error.codigo) from error
            if await self._polizas.actualizar(registrada, tx):
                return registrada
            return await self._releer(poliza, tx)

    async def _activar(self, poliza: Poliza, correlation_id: str) -> tuple[Poliza, bool]:
        documento = poliza.documento
        if documento is None:
            raise RuntimeError("emision: activar sin documento registrado")
        async with self._transacciones.transaccion() as tx:
            try:
                # Los mismos datos del PDF: el evento lleva su prima, suma, moneda y periodicidad.
                datos = await self._datos.ejecutar(poliza, documento.fecha_documento, tx)
            except DatosDocumentoNoDisponibles as error:
                raise _Fallo(error.codigo) from error
            except PolizaInvalida as error:
                raise _Fallo(CODIGO_DATOS_INVALIDOS) from error
            activa = poliza.activar(self._reloj.ahora())
            if not await self._polizas.actualizar(activa, tx):
                return await self._releer(poliza, tx), False
            await self._eventos.emitida(activa, datos, correlation_id, tx)
            return activa, True

    async def _releer(self, poliza: Poliza, tx: T) -> Poliza:
        releida = await self._polizas.por_id(poliza.poliza_id, tx)
        if releida is None:
            # El disparador de 004 impide borrar: no debería ocurrir.
            raise RuntimeError("emision: la póliza desapareció")
        return releida

    async def _registrar_fallo(self, poliza: Poliza, codigo: str) -> None:
        """Con la guarda: si otra tarea avanzó, no se pisa lo que registró. Si tampoco se puede
        escribir (la base), se registra y se sigue."""
        if poliza.estado is not EstadoPoliza.EMITIENDO:
            return
        try:
            async with self._transacciones.transaccion() as tx:
                await self._polizas.actualizar(
                    poliza.registrar_fallo(codigo, self._reloj.ahora()), tx
                )
        except Exception as error:  # noqa: BLE001 - el mensaje puede traer datos
            logger.error(
                MENSAJE_INESPERADO, extra={**_ids(poliza), "error_type": type(error).__name__}
            )


async def _parada_dentro_de(parada: asyncio.Event, segundos: float) -> bool:
    """True si llega la parada antes de `segundos`."""
    with suppress(TimeoutError):
        await asyncio.wait_for(parada.wait(), segundos)
    return parada.is_set()
