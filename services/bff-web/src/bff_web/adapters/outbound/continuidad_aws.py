"""Fuentes de Operación › Continuidad y Conciliación (HU-W29), todas de solo lectura:

- CloudWatch Logs: el grupo de la cronología (CONTINUIDAD_EVENTOS), donde EventBridge guarda los
  eventos de ECS, RDS y AWS FIS (infra/platform/observability.tf).
- CloudWatch: RequestCount del ALB interno por zona (CONTINUIDAD_ALB).
- S3: hu-w29/informe.json, que publica experiments/hu-w29/publicar.sh (CONTINUIDAD_BUCKET, solo exp).
"""

import json
import re
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from bff_web.domain.continuidad import EstadoTarea

MAX_PAGINAS = 20
ESPERA_CONSULTA_S = 8.0
CLAVE_INFORME = "hu-w29/informe.json"
_ZONA = re.compile(r"[a-z]{2}(?:-[a-z]+)+-\d[a-z]")
# Último estado de cada tarea en la retención del grupo (14 días por omisión).
CONSULTA_TAREAS = """
fields @timestamp
| filter `detail-type` = "ECS Task State Change"
| stats latest(detail.availabilityZone) as zona, latest(detail.lastStatus) as estado,
        latest(detail.desiredStatus) as deseado, latest(detail.healthStatus) as salud
  by detail.taskArn
| limit 10000
"""
DIAS_TAREAS = 14


class ErrorConsulta(RuntimeError):
    """Logs Insights no terminó la consulta a tiempo o la canceló."""


class RepositorioEventos:
    def __init__(
        self,
        cliente: Any,
        grupo: str,
        ahora: Callable[[], float] = time.time,
        dormir: Callable[[float], None] = time.sleep,
    ) -> None:
        self.cliente = cliente
        self.grupo = grupo
        self.ahora = ahora
        self.dormir = dormir

    def tareas(self) -> list[EstadoTarea]:
        fin = int(self.ahora())
        consulta = self.cliente.start_query(
            logGroupName=self.grupo,
            startTime=fin - DIAS_TAREAS * 86400,
            endTime=fin,
            queryString=CONSULTA_TAREAS,
        )
        limite = self.ahora() + ESPERA_CONSULTA_S
        while True:
            respuesta = self.cliente.get_query_results(queryId=consulta["queryId"])
            if respuesta.get("status") == "Complete":
                break
            if (
                respuesta.get("status") in ("Failed", "Cancelled", "Timeout")
                or self.ahora() > limite
            ):
                raise ErrorConsulta(str(respuesta.get("status")))
            self.dormir(0.5)
        filas = [
            {c["field"]: c.get("value", "") for c in fila} for fila in respuesta.get("results", [])
        ]
        return [
            EstadoTarea(
                f.get("zona", ""),
                f.get("estado", ""),
                f.get("deseado", ""),
                f.get("salud") or "UNKNOWN",
            )
            for f in filas
        ]

    def eventos(self, desde: datetime, hasta: datetime) -> list[Mapping[str, Any]]:
        parametros: dict[str, Any] = {
            "logGroupName": self.grupo,
            "startTime": int(desde.timestamp() * 1000),
            "endTime": int(hasta.timestamp() * 1000),
        }
        eventos: list[Mapping[str, Any]] = []
        for _ in range(MAX_PAGINAS):
            respuesta = self.cliente.filter_log_events(**parametros)
            for item in respuesta.get("events", []):
                try:
                    eventos.append(json.loads(item["message"]))
                except (KeyError, json.JSONDecodeError):
                    continue
            if not respuesta.get("nextToken"):
                break
            parametros["nextToken"] = respuesta["nextToken"]
        return eventos


class RepositorioTrafico:
    def __init__(self, cliente: Any, alb: str) -> None:
        self.cliente = cliente
        self.alb = alb

    def por_zona(self, desde: datetime, hasta: datetime) -> dict[str, float]:
        if not self.alb:
            return {}
        busqueda = (
            "SEARCH('{AWS/ApplicationELB,AvailabilityZone,LoadBalancer} "
            f'MetricName="RequestCount" LoadBalancer="{self.alb}"\', \'Sum\', 60)'
        )
        respuesta = self.cliente.get_metric_data(
            MetricDataQueries=[{"Id": "trafico", "Expression": busqueda, "Period": 60}],
            StartTime=desde,
            EndTime=hasta,
        )
        trafico: dict[str, float] = {}
        for serie in respuesta.get("MetricDataResults", []):
            zona = _ZONA.search(str(serie.get("Label", "")))
            if zona:
                trafico[zona.group(0)] = trafico.get(zona.group(0), 0.0) + sum(
                    serie.get("Values", [])
                )
        return trafico


class RepositorioInformes:
    def __init__(self, cliente: Any, bucket: str) -> None:
        self.cliente = cliente
        self.bucket = bucket

    def ultimo(self) -> Mapping[str, Any] | None:
        if not self.bucket:
            return None
        try:
            objeto = self.cliente.get_object(Bucket=self.bucket, Key=CLAVE_INFORME)
        except self.cliente.exceptions.NoSuchKey:
            return None
        informe: Mapping[str, Any] = json.loads(objeto["Body"].read())
        return informe


def crear_repositorios(
    region: str, grupo: str, alb: str, bucket: str
) -> tuple[RepositorioEventos, RepositorioTrafico, RepositorioInformes]:
    import boto3

    return (
        RepositorioEventos(boto3.client("logs", region_name=region), grupo),
        RepositorioTrafico(boto3.client("cloudwatch", region_name=region), alb),
        RepositorioInformes(boto3.client("s3", region_name=region), bucket),
    )
