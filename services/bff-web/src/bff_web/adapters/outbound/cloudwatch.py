"""Alarmas del ambiente sobre CloudWatch (DescribeAlarms, DescribeAlarmHistory y GetMetricData).

Las APIs devuelven las alarmas de toda la cuenta y región: se filtra por el prefijo
del ambiente (ALERTAS_PREFIJO).

HU-W22 (H09, DS3-26): `MetricasParametricos` lee con **un** `GetMetricData` la profundidad de la cola
`parametricos-eventos`, la de su DLQ y las tareas activas de `pagos-parametricos`. Cualquier campo que
no se pueda leer (sin métrica, sin permiso o un error) es `None`: H09 lo muestra «no disponible» y
nunca falla por eso.
"""

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from bff_web.domain.alertas import ESTADO_ALARMA, Transicion

MAX_PAGINAS = 20
ESPACIO_SOCIOS = "Solventa/Socios"


class RepositorioAlarmas:
    def __init__(
        self, cliente: Any, ambiente: str, ahora: Callable[[], datetime] = lambda: datetime.now(UTC)
    ) -> None:
        self.cliente = cliente
        self.ambiente = ambiente
        self.ahora = ahora

    def abiertas(self, prefijo: str) -> list[Mapping[str, Any]]:
        parametros: dict[str, Any] = {
            "AlarmNamePrefix": prefijo,
            "StateValue": ESTADO_ALARMA,
            "AlarmTypes": ["MetricAlarm"],
        }
        alarmas: list[Mapping[str, Any]] = []
        for _ in range(MAX_PAGINAS):
            respuesta = self.cliente.describe_alarms(**parametros)
            alarmas.extend(respuesta.get("MetricAlarms", []))
            if not respuesta.get("NextToken"):
                break
            parametros["NextToken"] = respuesta["NextToken"]
        return alarmas

    def transiciones(self, prefijo: str, desde: datetime, hasta: datetime) -> list[Transicion]:
        parametros: dict[str, Any] = {
            "AlarmTypes": ["MetricAlarm"],
            "HistoryItemType": "StateUpdate",
            "StartDate": desde,
            "EndDate": hasta,
            "ScanBy": "TimestampAscending",
            "MaxRecords": 100,
        }
        transiciones: list[Transicion] = []
        for _ in range(MAX_PAGINAS):
            respuesta = self.cliente.describe_alarm_history(**parametros)
            for item in respuesta.get("AlarmHistoryItems", []):
                if not str(item.get("AlarmName", "")).startswith(prefijo):
                    continue
                datos = json.loads(item.get("HistoryData") or "{}")
                transiciones.append(
                    Transicion(
                        alarma=item["AlarmName"],
                        momento=item["Timestamp"],
                        anterior=(datos.get("oldState") or {}).get("stateValue", ""),
                        nuevo=(datos.get("newState") or {}).get("stateValue", ""),
                    )
                )
            if not respuesta.get("NextToken"):
                break
            parametros["NextToken"] = respuesta["NextToken"]
        return transiciones

    def socio_con_mayor_cuota(self) -> str | None:
        """Socio con el mayor PartnerQuotaUsage reciente (lo publica api-socios por socio)."""
        fin = self.ahora()
        consulta = (
            f'SELECT MAX(PartnerQuotaUsage) FROM SCHEMA("{ESPACIO_SOCIOS}", Environment, Partner) '
            f"WHERE Environment = '{self.ambiente}' GROUP BY Partner ORDER BY MAX() DESC LIMIT 1"
        )
        respuesta = self.cliente.get_metric_data(
            MetricDataQueries=[{"Id": "cuota", "Expression": consulta, "Period": 300}],
            StartTime=fin - timedelta(minutes=15),
            EndTime=fin,
        )
        resultados = [r for r in respuesta.get("MetricDataResults", []) if r.get("Values")]
        return str(resultados[0]["Label"]) if resultados else None


def crear_repositorio(region: str, ambiente: str) -> RepositorioAlarmas:
    import boto3

    return RepositorioAlarmas(boto3.client("cloudwatch", region_name=region), ambiente)


# --- HU-W22: cola y consumidores de H09 -------------------------------------------------------------
VENTANA_PARAMETRICOS = timedelta(minutes=10)
PERIODO_PARAMETRICOS_S = 60


def _ultimo(resultado: Mapping[str, Any]) -> int | None:
    """El último valor de una serie, como entero; `None` si no hay puntos."""
    puntos = list(zip(resultado.get("Timestamps", []), resultado.get("Values", []), strict=False))
    if not puntos:
        return None
    return round(float(max(puntos, key=lambda punto: punto[0])[1]))


class MetricasParametricos:
    """La cola `parametricos-eventos` y quienes la consumen (DS3-26, patrón de HU-W28)."""

    def __init__(
        self, cliente: Any, ahora: Callable[[], datetime] = lambda: datetime.now(UTC)
    ) -> None:
        self.cliente = cliente
        self.ahora = ahora

    def leer(
        self, cola: str, dlq: str, cluster: str | None, servicio: str | None
    ) -> dict[str, int | None]:
        """`profundidad` (mensajes visibles de la cola), `dlq` (los de su DLQ) y `tareas`
        (`RunningTaskCount` del servicio, si hay cluster y servicio). Lanza si CloudWatch falla."""
        consultas: list[dict[str, Any]] = [
            self._sqs("profundidad", cola),
            self._sqs("dlq", dlq),
        ]
        if cluster and servicio:
            consultas.append(
                {
                    "Id": "tareas",
                    "MetricStat": {
                        "Metric": {
                            "Namespace": "ECS/ContainerInsights",
                            "MetricName": "RunningTaskCount",
                            "Dimensions": [
                                {"Name": "ClusterName", "Value": cluster},
                                {"Name": "ServiceName", "Value": servicio},
                            ],
                        },
                        "Period": PERIODO_PARAMETRICOS_S,
                        "Stat": "Maximum",
                    },
                    "ReturnData": True,
                }
            )
        fin = self.ahora()
        respuesta = self.cliente.get_metric_data(
            MetricDataQueries=consultas, StartTime=fin - VENTANA_PARAMETRICOS, EndTime=fin
        )
        leidas = {r["Id"]: _ultimo(r) for r in respuesta.get("MetricDataResults", [])}
        return {
            "profundidad": leidas.get("profundidad"),
            "dlq": leidas.get("dlq"),
            "tareas": leidas.get("tareas"),
        }

    @staticmethod
    def _sqs(identificador: str, cola: str) -> dict[str, Any]:
        return {
            "Id": identificador,
            "MetricStat": {
                "Metric": {
                    "Namespace": "AWS/SQS",
                    "MetricName": "ApproximateNumberOfMessagesVisible",
                    "Dimensions": [{"Name": "QueueName", "Value": cola}],
                },
                "Period": PERIODO_PARAMETRICOS_S,
                "Stat": "Maximum",
            },
            "ReturnData": True,
        }


def crear_metricas_parametricos(region: str) -> MetricasParametricos:
    import boto3
    from botocore.config import Config

    # Sin reintentos y con corte corto: H09 no espera a una fuente secundaria.
    config = Config(retries={"max_attempts": 1}, connect_timeout=1, read_timeout=1)
    return MetricasParametricos(boto3.client("cloudwatch", region_name=region, config=config))
