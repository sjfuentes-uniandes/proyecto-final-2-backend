class DispositivoNoAutorizado(Exception):
    """El dispositivo es de otro cliente. Marcadora: no lleva datos del dispositivo."""


class DispositivoNoEncontrado(Exception):
    """El dispositivo no existe. Marcadora."""
