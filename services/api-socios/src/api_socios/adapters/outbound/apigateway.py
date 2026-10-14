"""Las API keys se crean fuera de Terraform; destroy.sh las borra por la etiqueta Socio."""

from typing import Any

from api_socios.domain.socios import Plan


class ApiKeysApiGateway:
    def __init__(
        self, cliente: Any, ambiente: str, planes: dict[str, str], proyecto: str = "solventa"
    ) -> None:
        self.cliente = cliente
        self.ambiente = ambiente
        self.planes = planes
        self.proyecto = proyecto

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

    def asociar_plan(self, api_key_id: str, plan: Plan) -> None:
        try:
            plan_id = self.planes[plan.value]
        except KeyError as error:
            raise LookupError(f"El plan {plan.value} no está en PARTNERS_USAGE_PLANS") from error
        self.cliente.create_usage_plan_key(usagePlanId=plan_id, keyId=api_key_id, keyType="API_KEY")

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
