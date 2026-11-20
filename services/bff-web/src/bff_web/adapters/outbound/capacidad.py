"""Capacidad de un servicio ECS sobre CloudWatch y Application Auto Scaling (HU-W08).

Solo lectura (infra/apps/iam.tf): GetMetricData, DescribeScalableTargets y
DescribeScalingActivities.
- Cotizaciones por minuto: SampleCount de OperationDuration con las dimensiones del EMF
  (Environment, Service, Operation; infra/apps/ecs.tf) y Operation = "cotizacion".
- Tareas activas: SampleCount de AWS/ECS CPUUtilization por minuto, como el tablero
  (infra/apps/dashboard.tf, «Tareas en ejecución por servicio»).
"""

from collections.abc import Mapping
from datetime import datetime
from typing import Any

MAX_PAGINAS = 20
PERIODO_S = 60
OPERACION = "cotizacion"
DIMENSION_ESCALABLE = "ecs:service:DesiredCount"


class RepositorioCapacidad:
    def __init__(
        self,
        cloudwatch: Any,
        autoscaling: Any,
        namespace: str,
        ambiente: str,
        cluster: str,
        servicio: str,
    ) -> None:
        self.cloudwatch = cloudwatch
        self.autoscaling = autoscaling
        self.namespace = namespace
        self.ambiente = ambiente
        self.cluster = cluster
        self.servicio = servicio

    @property
    def recurso(self) -> str:
        return f"service/{self.cluster}/{self.servicio}"

    def _consulta(
        self, identificador: str, namespace: str, metrica: str, dimensiones: Mapping[str, str]
    ) -> dict[str, Any]:
        return {
            "Id": identificador,
            "MetricStat": {
                "Metric": {
                    "Namespace": namespace,
                    "MetricName": metrica,
                    "Dimensions": [{"Name": k, "Value": v} for k, v in dimensiones.items()],
                },
                "Period": PERIODO_S,
                "Stat": "SampleCount",
            },
        }

    def metricas(
        self, desde: datetime, hasta: datetime
    ) -> tuple[dict[datetime, float], dict[datetime, float]]:
        """Cotizaciones y tareas por minuto en [desde, hasta), en una sola llamada."""
        parametros: dict[str, Any] = {
            "MetricDataQueries": [
                self._consulta(
                    "cotizaciones",
                    self.namespace,
                    "OperationDuration",
                    {
                        "Environment": self.ambiente,
                        "Service": self.servicio,
                        "Operation": OPERACION,
                    },
                ),
                self._consulta(
                    "tareas",
                    "AWS/ECS",
                    "CPUUtilization",
                    {"ClusterName": self.cluster, "ServiceName": self.servicio},
                ),
            ],
            "StartTime": desde,
            "EndTime": hasta,
            "ScanBy": "TimestampAscending",
        }
        series: dict[str, dict[datetime, float]] = {"cotizaciones": {}, "tareas": {}}
        for _ in range(MAX_PAGINAS):
            respuesta = self.cloudwatch.get_metric_data(**parametros)
            for resultado in respuesta.get("MetricDataResults", []):
                serie = series.get(resultado.get("Id", ""))
                if serie is not None:
                    serie.update(
                        zip(
                            resultado.get("Timestamps", []),
                            resultado.get("Values", []),
                            strict=False,
                        )
                    )
            if not respuesta.get("NextToken"):
                break
            parametros["NextToken"] = respuesta["NextToken"]
        return series["cotizaciones"], series["tareas"]

    def limites(self) -> tuple[int, int]:
        """Mínimo y máximo del scalable target. Sin target, LookupError."""
        respuesta = self.autoscaling.describe_scalable_targets(
            ServiceNamespace="ecs",
            ResourceIds=[self.recurso],
            ScalableDimension=DIMENSION_ESCALABLE,
        )
        objetivos = respuesta.get("ScalableTargets", [])
        if not objetivos:
            raise LookupError("el servicio no tiene objetivo de escalado")
        return int(objetivos[0]["MinCapacity"]), int(objetivos[0]["MaxCapacity"])

    def actividades(self, desde: datetime) -> list[Mapping[str, Any]]:
        """Actividades de escalado hasta la primera página que ya empieza antes de `desde`.

        AWS las lista de la más reciente a la más antigua; quien las usa no depende de ese
        orden (elige la más reciente dentro de la ventana), solo se ahorra páginas.
        """
        parametros: dict[str, Any] = {
            "ServiceNamespace": "ecs",
            "ResourceId": self.recurso,
            "ScalableDimension": DIMENSION_ESCALABLE,
        }
        actividades: list[Mapping[str, Any]] = []
        for _ in range(MAX_PAGINAS):
            respuesta = self.autoscaling.describe_scaling_activities(**parametros)
            pagina = respuesta.get("ScalingActivities", [])
            actividades.extend(pagina)
            if not respuesta.get("NextToken") or any(a["StartTime"] < desde for a in pagina):
                break
            parametros["NextToken"] = respuesta["NextToken"]
        return actividades


def crear_repositorio(
    region: str, namespace: str, ambiente: str, cluster: str, servicio: str
) -> RepositorioCapacidad:
    import boto3

    return RepositorioCapacidad(
        boto3.client("cloudwatch", region_name=region),
        boto3.client("application-autoscaling", region_name=region),
        namespace,
        ambiente,
        cluster,
        servicio,
    )
