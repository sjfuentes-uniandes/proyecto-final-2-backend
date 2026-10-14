"""GET /socio/me no aparece: solo exige autenticación."""

from api_socios.domain.socios import Permiso

PERMISO_POR_OPERACION: dict[tuple[str, str], Permiso] = {
    ("POST", "/cotizaciones"): Permiso.COTIZAR,
    ("GET", "/cotizaciones/{cotizacion_id}"): Permiso.COTIZAR,
    ("POST", "/polizas"): Permiso.EMITIR,
    ("GET", "/polizas/{poliza_id}"): Permiso.CONSULTAR,
    ("POST", "/siniestros"): Permiso.SINIESTROS,
}


def permiso_requerido(metodo: str, ruta: str) -> Permiso:
    try:
        return PERMISO_POR_OPERACION[(metodo.upper(), ruta)]
    except KeyError as error:
        raise LookupError(f"Operación sin permiso asignado: {metodo} {ruta}") from error
