"""Respuestas 429 por socio: filtro PartnerThrottled de infra/platform/observability.tf."""

from datetime import datetime
from typing import Any

ESPACIO = "Solventa/Socios"
METRICA = "PartnerThrottled"
UN_DIA_S = 86_400


class MetricasCloudWatch:
    def __init__(self, cliente: Any) -> None:
        self.cliente = cliente

    def limitadas(self, api_key_id: str, desde: datetime, hasta: datetime) -> int:
        respuesta = self.cliente.get_metric_statistics(
            Namespace=ESPACIO,
            MetricName=METRICA,
            Dimensions=[{"Name": "ApiKeyId", "Value": api_key_id}],
            StartTime=desde,
            EndTime=hasta,
            Period=UN_DIA_S,
            Statistics=["Sum"],
        )
        return int(sum(punto["Sum"] for punto in respuesta.get("Datapoints", [])))


def crear_metricas(region: str) -> MetricasCloudWatch:
    import boto3

    return MetricasCloudWatch(boto3.client("cloudwatch", region_name=region))
