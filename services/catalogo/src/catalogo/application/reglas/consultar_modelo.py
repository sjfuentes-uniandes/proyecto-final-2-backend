"""Consulta del modelo vigente o de una versión cerrada (04 W11-P04)."""

from datetime import datetime

from solventa_common.telemetry import RESULTADO_RECHAZADO, operacion

from catalogo.application.reglas.puertos import RepositorioModelos
from catalogo.domain.reglas.errores import ModeloNoEncontrado
from catalogo.domain.reglas.modelo import ModeloPublicado


class ConsultarModelo:
    def __init__(self, repo: RepositorioModelos) -> None:
        self._repo = repo

    async def vigente(self, producto_id: str, en: datetime) -> ModeloPublicado:
        with operacion("modelo_riesgo.consultar") as op:
            modelo = self._encontrado(await self._repo.vigente(producto_id, en), op)
        return modelo

    async def por_version(self, version: str) -> ModeloPublicado:
        with operacion("modelo_riesgo.consultar") as op:
            modelo = self._encontrado(await self._repo.por_version(version), op)
        return modelo

    @staticmethod
    def _encontrado(modelo: ModeloPublicado | None, op: operacion) -> ModeloPublicado:
        if modelo is None:
            op.marcar(RESULTADO_RECHAZADO)
            raise ModeloNoEncontrado()
        return modelo
