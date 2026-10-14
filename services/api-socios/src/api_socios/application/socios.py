import logging
from collections.abc import Callable
from datetime import UTC, date, datetime, time
from typing import Protocol

from solventa_common.telemetry import RESULTADO_RECHAZADO, operacion

from api_socios.domain.cuotas import (
    Consumo,
    LimitesPlan,
    PlanNoDisponible,
    calcular_consumo,
    ventana,
)
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

    def planes_disponibles(self) -> list[Plan]: ...

    def asociar_plan(self, api_key_id: str, plan: Plan) -> None: ...

    def desasociar_plan(self, api_key_id: str, plan: Plan) -> None: ...

    def limites(self, plan: Plan) -> LimitesPlan: ...

    def uso(
        self, api_key_id: str, plan: Plan, desde: date, hasta: date
    ) -> dict[str, list[list[int]]]: ...

    def deshabilitar(self, api_key_id: str) -> None: ...

    def eliminar(self, api_key_id: str) -> None: ...


class MetricasSocios(Protocol):
    def limitadas(self, api_key_id: str, desde: datetime, hasta: datetime) -> int: ...


class AdministrarSocios:
    def __init__(
        self,
        repositorio: RepositorioSocios,
        identidad: ProveedorIdentidad,
        api_keys: GestorApiKeys,
        metricas: MetricasSocios | None = None,
    ) -> None:
        self.repositorio = repositorio
        self.identidad = identidad
        self.api_keys = api_keys
        self.metricas = metricas

    def _exigir_plan(self, plan: Plan) -> None:
        if plan not in self.api_keys.planes_disponibles():
            raise PlanNoDisponible(plan.value)

    @operacion("habilitar_socio")
    def habilitar(self, nuevo: NuevoSocio) -> tuple[Socio, CredencialesEmitidas]:
        """Si un paso falla, deshace los anteriores."""
        self._exigir_plan(nuevo.plan)
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
            self._compensar(socio_id, deshacer, "no se pudo compensar el alta")
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

    def _compensar(self, socio_id: str, deshacer: list[Callable[[], None]], mensaje: str) -> None:
        for paso in reversed(deshacer):
            try:
                paso()
            except Exception as error:  # noqa: BLE001 - se registra y se sigue: no debe ocultar el error original
                logger.error(
                    mensaje, extra={"socio_id": socio_id, "error_type": type(error).__name__}
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

    @operacion("cambiar_plan_socio")
    def cambiar_plan(self, socio_id: str, plan: Plan) -> Socio:
        """API Gateway admite una API key en un solo plan por stage: primero se quita del anterior."""
        socio = self.obtener(socio_id)
        actualizado = socio.con_plan(plan)
        if socio.plan is plan:
            return socio
        self._exigir_plan(plan)
        deshacer: list[Callable[[], None]] = []
        try:
            self.api_keys.desasociar_plan(socio.api_key_id, socio.plan)
            deshacer.append(lambda: self.api_keys.asociar_plan(socio.api_key_id, socio.plan))
            self.api_keys.asociar_plan(socio.api_key_id, plan)
            deshacer.append(lambda: self.api_keys.desasociar_plan(socio.api_key_id, plan))
            self.repositorio.guardar(actualizado)
        except Exception:
            self._compensar(socio_id, deshacer, "no se pudo compensar el cambio de plan")
            raise
        logger.info(
            "plan de socio cambiado",
            extra={"socio_id": socio_id, "plan_anterior": socio.plan.value, "plan": plan.value},
        )
        return actualizado

    @operacion("listar_planes")
    def planes(self) -> list[LimitesPlan]:
        return [self.api_keys.limites(plan) for plan in self.api_keys.planes_disponibles()]

    @operacion("consultar_consumo_socio")
    def consumo(self, socio_id: str, ahora: datetime | None = None) -> Consumo:
        socio = self.obtener(socio_id)
        momento = (ahora or datetime.now(UTC)).astimezone(UTC)
        limites = self.api_keys.limites(socio.plan)
        vigente = ventana(limites.periodo, momento.date(), limites.desplazamiento)
        registros = self.api_keys.uso(
            socio.api_key_id, socio.plan, vigente.inicio, min(vigente.fin, momento.date())
        )
        inicio = datetime.combine(vigente.inicio, time(), UTC)
        limitadas = self._limitadas(socio, inicio, momento)
        return calcular_consumo(limites, vigente, registros, socio.api_key_id, limitadas)

    def _limitadas(self, socio: Socio, desde: datetime, hasta: datetime) -> int | None:
        if self.metricas is None:
            return None
        try:
            return self.metricas.limitadas(socio.api_key_id, desde, hasta)
        except Exception as error:  # noqa: BLE001 - las métricas son informativas: el consumo sigue
            logger.warning(
                "no se pudieron leer las limitaciones del socio",
                extra={"socio_id": socio.id, "error_type": type(error).__name__},
            )
            return None


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
