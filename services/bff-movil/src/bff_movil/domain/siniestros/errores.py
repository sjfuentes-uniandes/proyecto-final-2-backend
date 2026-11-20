from solventa_common.errors import ErrorNegocio


class PolizaNoVigente(ErrorNegocio):
    def __init__(self) -> None:
        super().__init__("POLIZA_NO_VIGENTE", "La póliza no está vigente", 409)
