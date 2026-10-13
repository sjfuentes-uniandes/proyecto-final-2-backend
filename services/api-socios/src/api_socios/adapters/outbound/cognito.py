"""Clientes OAuth client_credentials de socios en el pool de Cognito "socios"."""

from typing import Any

from api_socios.domain.socios import Permiso


class ClientesCognito:
    def __init__(self, cliente: Any, pool_id: str, servidor_recursos: str) -> None:
        self.cliente = cliente
        self.pool_id = pool_id
        self.servidor = servidor_recursos

    def crear_cliente(self, socio_id: str, permisos: frozenset[Permiso]) -> tuple[str, str]:
        # Misma configuración que los socios de Terraform (infra/platform/identity.tf).
        respuesta = self.cliente.create_user_pool_client(
            UserPoolId=self.pool_id,
            ClientName=f"socio-{socio_id}",
            GenerateSecret=True,
            AllowedOAuthFlows=["client_credentials"],
            AllowedOAuthFlowsUserPoolClient=True,
            AllowedOAuthScopes=[f"{self.servidor}/{p.value}" for p in sorted(permisos)],
            SupportedIdentityProviders=["COGNITO"],
            EnableTokenRevocation=True,
            AccessTokenValidity=60,
            TokenValidityUnits={"AccessToken": "minutes"},
        )
        datos = respuesta["UserPoolClient"]
        return datos["ClientId"], datos["ClientSecret"]

    def eliminar_cliente(self, client_id: str) -> None:
        try:
            self.cliente.delete_user_pool_client(UserPoolId=self.pool_id, ClientId=client_id)
        except self.cliente.exceptions.ResourceNotFoundException:
            pass  # ya eliminado: desactivar es idempotente


def crear_clientes(region: str, pool_id: str, servidor_recursos: str) -> ClientesCognito:
    import boto3

    return ClientesCognito(boto3.client("cognito-idp", region_name=region), pool_id, servidor_recursos)
