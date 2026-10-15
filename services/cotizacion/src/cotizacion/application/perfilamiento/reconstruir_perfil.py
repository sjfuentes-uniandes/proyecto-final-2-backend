"""Reconstrucción del resultado de un perfil (W11-P11, AC2 y regla 13).

Vuelve a evaluar los insumos guardados con la versión exacta del modelo y compara con el
resultado persistido. El hash verifica que la versión no cambió desde el cálculo.
"""

from uuid import UUID

from solventa_common.telemetry import operacion

from cotizacion.application.perfilamiento.puertos import PuertoModeloRiesgo, RepositorioPerfiles
from cotizacion.domain.perfilamiento.errores import PerfilNoEncontrado
from cotizacion.domain.perfilamiento.motor import FuenteEvaluada, evaluar


class ReconstruirPerfil:
    def __init__(self, repo: RepositorioPerfiles, modelos: PuertoModeloRiesgo) -> None:
        self._repo = repo
        self._modelos = modelos

    async def ejecutar(self, perfil_id: UUID) -> tuple[bool, str, str]:
        """(coincide, versión del modelo, hash del modelo) del perfil."""
        with operacion("perfilamiento.reconstruccion"):
            perfil = await self._repo.por_id(perfil_id)
            if perfil is None:
                raise PerfilNoEncontrado()
            modelo = await self._modelos.por_version(perfil.version_modelo)
            coincide = modelo.hash == perfil.hash_modelo
            if coincide:
                fuentes = {r.fuente: FuenteEvaluada(r.estado, r.confianza) for r in perfil.fuentes}
                coincide = evaluar(modelo, perfil.insumos, fuentes) == perfil.resultado
            respuesta = (coincide, perfil.version_modelo, perfil.hash_modelo)
        return respuesta
