"""Rechazos de un evento externo (HU-W21, DS3-16). La API los traduce a su código estable; ninguno
lleva el cuerpo, la firma ni el secreto."""


class FirmaInvalida(Exception):
    """Fuente desconocida, firma ausente o que no corresponde al cuerpo crudo y la marca."""


class MarcaFueraDeRango(Exception):
    """La marca de tiempo falta, no es un instante o se aparta del reloj más del máximo."""


class ContratoInvalido(Exception):
    """El cuerpo no es JSON, no cumple `EventoExterno` o su versión no está soportada.

    `campos` son las rutas de los campos inválidos (camelCase), nunca sus valores.
    """

    def __init__(self, campos: tuple[str, ...] = ()) -> None:
        super().__init__("contrato inválido")
        self.campos = campos
