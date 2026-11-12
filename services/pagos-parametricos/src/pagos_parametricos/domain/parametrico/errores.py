"""Causas de un fallo de la evaluación que H10 reconoce por el nombre de la excepción (HU-W24, W24
§9): el consumidor anota `type(error).__name__` y la cola de descarte las traduce a
`REFERENCIA_INEXISTENTE` ("La póliza referida no existe"). Sin mensaje: nunca llevan datos."""


class PolizaInexistente(Exception):
    """El evento nombra una póliza que no está en la copia local de coberturas."""


class CoberturaInexistente(Exception):
    """La póliza existe, pero no tiene una cobertura paramétrica con regla (p. ej. una cancelación
    que llegó antes que la emisión)."""
