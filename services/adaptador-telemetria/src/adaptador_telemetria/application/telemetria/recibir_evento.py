"""Caso de uso: recibir un evento externo firmado (HU-W21, DS3-16; operación `telemetria.recibir`).

Orden (cada paso solo corre si el anterior pasó; un rechazo no archiva ni escribe nada):

1. fuente y firma, sobre el cuerpo crudo y en tiempo constante;
2. desfase de la marca de tiempo (± `desfase_max_s`): después de la firma, así quien no puede firmar
   no averigua la ventana;
3. contrato (`EventoExterno` v1);
4. ¿ya se recibió? Un reenvío responde con el `eventoId` y el `recibidoEn` del primero y **no
   vuelve a archivar**: el original que se conserva es el del primer envío;
5. archivo del original en `<prefijo><AAAA>/<MM>/<DD>/<eventoId>.json`, **antes** de la transacción
   y sin conexión de base abierta (§13, regla 8). Si dos envíos llegan a la vez, ambos escriben la
   misma clave;
6. una transacción: `eventos_recibidos` (`ON CONFLICT` = duplicado) y `EventoParametricoObservado`
   en el Outbox.

Nunca se registran el cuerpo, la firma ni el secreto; el log de cierre lleva `evento_id`, `fuente` y
`resultado`.
"""

import logging
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from solventa_common.telemetry import operacion

from adaptador_telemetria.application.telemetria.puertos import (
    Archivo,
    DependenciaNoDisponible,
    Registro,
    Reloj,
)
from adaptador_telemetria.domain.telemetria.contrato import validar_contrato
from adaptador_telemetria.domain.telemetria.errores import (
    ContratoInvalido,
    FirmaInvalida,
    MarcaFueraDeRango,
)
from adaptador_telemetria.domain.telemetria.firma import (
    dentro_de_desfase,
    leer_marca,
    verificar_firma,
)
from adaptador_telemetria.domain.telemetria.modelos import EventoCanonico, clave_archivo

OPERACION = "telemetria.recibir"
ACEPTADO = "aceptado"
DUPLICADO = "duplicado"
FIRMA_INVALIDA = "firma_invalida"
MARCA_INVALIDA = "marca_invalida"
CONTRATO_INVALIDO = "contrato_invalido"
ERROR = "error"
MENSAJE_CIERRE = "telemetria: evento recibido"

logger = logging.getLogger("adaptador_telemetria.recibir")


def _metadatos(evento: EventoCanonico, recibido_en: datetime) -> dict[str, str]:
    """Metadatos del objeto: sin ellos no se podría reconstruir el sobre desde el original, porque
    la fuente viaja en un encabezado y no en el cuerpo (reproceso de HU-W24, guía §7)."""
    return {
        "fuente": evento.fuente,
        "id-externo": evento.id_externo,
        "evento-id": str(evento.evento_id),
        "recibido-en": recibido_en.astimezone(UTC).isoformat().replace("+00:00", "Z"),
    }


@dataclass(frozen=True)
class Recepcion:
    evento_id: UUID
    recibido_en: datetime
    duplicado: bool


class RecibirEvento:
    def __init__(
        self,
        *,
        secreto: str | None,
        fuentes: Collection[str],
        desfase_max_s: int,
        prefijo_archivo: str,
        archivo: Archivo,
        registro: Registro,
        reloj: Reloj,
    ) -> None:
        self._secreto = secreto
        self._fuentes = frozenset(fuentes)
        self._desfase_max_s = desfase_max_s
        self._prefijo = prefijo_archivo
        self._archivo = archivo
        self._registro = registro
        self._reloj = reloj

    async def __call__(
        self,
        fuente: str | None,
        marca_tiempo: str | None,
        firma: str | None,
        cuerpo: bytes,
        correlation_id: str,
    ) -> Recepcion:
        with operacion(OPERACION) as op:
            # La fuente solo se registra si es una de las autorizadas: antes de autenticar, es
            # texto de quien llama.
            fuente_conocida = fuente if fuente in self._fuentes else None
            try:
                autentica = self._autenticar(fuente_conocida, marca_tiempo, firma, cuerpo)
            except FirmaInvalida:
                self._cerrar(op, FIRMA_INVALIDA, None, None)
                raise
            except MarcaFueraDeRango:
                self._cerrar(op, MARCA_INVALIDA, None, None)
                raise
            try:
                evento = validar_contrato(cuerpo, autentica)
            except ContratoInvalido:
                self._cerrar(op, CONTRATO_INVALIDO, autentica, None)
                raise
            evento_id = evento.evento_id

            try:
                previo = await self._registro.recibido(evento_id)
                if previo is not None:
                    self._cerrar(op, DUPLICADO, autentica, evento_id)
                    return Recepcion(evento_id, previo, duplicado=True)

                ahora = self._reloj.ahora()
                clave = clave_archivo(self._prefijo, ahora, evento_id)
                await self._archivo.guardar(clave, cuerpo, _metadatos(evento, ahora))
                registrado = await self._registro.registrar(evento, ahora, clave, correlation_id)
            except DependenciaNoDisponible:
                self._cerrar(op, ERROR, autentica, evento_id)
                raise
            resultado = ACEPTADO if registrado.nuevo else DUPLICADO
            self._cerrar(op, resultado, autentica, evento_id)
            return Recepcion(evento_id, registrado.recibido_en, duplicado=not registrado.nuevo)

    def _autenticar(
        self, fuente: str | None, marca_tiempo: str | None, firma: str | None, cuerpo: bytes
    ) -> str:
        """La fuente autenticada, o `FirmaInvalida` / `MarcaFueraDeRango`."""
        # Sin secreto (credenciales ausentes o ilegibles) no se acepta ningún envío.
        if fuente is None or self._secreto is None or marca_tiempo is None:
            raise FirmaInvalida
        if not verificar_firma(self._secreto, marca_tiempo, cuerpo, firma):
            raise FirmaInvalida
        marca = leer_marca(marca_tiempo)
        if marca is None or not dentro_de_desfase(marca, self._reloj.ahora(), self._desfase_max_s):
            raise MarcaFueraDeRango
        return fuente

    @staticmethod
    def _cerrar(op: operacion, resultado: str, fuente: str | None, evento_id: UUID | None) -> None:
        op.marcar(resultado)
        extra: dict[str, str] = {"resultado": resultado}
        if fuente is not None:
            extra["fuente"] = fuente
        if evento_id is not None:
            extra["evento_id"] = str(evento_id)
        logger.info(MENSAJE_CIERRE, extra=extra)
