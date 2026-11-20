"""Causas de un fallo de la evaluación que H10 reconoce por el nombre de la excepción (HU-W24, W24
§9): el consumidor anota `type(error).__name__` y la cola de descarte las traduce a
`REFERENCIA_INEXISTENTE` ("La póliza referida no existe"). Sin mensaje: nunca llevan datos."""


class PolizaInexistente(Exception):
    """El evento nombra una póliza que no está en la copia local de coberturas."""


class CoberturaInexistente(Exception):
    """La póliza existe, pero no tiene una cobertura paramétrica con regla (p. ej. una cancelación
    que llegó antes que la emisión)."""


class ActivacionFallida(Exception):
    """HU-W22: al menos una activación del evento no pudo crear su siniestro (`siniestros` no
    respondió, rechazó la solicitud o la cobertura estaba incompleta). La continuación lanza esta
    excepción **después** de intentar todas, para que el mensaje no se borre: SQS lo reentrega,
    `reanudar` reintenta las que siguen `PENDIENTE` y, tras `maxReceiveCount`, va a la DLQ (AC4).
    El consumidor anota su nombre como causa (`ERROR_PROCESAMIENTO` en H10); no lleva datos."""

    def __init__(self, fallidas: int, total: int) -> None:
        super().__init__(f"{fallidas} de {total}")
        self.fallidas = fallidas
        self.total = total


class SiniestrosRechazo(Exception):
    """`siniestros` respondió con un código que la creación no promete (un 4xx, o un cuerpo fuera de
    su contrato). Lleva el estado y el `codigo` del error, nunca el cuerpo."""

    def __init__(self, estado: int, codigo: str | None) -> None:
        super().__init__(f"{estado} {codigo or '-'}")
        self.estado = estado
        self.codigo = codigo
