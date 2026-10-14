"""Las API keys se crean fuera de Terraform; destroy.sh las borra por la etiqueta Socio."""

import time
from collections.abc import Callable
from datetime import date
from typing import Any

from api_socios.domain.cuotas import LimitesPlan, Periodo, plan_de_uso, planes_disponibles
from api_socios.domain.socios import Plan

LIMITES_TTL_S = 300.0


class ApiKeysApiGateway:
    def __init__(
        self,
        cliente: Any,
        ambiente: str,
        planes: dict[str, str],
        proyecto: str = "solventa",
        reloj: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cliente = cliente
        self.ambiente = ambiente
        self.planes = planes
        self.proyecto = proyecto
        self.reloj = reloj
        self._limites: dict[Plan, tuple[float, LimitesPlan]] = {}

    def crear(self, socio_id: str, nombre: str) -> tuple[str, str]:
        respuesta = self.cliente.create_api_key(
            name=f"{self.proyecto}-{self.ambiente}-socio-{socio_id}",
            description=f"Consumo atribuido al socio {socio_id} ({nombre})"[:250],
            enabled=True,
            tags={
                "Project": self.proyecto,
                "Environment": self.ambiente,
                "Socio": socio_id,
                "ManagedBy": "api-socios",
            },
        )
        return respuesta["id"], respuesta["value"]

    def planes_disponibles(self) -> list[Plan]:
        return planes_disponibles(self.planes)

    def asociar_plan(self, api_key_id: str, plan: Plan) -> None:
        self.cliente.create_usage_plan_key(
            usagePlanId=plan_de_uso(self.planes, plan), keyId=api_key_id, keyType="API_KEY"
        )

    def desasociar_plan(self, api_key_id: str, plan: Plan) -> None:
        try:
            self.cliente.delete_usage_plan_key(
                usagePlanId=plan_de_uso(self.planes, plan), keyId=api_key_id
            )
        except self.cliente.exceptions.NotFoundException:
            pass

    def limites(self, plan: Plan) -> LimitesPlan:
        ahora = self.reloj()
        guardado = self._limites.get(plan)
        if guardado and guardado[0] > ahora:
            return guardado[1]
        datos = self.cliente.get_usage_plan(usagePlanId=plan_de_uso(self.planes, plan))
        cuota, limite = datos["quota"], datos.get("throttle", {})
        limites = LimitesPlan(
            plan=plan,
            limite_por_segundo=float(limite.get("rateLimit", 0)),
            rafaga=int(limite.get("burstLimit", 0)),
            cuota=int(cuota["limit"]),
            periodo=Periodo(cuota["period"]),
            desplazamiento=int(cuota.get("offset", 0)),
        )
        self._limites[plan] = (ahora + LIMITES_TTL_S, limites)
        return limites

    def uso(
        self, api_key_id: str, plan: Plan, desde: date, hasta: date
    ) -> dict[str, list[list[int]]]:
        respuesta = self.cliente.get_usage(
            usagePlanId=plan_de_uso(self.planes, plan),
            keyId=api_key_id,
            startDate=desde.isoformat(),
            endDate=hasta.isoformat(),
        )
        return respuesta.get("items") or {}

    def deshabilitar(self, api_key_id: str) -> None:
        try:
            self.cliente.update_api_key(
                apiKey=api_key_id,
                patchOperations=[{"op": "replace", "path": "/enabled", "value": "false"}],
            )
        except self.cliente.exceptions.NotFoundException:
            pass

    def eliminar(self, api_key_id: str) -> None:
        try:
            self.cliente.delete_api_key(apiKey=api_key_id)
        except self.cliente.exceptions.NotFoundException:
            pass


def crear_api_keys(region: str, ambiente: str, planes: dict[str, str]) -> ApiKeysApiGateway:
    import boto3

    return ApiKeysApiGateway(boto3.client("apigateway", region_name=region), ambiente, planes)
