"""Errores de dominio de la póliza (HU-W17, 17.6). Los mensajes nombran el campo, nunca su valor."""


class PolizaInvalida(ValueError):
    """La póliza, su documento o su firma no cumplen una invariante."""


class TransicionInvalida(Exception):
    """La transición no se permite desde el estado actual: un punto de control ya registrado, uno
    que falta o cualquier intento sobre una póliza ACTIVA (plan §d)."""

    def __init__(self, desde: object, hacia: str) -> None:
        super().__init__(f"{desde} -> {hacia}")
        self.desde = desde
        self.hacia = hacia


class ProductoSinRamo(Exception):
    """El producto no está en la tabla de ramos de polizas (ADR 0014, decisión 2): sin ramo no hay
    número."""

    codigo = "PRODUCTO_SIN_RAMO"

    def __init__(self, producto_id: str) -> None:
        super().__init__(self.codigo)
        self.producto_id = producto_id


class HuellaNoCoincide(Exception):
    """La huella firmada no es la del documento registrado."""

    codigo = "HUELLA_NO_COINCIDE"

    def __init__(self) -> None:
        super().__init__(self.codigo)


class DatosDocumentoNoDisponibles(Exception):
    """HU-W17 (17.7): no están la aceptación de la póliza, su decisión o su recaudo confirmado. Sin
    ellos no hay PDF; la emisión sigue EMITIENDO con este código."""

    codigo = "DATOS_DOCUMENTO_NO_DISPONIBLES"

    def __init__(self) -> None:
        super().__init__(self.codigo)


class FalloAlmacenDocumentos(Exception):
    """HU-W17 (17.7): el bucket no guardó el documento o no se pudo firmar su URL. `codigo` es el
    que la emisión registra como fallo (17.8); `error_s3`, el código de error de S3 si lo hubo. El
    mensaje de S3 no se conserva: puede traer el bucket y la clave."""

    # 4xx de S3: permisos, política del bucket (cifrado) o bucket inexistente. No se arregla solo.
    RECHAZADO = "ALMACEN_RECHAZADO"
    # 5xx, timeout o sin conexión: el próximo ciclo reintenta.
    NO_DISPONIBLE = "ALMACEN_NO_DISPONIBLE"
    # PutObject sin VersionId: el bucket no está versionado y no habría referencia estable.
    SIN_VERSION = "ALMACEN_SIN_VERSION"

    def __init__(self, codigo: str, error_s3: str | None = None) -> None:
        super().__init__(codigo)
        self.codigo = codigo
        self.error_s3 = error_s3
