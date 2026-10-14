"""Ejecutor de las consultas a las fuentes autorizadas (fase B de crear_perfil)."""

from collections.abc import Sequence

from cotizacion.application.perfilamiento.puertos import PuertoFuente
from cotizacion.domain.perfilamiento.modelos import (
    ContextoConsulta,
    ResultadoFuente,
    SolicitudFuente,
)


class EjecutorSecuencial:
    """Provisional: W12-P02 lo reemplaza por EjecutorParalelo.

    Consulta las fuentes una tras otra, en el orden de las solicitudes. No agrega timeouts ni
    telemetría: cada llamada la acota el timeout HTTP de AdaptadorDatosHttp y PuertoFuente no
    lanza por errores del proveedor (DT-28).
    """

    def __init__(self, puerto: PuertoFuente) -> None:
        self._puerto = puerto

    async def ejecutar(
        self, solicitudes: Sequence[SolicitudFuente], contexto: ContextoConsulta
    ) -> list[ResultadoFuente]:
        return [await self._puerto.consultar(s, contexto) for s in solicitudes]
