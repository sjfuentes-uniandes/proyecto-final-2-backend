from adaptador_datos.adapters.outbound.proveedor_http import ProveedorHttp
from adaptador_datos.domain.consultas.modelos import Aliado


class DatosAbiertosHttp(ProveedorHttp):
    """Simulador de Datos Abiertos: registros públicos del Estado."""

    aliado = Aliado.DATOS_ABIERTOS
