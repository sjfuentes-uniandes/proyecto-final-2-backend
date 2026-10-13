from typing import Any

import pytest
from api_socios.adapters.outbound.apigateway import ApiKeysApiGateway
from api_socios.adapters.outbound.cognito import ClientesCognito
from api_socios.domain.socios import Permiso, Plan


class Grabadora:
    """Cliente boto3 falso que registra las llamadas."""

    class exceptions:
        class ResourceNotFoundException(Exception):
            pass

        class NotFoundException(Exception):
            pass

    def __init__(
        self, respuestas: dict[str, Any] | None = None, fallas: set[str] | None = None
    ) -> None:
        self.llamadas: list[tuple[str, dict[str, Any]]] = []
        self.respuestas = respuestas or {}
        self.fallas = fallas or set()

    def __getattr__(self, operacion: str) -> Any:
        def llamar(**kwargs: Any) -> Any:
            self.llamadas.append((operacion, kwargs))
            if operacion in self.fallas:
                raise (
                    self.exceptions.ResourceNotFoundException
                    if "user_pool" in operacion
                    else self.exceptions.NotFoundException
                )()
            return self.respuestas.get(operacion, {})

        return llamar


def test_cliente_cognito_client_credentials_con_permisos_seleccionados() -> None:
    cognito = Grabadora(
        {"create_user_pool_client": {"UserPoolClient": {"ClientId": "abc", "ClientSecret": "s"}}}
    )
    client_id, secreto = ClientesCognito(cognito, "us-east-1_p", "solventa").crear_cliente(
        "SOC-000007", frozenset({Permiso.CONSULTAR, Permiso.COTIZAR})
    )
    assert (client_id, secreto) == ("abc", "s")
    _, kwargs = cognito.llamadas[0]
    assert kwargs["UserPoolId"] == "us-east-1_p" and kwargs["ClientName"] == "socio-SOC-000007"
    assert kwargs["GenerateSecret"] is True and kwargs["AllowedOAuthFlowsUserPoolClient"] is True
    assert kwargs["AllowedOAuthFlows"] == ["client_credentials"]
    assert kwargs["AllowedOAuthScopes"] == ["solventa/consultar", "solventa/cotizar"]


def test_eliminar_cliente_inexistente_no_falla() -> None:
    cognito = Grabadora(fallas={"delete_user_pool_client"})
    ClientesCognito(cognito, "us-east-1_p", "solventa").eliminar_cliente("abc")
    assert cognito.llamadas == [
        ("delete_user_pool_client", {"UserPoolId": "us-east-1_p", "ClientId": "abc"})
    ]


def test_api_key_etiquetada_y_asociada_al_plan() -> None:
    api = Grabadora({"create_api_key": {"id": "k1", "value": "v1"}})
    keys = ApiKeysApiGateway(api, "int", {"basico": "plan-b", "estandar": "plan-e"})
    assert keys.crear("SOC-000007", "Fintech") == ("k1", "v1")
    keys.asociar_plan("k1", Plan.ESTANDAR)
    (_, crear), (_, plan) = api.llamadas
    assert crear["name"] == "solventa-int-socio-SOC-000007" and crear["enabled"] is True
    assert crear["tags"] == {
        "Project": "solventa",
        "Environment": "int",
        "Socio": "SOC-000007",
        "ManagedBy": "api-socios",
    }
    assert plan == {"usagePlanId": "plan-e", "keyId": "k1", "keyType": "API_KEY"}


def test_plan_sin_configurar() -> None:
    with pytest.raises(LookupError):
        ApiKeysApiGateway(Grabadora(), "int", {"basico": "plan-b"}).asociar_plan(
            "k1", Plan.ESTANDAR
        )


def test_deshabilitar_y_eliminar_son_tolerantes() -> None:
    api = Grabadora(fallas={"delete_api_key"})
    keys = ApiKeysApiGateway(api, "int", {})
    keys.deshabilitar("k1")
    keys.eliminar("k1")
    assert api.llamadas[0] == (
        "update_api_key",
        {
            "apiKey": "k1",
            "patchOperations": [{"op": "replace", "path": "/enabled", "value": "false"}],
        },
    )


def test_deshabilitar_api_key_inexistente_no_falla() -> None:
    api = Grabadora(fallas={"update_api_key"})
    ApiKeysApiGateway(api, "int", {}).deshabilitar("k-borrada")  # desactivar es idempotente
    assert [operacion for operacion, _ in api.llamadas] == ["update_api_key"]
