from typing import Literal

MotivoNoDisponible = Literal["TIEMPO_AGOTADO", "ERROR"]


class ClienteNoEncontrado(Exception):
    """El cliente no existe o el titular del token no se relaciona con ninguno.

    Excepción marcadora: se distingue por tipo y no lleva datos del cliente.
    """


class ServicioNoDisponible(Exception):
    """`clientes` no respondió a tiempo, falló o violó su contrato."""

    def __init__(self, motivo: MotivoNoDisponible) -> None:
        super().__init__(motivo)
        self.motivo: MotivoNoDisponible = motivo
