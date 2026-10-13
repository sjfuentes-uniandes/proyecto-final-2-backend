from adaptador_datos.adapters.outbound.proveedor_http import ProveedorHttp
from adaptador_datos.domain.consultas.modelos import Aliado


class OpenFinanceHttp(ProveedorHttp):
    """Simulador de Open Finance: productos, historial de pagos e ingresos."""

    aliado = Aliado.OPEN_FINANCE
