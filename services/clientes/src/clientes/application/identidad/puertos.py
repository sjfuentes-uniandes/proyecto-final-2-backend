from typing import Any, Protocol
from uuid import UUID

from clientes.domain.identidad.modelos import Cliente, DatosCliente, ResultadoKyc


class RepositorioClientes(Protocol):
    def obtener(self, cliente_id: UUID) -> Cliente | None: ...

    def por_sub(self, sub: str) -> Cliente | None: ...

    def por_registro(self, registro_id: UUID) -> Cliente | None: ...

    def asegurar(self, sub: str) -> Cliente:
        """Devuelve el cliente del `sub`, creándolo si no existe. Idempotente (AC3)."""
        ...

    def asegurar_registro(self, registro_id: UUID, correo: str) -> Cliente:
        """Devuelve el cliente del registro, creándolo si no existe. Idempotente (AC3)."""
        ...

    def vincular(self, cliente_id: UUID, sub: str) -> Cliente:
        """Asocia el `sub` al cliente. Lanza RegistroYaVinculado si ya es de otro."""
        ...

    def guardar(self, cliente: Cliente) -> None: ...

    def asegurar_por_documento(self, datos: DatosCliente) -> tuple[Cliente, bool]:
        """Devuelve el cliente de ese documento y si se creó ahora. Idempotente (AC4).

        Un documento ya registrado devuelve el cliente existente sin modificar sus datos.
        """
        ...


class VeredictoDocumento(Protocol):
    @property
    def resultado(self) -> ResultadoKyc: ...

    @property
    def motivo(self) -> str | None: ...

    @property
    def referencia(self) -> str: ...

    @property
    def proveedor(self) -> str: ...


class ConsultaVerificacionDocumental(Protocol):
    async def verificar_documento(
        self, cliente_id: UUID, datos: DatosCliente
    ) -> VeredictoDocumento:
        """Lanza IdentidadNoDisponible si el proveedor no respondió o falló."""
        ...


class VeredictoKyc(Protocol):
    # Propiedades de solo lectura: el adaptador entrega una dataclass congelada.
    @property
    def resultado(self) -> ResultadoKyc: ...

    @property
    def motivo(self) -> str | None: ...


class ConsultaVerificacion(Protocol):
    async def verificar(
        self, cliente_id: UUID, selfie: str, prueba_de_vida: dict[str, Any]
    ) -> VeredictoKyc:
        """Lanza IdentidadNoDisponible si el proveedor no respondió o falló."""
        ...
