"""Llamadas HTTP de cotizacion a otros servicios con el presupuesto del recorrido (HU-W05).

`llamar_protegido` ejecuta la solicitud dentro de una `resiliencia.Proteccion` con el restante del
presupuesto como deadline, envía su timeout efectivo en X-Deadline-Ms y traduce el resultado a los
errores del dominio. Así catalogo y clientes cortan cuando cotizacion deja de esperar.
"""

import httpx
from solventa_common import presupuesto, resiliencia

from cotizacion.domain.perfilamiento.errores import DependenciaAgotada, DependenciaNoDisponible


def _reintentable(respuesta: httpx.Response) -> httpx.Response:
    """Un 5xx se lanza para que la protección lo reintente; el 504 no (es un corte de tiempo)."""
    if respuesta.status_code >= 500 and respuesta.status_code != 504:
        respuesta.raise_for_status()
    return respuesta


async def llamar_protegido(
    cliente: httpx.AsyncClient,
    proteccion: resiliencia.Proteccion,
    url: str,
    parametros: dict[str, str] | None,
    *,
    clave: str,
    grupo: str,
    timeout_ms: int,
    dependencia: str,
    metodo: str = "GET",
    cuerpo: object = None,
) -> httpx.Response:
    """La solicitud con el restante del recorrido como deadline (HU-W05).

    Traduce a dominio: corte → DependenciaAgotada; falla, 5xx, circuito o bulkhead →
    DependenciaNoDisponible. El resto de respuestas se devuelve para que el llamador decida.
    """

    async def llamada(timeout_s: float) -> httpx.Response:
        return _reintentable(
            await cliente.request(
                metodo,
                url,
                params=parametros,
                json=cuerpo,
                timeout=timeout_s,
                headers=presupuesto.encabezado(timeout_s * 1000),
            )
        )

    try:
        respuesta = await proteccion.ejecutar(
            clave, grupo, llamada, timeout_ms, presupuesto.restante_ms()
        )
    except resiliencia.TiempoAgotado as error:
        raise DependenciaAgotada(dependencia) from error
    except (resiliencia.DependenciaNoDisponible, httpx.HTTPError) as error:
        raise DependenciaNoDisponible(dependencia) from error
    if respuesta.status_code == 504:
        # El servicio llamado cortó con nuestro X-Deadline-Ms: también es un timeout.
        resiliencia.registrar_tiempo_agotado(grupo, clave, timeout_ms, presupuesto.restante_ms())
        raise DependenciaAgotada(dependencia)
    return respuesta
