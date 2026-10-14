"""Publica una versión nueva del modelo de riesgo (04 W11-P04)."""

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from solventa_common.telemetry import RESULTADO_RECHAZADO, operacion

from catalogo.application.reglas.puertos import Reloj, RepositorioModelos
from catalogo.domain.reglas.errores import DefinicionInvalida, VersionExistente, VigenciaRetroactiva
from catalogo.domain.reglas.hash import hash_canonico
from catalogo.domain.reglas.modelo import ModeloPublicado
from catalogo.domain.reglas.validacion import validar_definicion

# Holgura para el reloj de quien publica "vigente desde ahora".
TOLERANCIA_RETROACTIVA = timedelta(seconds=5)


class PublicarModelo:
    def __init__(self, repo: RepositorioModelos, reloj: Reloj) -> None:
        self._repo = repo
        self._reloj = reloj

    async def ejecutar(
        self,
        version: str,
        producto_id: str,
        vigente_desde: datetime,
        definicion: Mapping[str, Any],
        actor: str,
    ) -> ModeloPublicado:
        with operacion("modelo_riesgo.publicar") as op:
            try:
                errores = validar_definicion(definicion)
                if errores:
                    raise DefinicionInvalida(errores)
                if vigente_desde < self._reloj.ahora() - TOLERANCIA_RETROACTIVA:
                    raise VigenciaRetroactiva()
                modelo = ModeloPublicado(
                    version=version,
                    producto_id=producto_id,
                    vigente_desde=vigente_desde,
                    definicion=definicion,
                    hash=hash_canonico(definicion),
                    publicado_por=actor,
                )
                await self._repo.guardar(modelo)
            except (DefinicionInvalida, VigenciaRetroactiva, VersionExistente):
                op.marcar(RESULTADO_RECHAZADO)
                raise
        return modelo
