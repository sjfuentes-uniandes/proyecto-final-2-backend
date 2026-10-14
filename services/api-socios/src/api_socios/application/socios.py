import logging
from collections.abc import Callable
from typing import Protocol

from solventa_common.telemetry import RESULTADO_RECHAZADO, operacion

from api_socios.domain.socios import (
    CredencialesEmitidas,
    NuevoSocio,
    Permiso,
    Plan,
    Socio,
    SocioNoEncontrado,
)

logger = logging.getLogger("api_socios.socios")


class RepositorioSocios(Protocol):
    def siguiente_identificador(self) -> str: ...

    def guardar(self, socio: Socio) -> None: ...

    def obtener(self, socio_id: str) -> Socio | None: ...

    def por_client_id(self, client_id: str) -> Socio | None: ...

    def listar(self) -> list[Socio]: ...


class ProveedorIdentidad(Protocol):
    def crear_cliente(self, socio_id: str, permisos: frozenset[Permiso]) -> tuple[str, str]: ...

    def eliminar_cliente(self, client_id: str) -> None: ...


class GestorApiKeys(Protocol):
    def crear(self, socio_id: str, nombre: str) -> tuple[str, str]: ...

    def asociar_plan(self, api_key_id: str, plan: Plan) -> None: ...

    def deshabilitar(self, api_key_id: str) -> None: ...

    def eliminar(self, api_key_id: str) -> None: ...


class AdministrarSocios:
    def __init__(
        self, repositorio: RepositorioSocios, identidad: ProveedorIdentidad, api_keys: GestorApiKeys
    ) -> None:
        self.repositorio = repositorio
        self.identidad = identidad
        self.api_keys = api_keys

    @operacion("habilitar_socio")
    def habilitar(self, nuevo: NuevoSocio) -> tuple[Socio, CredencialesEmitidas]:
        """Si un paso falla, deshace los anteriores."""
        socio_id = self.repositorio.siguiente_identificador()
        deshacer: list[Callable[[], None]] = []
        try:
            client_id, secreto = self.identidad.crear_cliente(socio_id, nuevo.permisos)
            deshacer.append(lambda: self.identidad.eliminar_cliente(client_id))
            api_key_id, api_key = self.api_keys.crear(socio_id, nuevo.nombre)
            deshacer.append(lambda: self.api_keys.eliminar(api_key_id))
            self.api_keys.asociar_plan(api_key_id, nuevo.plan)
            socio = Socio(
                id=socio_id,
                nombre=nuevo.nombre,
                canal=nuevo.canal,
                plan=nuevo.plan,
                permisos=nuevo.permisos,
                client_id=client_id,
                api_key_id=api_key_id,
            )
            self.repositorio.guardar(socio)
        except Exception:
            self._compensar(socio_id, deshacer)
            raise
        logger.info(
            "socio habilitado",
            extra={
                "socio_id": socio_id,
                "client_id": client_id,
                "permisos": sorted(nuevo.permisos),
                "plan": nuevo.plan.value,
            },
        )
        return socio, CredencialesEmitidas(
            client_id=client_id, client_secret=secreto, api_key=api_key
        )

    def _compensar(self, socio_id: str, deshacer: list[Callable[[], None]]) -> None:
        for paso in reversed(deshacer):
            try:
                paso()
            except Exception as error:  # noqa: BLE001 - se registra y se sigue: no debe ocultar el error original
                logger.error(
                    "no se pudo compensar el alta",
                    extra={"socio_id": socio_id, "error_type": type(error).__name__},
                )

    @operacion("listar_socios")
    def listar(self) -> list[Socio]:
        return sorted(self.repositorio.listar(), key=lambda s: s.id)

    @operacion("consultar_socio")
    def obtener(self, socio_id: str) -> Socio:
        socio = self.repositorio.obtener(socio_id)
        if socio is None:
            raise SocioNoEncontrado(socio_id)
        return socio

    @operacion("desactivar_socio")
    def desactivar(self, socio_id: str) -> Socio:
        socio = self.obtener(socio_id)
        if not socio.activo:
            return socio
        self.identidad.eliminar_cliente(socio.client_id)
        self.api_keys.deshabilitar(socio.api_key_id)
        desactivado = socio.desactivar()
        self.repositorio.guardar(desactivado)
        logger.info("socio desactivado", extra={"socio_id": socio_id, "client_id": socio.client_id})
        return desactivado


class AutenticarSocio:
    def __init__(self, repositorio: RepositorioSocios) -> None:
        self.repositorio = repositorio

    def ejecutar(self, client_id: str | None, api_key_id: str | None) -> Socio | None:
        with operacion("autenticar_socio") as medicion:
            socio = self.repositorio.por_client_id(client_id) if client_id else None
            if socio is None or not socio.autentica(client_id or "", api_key_id):
                medicion.marcar(RESULTADO_RECHAZADO)
                motivo = (
                    "inexistente"
                    if socio is None
                    else ("desactivado" if not socio.activo else "api_key_ajena")
                )
                logger.warning(
                    "credencial de socio rechazada",
                    extra={"client_id": client_id, "motivo": motivo},
                )
                return None
            return socio
