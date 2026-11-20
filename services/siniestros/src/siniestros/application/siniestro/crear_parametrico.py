"""Creación del siniestro paramétrico (HU-W22, DS3-23): `POST /v1/siniestros/parametricos`.

Lo llama `pagos-parametricos` después del commit de W21, con `Idempotency-Key = <eventoId>:<polizaId>`.
Crea **un** siniestro `APROBADO` por evento y póliza, aunque la solicitud llegue varias veces o dos
réplicas la procesen a la vez:

1. **Lectura corta** (una transacción, sin escribir): la clave de idempotencia
   (`parametrico:<Idempotency-Key>`, tabla `idempotencia` de W19) y el siniestro de ese evento y esa
   póliza. Si ya existe es una repetición (200): con otro cuerpo, 422 `IDEMPOTENCIA_CONFLICTO`.
2. **El código** `88-AAAA-NNNN` sale de `secuencia_codigo` en su propia transacción corta: nunca hay
   dos conexiones del pool anidadas, y una repetición no gasta un consecutivo.
3. **Escritura** (una sola transacción): vuelve a mirar la idempotencia (el bloqueo asesor serializa
   la misma clave), inserta el siniestro con `ON CONFLICT (evento_id, poliza_id) DO NOTHING` (la
   segunda barrera: si otra transacción ganó, se relee y se responde 200), el historial
   `CREADO_AUTOMATICAMENTE` y `SiniestroAprobado` en el Outbox, y guarda la respuesta de la clave.

Sin llamadas a otros servicios ni a S3. Los textos y valores del evento no salen a los registros.
"""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID, uuid4

from solventa_common import correlation
from solventa_common.errors import ErrorNegocio
from solventa_common.telemetry import RESULTADO_RECHAZADO, operacion

from siniestros.application.siniestro.errores import IdempotenciaConflicto, SolicitudInvalida
from siniestros.application.siniestro.puertos import (
    Codigos,
    Reloj,
    SiniestrosRepositorio,
    TxSiniestros,
)
from siniestros.application.siniestro.vistas import Plazos, ResumenCaso, resumen_de
from siniestros.domain.siniestro.modelos import EntradaHistorial, EstadoSiniestro, Siniestro
from siniestros.domain.siniestro.parametrico import (
    ROL_SISTEMA,
    DatosParametrico,
    nuevo_siniestro,
)
from siniestros.domain.siniestro.tramite import ACCION_CREADO_AUTOMATICAMENTE
from siniestros.domain.siniestro.transiciones import COLOMBIA, referencia_pago

PREFIJO_CLAVE: Final = "parametrico:"
_OPERACION = "POST /v1/siniestros/parametricos"

# Resultados de `siniestro.crear_parametrico` (operaciones.md).
RESULTADO_CREADO: Final = "creado"
RESULTADO_EXISTENTE: Final = "existente"


@dataclass(frozen=True)
class ResultadoCreacion:
    """El siniestro del evento y la póliza, y si esta solicitud lo creó (201) o ya existía (200)."""

    resumen: ResumenCaso
    creado: bool


def huella_de(datos: DatosParametrico) -> str:
    """Identifica la solicitud: la misma clave con otro cuerpo es un conflicto. Sale de lo que el
    cuerpo decide (nunca de los registros ni de la hora)."""
    e = datos.evento
    cuerpo: Mapping[str, Any] = {
        "eventoId": str(e.evento_id),
        "polizaId": str(datos.poliza_id),
        "clienteId": str(datos.cliente_id),
        "poliza": {
            "numero": datos.numero,
            "ramo": datos.ramo,
            "vigencia": [datos.vigencia.desde.isoformat(), datos.vigencia.hasta.isoformat()],
            "moneda": datos.moneda,
        },
        "evento": [
            e.fuente,
            e.tipo_evento,
            e.valor_observado,
            e.umbral,
            e.unidad,
            e.zona,
            e.observado_en.isoformat(),
            e.evento_origen,
        ],
        "regla": [e.regla, e.version_regla],
        "valor": f"{datos.valor:f}",
    }
    canonico = json.dumps(
        {"op": _OPERACION, "cuerpo": cuerpo},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonico.encode()).hexdigest()


class CrearParametrico:
    def __init__(
        self, repo: SiniestrosRepositorio, codigos: Codigos, reloj: Reloj, plazos: Plazos
    ) -> None:
        self._repo = repo
        self._codigos = codigos
        self._reloj = reloj
        self._plazos = plazos

    async def ejecutar(self, clave: str, datos: DatosParametrico) -> ResultadoCreacion:
        with operacion("siniestro.crear_parametrico") as op:
            try:
                resultado = await self._crear(clave, datos)
            except ErrorNegocio:
                op.marcar(RESULTADO_RECHAZADO)
                raise
            op.marcar(RESULTADO_CREADO if resultado.creado else RESULTADO_EXISTENTE)
            return resultado

    async def _crear(self, clave: str, datos: DatosParametrico) -> ResultadoCreacion:
        # La clave es el par evento-póliza (DS3-23): una clave distinta del cuerpo es un error.
        if clave.lower() != datos.clave.lower():
            raise SolicitudInvalida(["Idempotency-Key"])
        huella = huella_de(datos)
        clave_guardada = f"{PREFIJO_CLAVE}{datos.clave}"
        ahora = self._reloj.ahora()

        async with self._repo.transaccion() as tx:
            repetido = await self._repetido(tx, clave_guardada, huella, datos, ahora)
        if repetido is not None:
            return repetido

        codigo = await self._codigos.siguiente(ahora.astimezone(COLOMBIA).year)
        nuevo = nuevo_siniestro(
            datos,
            siniestro_id=uuid4(),
            codigo=codigo,
            ahora=ahora,
            correlation_id=correlation.obtener() or str(uuid4()),
        )
        async with self._repo.transaccion() as tx:
            # Otra réplica pudo ganar mientras se pedía el código (el bloqueo de la clave sigue
            # dentro de esta transacción hasta el commit).
            repetido = await self._repetido(tx, clave_guardada, huella, datos, ahora)
            if repetido is not None:
                return repetido
            if not await tx.insertar_parametrico(nuevo):
                existente = await tx.siniestro_parametrico(datos.evento.evento_id, datos.poliza_id)
                assert existente is not None  # el índice único lo rechazó por otro que existe
                await tx.guardar_idempotencia(
                    clave_guardada, huella, {"siniestroId": str(existente.id)}, ahora
                )
                return self._resultado(existente, ahora, creado=False)
            await tx.agregar_historial(_historial(nuevo, datos, ahora))
            await tx.evento_aprobado(nuevo, referencia_pago(nuevo.codigo), None, ahora)
            await tx.guardar_idempotencia(
                clave_guardada, huella, {"siniestroId": str(nuevo.id)}, ahora
            )
        return self._resultado(nuevo, ahora, creado=True)

    async def _repetido(
        self,
        tx: TxSiniestros,
        clave_guardada: str,
        huella: str,
        datos: DatosParametrico,
        ahora: datetime,
    ) -> ResultadoCreacion | None:
        """La respuesta de una solicitud anterior con la misma clave, o el siniestro que ya existe
        para ese evento y esa póliza; `None` si hay que crearlo."""
        previo = await tx.adquirir_idempotencia(clave_guardada)
        if previo is not None:
            if previo.huella != huella:
                raise IdempotenciaConflicto
            guardado = await tx.siniestro(UUID(previo.respuesta["siniestroId"]))
            assert guardado is not None  # el siniestro no se borra
            return self._resultado(guardado, ahora, creado=False)
        existente = await tx.siniestro_parametrico(datos.evento.evento_id, datos.poliza_id)
        if existente is None:
            return None
        # El par ya tiene siniestro pero su clave no (se purgó): se vuelve a guardar.
        await tx.guardar_idempotencia(
            clave_guardada, huella, {"siniestroId": str(existente.id)}, ahora
        )
        return self._resultado(existente, ahora, creado=False)

    def _resultado(
        self, siniestro: Siniestro, ahora: datetime, *, creado: bool
    ) -> ResultadoCreacion:
        return ResultadoCreacion(resumen_de(siniestro, ahora, self._plazos), creado)


def _historial(nuevo: Siniestro, datos: DatosParametrico, ahora: datetime) -> EntradaHistorial:
    """`CREADO_AUTOMATICAMENTE`: actor `SISTEMA_PARAMETRICO`, sin actor ni nombre, y solo los
    identificadores y la regla (ni la medición ni el cliente)."""
    evento = datos.evento
    return EntradaHistorial(
        id=uuid4(),
        siniestro_id=nuevo.id,
        accion=ACCION_CREADO_AUTOMATICAMENTE,
        ocurrido_en=ahora,
        estado_anterior=None,
        estado_nuevo=EstadoSiniestro.APROBADO,
        actor_id=None,
        actor_rol=ROL_SISTEMA,
        actor_nombre=None,
        detalles={
            "eventoId": str(evento.evento_id),
            "polizaId": str(datos.poliza_id),
            "regla": evento.regla,
            "versionRegla": evento.version_regla,
        },
    )
