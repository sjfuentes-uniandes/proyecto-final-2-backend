"""Métricas de socios en Solventa/Socios.

- PartnerThrottled: respuestas 429 por socio (filtro de infra/platform/observability.tf).
- PartnerQuotaUsage: porcentaje de la cuota consumido por socio (HU-W28, alarmas de cuota).
"""

from datetime import datetime
from typing import Any

ESPACIO = "Solventa/Socios"
METRICA = "PartnerThrottled"
METRICA_CUOTA = "PartnerQuotaUsage"
UN_DIA_S = 86_400
LOTE_PUT_METRIC_DATA = 1000


class MetricasCloudWatch:
    def __init__(self, cliente: Any, ambiente: str = "local") -> None:
        self.cliente = cliente
        self.ambiente = ambiente

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

    def publicar_cuotas(self, porcentajes: dict[str, int]) -> None:
        datos = [
            {
                "MetricName": METRICA_CUOTA,
                "Dimensions": [
                    {"Name": "Environment", "Value": self.ambiente},
                    {"Name": "Partner", "Value": socio_id},
                ],
                "Value": float(porcentaje),
                "Unit": "Percent",
            }
            for socio_id, porcentaje in sorted(porcentajes.items())
        ]
        for i in range(0, len(datos), LOTE_PUT_METRIC_DATA):
            self.cliente.put_metric_data(
                Namespace=ESPACIO, MetricData=datos[i : i + LOTE_PUT_METRIC_DATA]
            )


def crear_metricas(region: str, ambiente: str = "local") -> MetricasCloudWatch:
    import boto3

    return MetricasCloudWatch(boto3.client("cloudwatch", region_name=region), ambiente)
