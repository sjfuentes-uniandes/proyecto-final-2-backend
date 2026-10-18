"""Alarmas del ambiente sobre CloudWatch (DescribeAlarms, DescribeAlarmHistory y GetMetricData).

Las APIs devuelven las alarmas de toda la cuenta y región: se filtra por el prefijo
del ambiente (ALERTAS_PREFIJO).
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
