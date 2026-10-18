"""Casos de uso del consentimiento: otorgar, consultar, listar y texto vigente (HU-M08); revocar
y consultar el historial (HU-M09)."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from uuid import UUID, uuid4

from clientes.application.consentimientos.puertos import (
    CatalogoTextos,
    Otorgamiento,
    RepositorioConsentimientos,
    Revocacion,
)
from clientes.application.identidad.casos import ConsultarCliente
from clientes.domain.consentimientos.modelos import (
    IDIOMAS,
    PROPOSITOS,
    ClaveIdempotenciaEnUso,
    Consentimiento,
    ConsentimientoActivoExistente,
    ConsentimientoNoEncontrado,
    ConsentimientoYaVigente,
    IdempotenciaConflicto,
    IdentidadNoVerificada,
    RegistroIdempotencia,
    SolicitudInvalida,
    SolicitudOtorgamiento,
    TextoNoVigente,
)
from clientes.domain.consentimientos.textos import TextoLegal
from clientes.domain.identidad.modelos import ClienteNoEncontrado


@dataclass(frozen=True, slots=True)
class ResultadoOtorgar:
    consentimiento: Consentimiento
    repeticion: bool


class OtorgarConsentimiento:
    def __init__(
        self,
        repositorio: RepositorioConsentimientos,
        clientes: ConsultarCliente,
        textos: CatalogoTextos,
        nuevo_id: Callable[[], UUID] = uuid4,
    ) -> None:
        self._repositorio = repositorio
        self._clientes = clientes
        self._textos = textos
        self._nuevo_id = nuevo_id

    async def ejecutar(
        self,
        sub: str,
        clave: UUID,
        *,
        proposito: str | None,
        alcances: Sequence[str] | None,
        vigencia_meses: int | None,
        version_texto: str | None,
        idioma: str | None,
        correlation_id: str,
    ) -> ResultadoOtorgar:
        """Orden de evaluación de la ADR 0009: validación, clave de idempotencia, identidad
        `VERIFICADO`, `ACTIVO` vigente y, por último, crear. La repetición se resuelve antes del
        conflicto, de modo que reenviar la misma confirmación nunca produce un `409`."""
        solicitud = SolicitudOtorgamiento.desde(
            proposito=proposito,
            alcances=alcances,
            vigencia_meses=vigencia_meses,
            version_texto=version_texto,
            idioma=idioma,
        )
        if self._textos.vigente(solicitud.proposito, solicitud.idioma).version != (
            solicitud.version_texto
        ):
            raise TextoNoVigente()
        cliente = await self._clientes.por_sub(sub)
        huella = solicitud.huella()
        previo = await self._repositorio.por_clave(cliente.id, clave)
        if previo is not None:
            return _repeticion(previo, huella)
        if not cliente.verificado:
            raise IdentidadNoVerificada()
        activo = await self._repositorio.activo(cliente.id, solicitud.proposito)
        if activo is not None and activo.vigente:
            return await self._conflicto_o_repeticion(cliente.id, clave, huella, activo)
        try:
            creado = await self._repositorio.crear(
                Otorgamiento(
                    id=self._nuevo_id(),
                    cliente_id=cliente.id,
                    solicitud=solicitud,
                    clave=clave,
                    huella=huella,
                    actor_id=sub,
                    correlation_id=correlation_id,
                )
            )
        except (ConsentimientoActivoExistente, ClaveIdempotenciaEnUso) as error:
            return await self._tras_carrera(cliente.id, clave, solicitud, huella, error)
        return ResultadoOtorgar(creado, repeticion=False)

    async def _conflicto_o_repeticion(
        self, cliente_id: UUID, clave: UUID, huella: str, activo: Consentimiento
    ) -> ResultadoOtorgar:
        """Una solicitud con la misma clave pudo registrarse entre la lectura de la clave y la
        del `ACTIVO`: se relee la clave para que la repetición gane siempre al 409."""
        previo = await self._repositorio.por_clave(cliente_id, clave)
        if previo is not None:
            return _repeticion(previo, huella)
        raise ConsentimientoYaVigente(activo.id)

    async def _tras_carrera(
        self,
        cliente_id: UUID,
        clave: UUID,
        solicitud: SolicitudOtorgamiento,
        huella: str,
        error: Exception,
    ) -> ResultadoOtorgar:
        """Otra transacción ganó entre la lectura y la escritura: se relee y se resuelve como si
        hubiera llegado después."""
        previo = await self._repositorio.por_clave(cliente_id, clave)
        if previo is not None:
            return _repeticion(previo, huella)
        activo = await self._repositorio.activo(cliente_id, solicitud.proposito)
        if activo is not None and activo.vigente:
            raise ConsentimientoYaVigente(activo.id)
        raise error


def _repeticion(previo: RegistroIdempotencia, huella: str) -> ResultadoOtorgar:
    if previo.huella != huella:
        raise IdempotenciaConflicto()
    return ResultadoOtorgar(previo.consentimiento, repeticion=True)


class ConsultarConsentimientos:
    def __init__(self, repositorio: RepositorioConsentimientos, clientes: ConsultarCliente) -> None:
        self._repositorio = repositorio
        self._clientes = clientes

    async def detalle(self, sub: str, consentimiento_id: UUID) -> Consentimiento:
        """Un consentimiento ajeno es indistinguible de uno inexistente."""
        try:
            cliente = await self._clientes.por_sub(sub)
        except ClienteNoEncontrado as error:
            raise ConsentimientoNoEncontrado() from error
        consentimiento = await self._repositorio.obtener(cliente.id, consentimiento_id)
        if consentimiento is None:
            raise ConsentimientoNoEncontrado()
        return consentimiento

    async def listar(self, sub: str) -> list[Consentimiento]:
        """Quien aún no tiene cliente no tiene consentimientos: lista vacía, no error."""
        try:
            cliente = await self._clientes.por_sub(sub)
        except ClienteNoEncontrado:
            return []
        return await self._repositorio.listar(cliente.id)


class RevocarConsentimiento:
    def __init__(self, repositorio: RepositorioConsentimientos, clientes: ConsultarCliente) -> None:
        self._repositorio = repositorio
        self._clientes = clientes

    async def ejecutar(
        self, sub: str, consentimiento_id: UUID, *, correlation_id: str
    ) -> Consentimiento:
        """Idempotente por estado (AC3, ADR 0009): la primera vez revoca y escribe el evento; si
        ya está `REVOCADO` o `VENCIDO`, devuelve el mismo estado sin efectos. No exige identidad
        verificada: retirar el permiso siempre es posible. Uno ajeno es como uno inexistente."""
        try:
            cliente = await self._clientes.por_sub(sub)
        except ClienteNoEncontrado as error:
            raise ConsentimientoNoEncontrado() from error
        consentimiento = await self._repositorio.revocar(
            Revocacion(
                consentimiento_id=consentimiento_id,
                cliente_id=cliente.id,
                actor_id=sub,
                correlation_id=correlation_id,
            )
        )
        if consentimiento is None:
            raise ConsentimientoNoEncontrado()
        return consentimiento


class ConsultarTextoVigente:
    def __init__(self, textos: CatalogoTextos) -> None:
        self._textos = textos

    def ejecutar(self, proposito: str | None, idioma: str | None) -> TextoLegal:
        proposito_n = (proposito or "").strip().upper()
        idioma_n = (idioma or "").strip()
        campos = [
            nombre
            for nombre, valido in (
                ("proposito", proposito_n in PROPOSITOS),
                ("idioma", idioma_n in IDIOMAS),
            )
            if not valido
        ]
        if campos:
            raise SolicitudInvalida(campos)
        return self._textos.vigente(proposito_n, idioma_n)
