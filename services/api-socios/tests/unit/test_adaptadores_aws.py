from datetime import UTC, date, datetime
from typing import Any

import pytest
from api_socios.adapters.outbound.apigateway import ApiKeysApiGateway
from api_socios.adapters.outbound.cloudwatch import MetricasCloudWatch
from api_socios.adapters.outbound.cognito import ClientesCognito
from api_socios.domain.cuotas import LimitesPlan, Periodo, PlanNoDisponible
from api_socios.domain.socios import Permiso, Plan

PLANES = {"basico": "plan-b", "estandar": "plan-e", "prueba": "plan-p"}


class Grabadora:
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
    keys = ApiKeysApiGateway(Grabadora(), "int", {"basico": "plan-b"})
    with pytest.raises(PlanNoDisponible):
        keys.asociar_plan("k1", Plan.ESTANDAR)
    with pytest.raises(LookupError):
        keys.desasociar_plan("k1", Plan.PRUEBA)
    assert keys.planes_disponibles() == [Plan.BASICO]
    assert keys.cliente.llamadas == []


def test_desasociar_la_api_key_del_plan_anterior() -> None:
    api = Grabadora(fallas={"delete_usage_plan_key"})
    keys = ApiKeysApiGateway(Grabadora(), "int", PLANES)
    keys.desasociar_plan("k1", Plan.BASICO)
    assert keys.cliente.llamadas == [
        ("delete_usage_plan_key", {"usagePlanId": "plan-b", "keyId": "k1"})
    ]
    ApiKeysApiGateway(api, "int", PLANES).desasociar_plan("k1", Plan.BASICO)  # ya no estaba


def test_limites_del_plan_de_uso_con_cache() -> None:
    api = Grabadora(
        {
            "get_usage_plan": {
                "id": "plan-p",
                "throttle": {"rateLimit": 1.0, "burstLimit": 2},
                "quota": {"limit": 5, "offset": 0, "period": "DAY"},
            }
        }
    )
    reloj = iter([0.0, 10.0, 301.0])
    keys = ApiKeysApiGateway(api, "int", PLANES, reloj=lambda: next(reloj))
    esperado = LimitesPlan(Plan.PRUEBA, 1.0, 2, 5, Periodo.DIA, 0)
    assert keys.limites(Plan.PRUEBA) == esperado
    assert keys.limites(Plan.PRUEBA) == esperado
    assert keys.limites(Plan.PRUEBA) == esperado
    assert api.llamadas == [("get_usage_plan", {"usagePlanId": "plan-p"})] * 2


def test_uso_solo_de_la_api_key_del_socio() -> None:
    items = {"k1": [[3, 2]]}
    api = Grabadora({"get_usage": {"usagePlanId": "plan-p", "items": items}})
    keys = ApiKeysApiGateway(api, "int", PLANES)
    assert keys.uso("k1", Plan.PRUEBA, date(2026, 10, 13), date(2026, 10, 13)) == items
    assert api.llamadas == [
        (
            "get_usage",
            {
                "usagePlanId": "plan-p",
                "keyId": "k1",
                "startDate": "2026-10-13",
                "endDate": "2026-10-13",
            },
        )
    ]
    sin_trafico = ApiKeysApiGateway(Grabadora({"get_usage": {"items": {}}}), "int", PLANES)
    assert sin_trafico.uso("k9", Plan.BASICO, date(2026, 10, 1), date(2026, 10, 2)) == {}


def test_limitadas_por_api_key_en_cloudwatch() -> None:
    cloudwatch = Grabadora({"get_metric_statistics": {"Datapoints": [{"Sum": 4.0}, {"Sum": 2.0}]}})
    desde, hasta = datetime(2026, 10, 13, tzinfo=UTC), datetime(2026, 10, 13, 15, tzinfo=UTC)
    assert MetricasCloudWatch(cloudwatch).limitadas("k1", desde, hasta) == 6
    _, kwargs = cloudwatch.llamadas[0]
    assert kwargs["Namespace"] == "Solventa/Socios" and kwargs["MetricName"] == "PartnerThrottled"
    assert kwargs["Dimensions"] == [{"Name": "ApiKeyId", "Value": "k1"}]
    assert (kwargs["StartTime"], kwargs["EndTime"], kwargs["Statistics"]) == (desde, hasta, ["Sum"])
    vacio = Grabadora({"get_metric_statistics": {"Datapoints": []}})
    assert MetricasCloudWatch(vacio).limitadas("k1", desde, hasta) == 0


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
