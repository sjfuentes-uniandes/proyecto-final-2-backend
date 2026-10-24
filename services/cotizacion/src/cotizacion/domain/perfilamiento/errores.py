"""Errores de dominio del perfilamiento.

`DependenciaNoDisponible` es el error de dominio de cotizacion; no es la clase homónima de
`solventa_common.resiliencia`, que nunca se importa en este dominio.
"""


class ClienteNoEncontrado(Exception):
    """El cliente no existe en clientes."""


class IdentidadNoVerificada(Exception):
    """La identidad del cliente fue rechazada."""


class IdentidadPendiente(Exception):
    """La verificación de identidad del cliente no ha terminado."""


class ModeloNoDisponible(Exception):
    """No hay un modelo de riesgo vigente o con la versión pedida."""


class DependenciaNoDisponible(Exception):
    """Una dependencia (clientes, catalogo) no respondió o violó su contrato."""

    def __init__(self, dependencia: str) -> None:
        super().__init__(dependencia)
        self.dependencia = dependencia


class DependenciaAgotada(DependenciaNoDisponible):
    """La dependencia no respondió dentro de su timeout efectivo (HU-W05).

    Es una DependenciaNoDisponible: quien no la distingue la trata igual (503 recuperable). La
    cotización la clasifica como TIEMPO_AGOTADO para la política de HU-W06.
    """


class ConflictoIdempotencia(Exception):
    """La clave de idempotencia ya se usó con otra solicitud."""


class PerfilNoEncontrado(Exception):
    """El perfil pedido no existe."""


class DatosDeclaradosInvalidos(Exception):
    """Los datos declarados no cumplen las reglas del producto."""

    def __init__(self, campos: tuple[str, ...]) -> None:
        super().__init__(", ".join(campos))
        self.campos = campos
