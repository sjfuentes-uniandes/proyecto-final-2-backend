"""Errores de negocio de la suscripción; la API los traduce a B2 (HU-W13)."""


class CotizacionNoEncontrada(Exception):
    """La cotización no existe en cotizacion."""


class OfertaVencida(Exception):
    """ahora ≥ vigenteHasta: no se decide sobre una oferta vencida (DS2-10)."""


class IdentidadNoVerificada(Exception):
    """La identidad del cliente fue rechazada (DS2-17)."""


class IdentidadPendiente(Exception):
    """La verificación de identidad del cliente no terminó (DS2-17)."""


class ReglasNoVigentes(Exception):
    """El producto no tiene reglas de suscripción vigentes, o no existe la versión pedida."""


class ConflictoIdempotencia(Exception):
    """La Idempotency-Key ya se usó con otra solicitud."""


class DecisionNoEncontrada(Exception):
    """No existe una decisión con ese identificador."""


class CursorInvalido(Exception):
    """El cursor de la bandeja no es uno que haya entregado polizas."""


class RangoInvalido(Exception):
    """El rango de fechas del resumen está vacío o invertido."""


class DependenciaNoDisponible(Exception):
    """cotizacion o catalogo no respondieron a tiempo o violaron su contrato: falla cerrada."""

    def __init__(self, dependencia: str, tiempo_agotado: bool = False) -> None:
        super().__init__(dependencia)
        self.dependencia = dependencia
        self.tiempo_agotado = tiempo_agotado
