"""Errores funcionales de siniestros (HU-W19). Cada uno lleva su código estable de
`contracts/codigos-error.md`; `RutaB2` los responde como {codigo, mensaje, correlationId, detalles}.
Los mensajes no incluyen datos del caso."""

from collections.abc import Sequence
from typing import Any

from solventa_common.errors import ErrorNegocio


class ActorRequerido(ErrorNegocio):
    def __init__(self) -> None:
        super().__init__("ACTOR_REQUERIDO", "Falta el actor autenticado (X-Actor-Id)", 401)


class RolNoAutorizado(ErrorNegocio):
    def __init__(self) -> None:
        super().__init__("ROL_NO_AUTORIZADO", "El rol no puede usar este recurso", 403)


class SiniestroNoEncontrado(ErrorNegocio):
    def __init__(self) -> None:
        super().__init__("SINIESTRO_NO_ENCONTRADO", "El siniestro no existe", 404)


class EvidenciaNoEncontrada(ErrorNegocio):
    def __init__(self) -> None:
        super().__init__("EVIDENCIA_NO_ENCONTRADA", "La evidencia no existe en este siniestro", 404)


class EvidenciaNoDisponible(ErrorNegocio):
    """El registro existe pero el objeto no está en el almacén: no se emite la URL."""

    def __init__(self) -> None:
        super().__init__(
            "EVIDENCIA_NO_DISPONIBLE", "El archivo de la evidencia no está disponible", 404
        )


class ColaSinCasos(ErrorNegocio):
    def __init__(self) -> None:
        super().__init__("COLA_SIN_CASOS", "No hay siniestros sin asignar", 404)


class IdempotenciaConflicto(ErrorNegocio):
    def __init__(self) -> None:
        super().__init__(
            "IDEMPOTENCIA_CONFLICTO", "La Idempotency-Key ya se usó con otra solicitud", 422
        )


class SolicitudInvalida(ErrorNegocio):
    def __init__(self, campos: Sequence[str]) -> None:
        detalles: dict[str, Any] = {"campos": list(campos)}
        super().__init__("SOLICITUD_INVALIDA", "Solicitud inválida", 422, detalles)


class DependenciaNoDisponible(ErrorNegocio):
    def __init__(self, dependencia: str) -> None:
        super().__init__(
            "DEPENDENCIA_NO_DISPONIBLE",
            "Una dependencia no responde",
            503,
            {"dependencia": dependencia},
        )
