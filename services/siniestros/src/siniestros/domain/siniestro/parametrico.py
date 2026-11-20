"""Siniestro paramétrico (HU-W22, DS3-23). Puro: el instante y el identificador llegan como argumento.

Un evento externo que activa una póliza crea **un** siniestro `APROBADO`, sin analista, con la
póliza, la medición, la regla y su versión, y `valorIndemnizar` igual al valor de la cobertura
(`deducible` 0: lo exige la restricción `siniestros_liquidacion_segun_estado`, W20).

La clave del caso es el par `(eventoId, polizaId)` y la `Idempotency-Key` es su texto
(`<eventoId>:<polizaId>`).

Lo que la cobertura no sabe, el dominio lo fija así (R-178 y R-189 de la bitácora del Sprint 3):

- **Amparo:** el propio tipo de evento, con el valor de la cobertura como suma asegurada; `causa` es
  ese código.
- **Carencia 0 y plazo de aviso de 365 días:** un paramétrico no depende de un aviso del cliente.
  Con esos valores las verificaciones de «Cobertura» no marcan un incumplimiento que no aplica.
- **Primas al día:** una cobertura `ACTIVA` en `pagos-parametricos` es una póliza vigente.
- **Beneficiario:** el titular de la póliza, con referencia opaca derivada del cliente
  (`ben-par-<clienteId>`) y una cuenta enmascarada sintética fija. Nunca la cuenta real.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Final
from uuid import UUID

from siniestros.domain.siniestro.errores import SiniestroInvalido
from siniestros.domain.siniestro.modelos import (
    Amparo,
    Beneficiario,
    CopiaPoliza,
    EstadoSiniestro,
    EventoParametrico,
    OrigenSiniestro,
    Siniestro,
    Ubicacion,
    Vigencia,
)

ROL_SISTEMA: Final = "SISTEMA_PARAMETRICO"
CARENCIA_PARAMETRICO_DIAS: Final = 0
PLAZO_AVISO_PARAMETRICO_DIAS: Final = 365
PREFIJO_BENEFICIARIO: Final = "ben-par-"
CUENTA_PARAMETRICA: Final = "****0000"
_MAX_DECIMALES: Final = 2


def clave_idempotente(evento_id: UUID, poliza_id: UUID) -> str:
    """`<eventoId>:<polizaId>`: la `Idempotency-Key` de la creación y la clave del caso."""
    return f"{evento_id}:{poliza_id}"


def beneficiario_de(cliente_id: UUID) -> Beneficiario:
    """El titular cobra su indemnización: referencia opaca derivada del cliente, nunca la cuenta."""
    return Beneficiario(f"{PREFIJO_BENEFICIARIO}{cliente_id}", CUENTA_PARAMETRICA)


def validar_valor(texto: str) -> Decimal:
    """El valor de la cobertura: mayor que cero y con a lo sumo dos decimales (DS2-16)."""
    try:
        valor = Decimal(texto)
    except InvalidOperation:
        raise SiniestroInvalido("valor: no es un decimal") from None
    if not valor.is_finite() or valor <= 0:
        raise SiniestroInvalido("valor: debe ser mayor que cero")
    if valor.as_tuple().exponent < -_MAX_DECIMALES:  # type: ignore[operator]
        raise SiniestroInvalido("valor: a lo sumo dos decimales")
    return valor


@dataclass(frozen=True)
class DatosParametrico:
    """Lo que `pagos-parametricos` informa de la activación (el cuerpo de la solicitud)."""

    evento: EventoParametrico
    poliza_id: UUID
    cliente_id: UUID
    numero: str
    ramo: str
    vigencia: Vigencia
    moneda: str
    valor: Decimal

    @property
    def clave(self) -> str:
        return clave_idempotente(self.evento.evento_id, self.poliza_id)


def nuevo_siniestro(
    datos: DatosParametrico,
    *,
    siniestro_id: UUID,
    codigo: str,
    ahora: datetime,
    correlation_id: str,
) -> Siniestro:
    """El siniestro paramétrico: nace `APROBADO` y su reloj ya está detenido en `ahora`."""
    evento = datos.evento
    copia = CopiaPoliza(
        numero=datos.numero,
        ramo=datos.ramo,
        moneda=datos.moneda,
        amparos=(Amparo(evento.tipo_evento, evento.tipo_evento, f"{datos.valor:f}"),),
        vigencia=datos.vigencia,
        carencia_dias=CARENCIA_PARAMETRICO_DIAS,
        plazo_aviso_dias=PLAZO_AVISO_PARAMETRICO_DIAS,
        primas_al_dia=True,
        beneficiario=beneficiario_de(datos.cliente_id),
    )
    return Siniestro(
        id=siniestro_id,
        codigo=codigo,
        origen=OrigenSiniestro.PARAMETRICO,
        estado=EstadoSiniestro.APROBADO,
        poliza_id=datos.poliza_id,
        cliente_id=datos.cliente_id,
        copia_poliza=copia,
        causa=evento.tipo_evento,
        fecha_evento=evento.observado_en,
        ubicacion_declarada=Ubicacion(descripcion=f"Zona {evento.zona}"),
        reportado_en=ahora,
        correlation_id=correlation_id,
        decidido_en=ahora,
        moneda=datos.moneda,
        deducible=Decimal("0.00"),
        valor_indemnizar=datos.valor,
        evento=evento,
    )
