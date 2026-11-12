"""W21-B12 (N1): las verificaciones de H11 sobre la copia local de coberturas: la función pura y
`VerificacionesPg` con una conexión falsa."""

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg
import pytest
from pagos_parametricos.adapters.outbound.verificaciones_pg import VerificacionesPg
from pagos_parametricos.domain.descarte import interpretar
from pagos_parametricos.domain.parametrico.modelos import Cobertura, EstadoCobertura, Regla
from pagos_parametricos.domain.parametrico.verificacion import verificar

pytestmark = pytest.mark.anyio
DESDE = datetime(2026, 9, 1, 5, tzinfo=UTC)
HASTA = datetime(2027, 9, 1, 5, tzinfo=UTC)
OBSERVADO = datetime(2026, 11, 10, 14, tzinfo=UTC)
EVENTO = "bafc2b26-7c88-5ae3-b5ac-26fdf5427a09"


def _cobertura(
    numero: int = 1,
    estado: EstadoCobertura = EstadoCobertura.ACTIVA,
    umbral: str = "120",
    valor_pago: str = "1400000.00",
    desde: datetime = DESDE,
    hasta: datetime = HASTA,
) -> Cobertura:
    regla = Regla(
        "LLUVIA_ACUMULADA",
        "11001",
        Decimal(umbral),
        "mm",
        Decimal(valor_pago),
        "COP",
        "1",
        desde,
        hasta,
    )
    return Cobertura(UUID(int=numero), estado, regla, DESDE, numero=f"250-2026-{numero:06d}")


# --- La función pura -----------------------------------------------------------------------------
def test_con_una_cobertura_vigente_todo_es_si_y_trae_umbral_y_valor() -> None:
    assert verificar([_cobertura()], OBSERVADO, False) == {
        "polizaVigente": True,
        "coberturaActiva": True,
        "pagoPrevio": False,
        "umbral": "120",
        "valorIndemnizar": "1400000.00",
    }


def test_sin_cobertura_nada_esta_activo_y_no_hay_umbral_ni_valor() -> None:
    assert verificar([], OBSERVADO, False) == {
        "polizaVigente": False,
        "coberturaActiva": False,
        "pagoPrevio": False,
    }


def test_una_cobertura_activa_fuera_de_ventana_no_es_poliza_vigente() -> None:
    fuera = _cobertura(desde=DESDE - timedelta(days=400), hasta=DESDE)
    resultado = verificar([fuera], OBSERVADO, False)
    assert (resultado["polizaVigente"], resultado["coberturaActiva"]) == (False, True)
    assert resultado["umbral"] == "120"  # la cobertura activa más cercana


def test_una_cobertura_cancelada_no_esta_activa() -> None:
    resultado = verificar([_cobertura(estado=EstadoCobertura.CANCELADA)], OBSERVADO, False)
    assert (resultado["polizaVigente"], resultado["coberturaActiva"]) == (False, False)
    assert "umbral" not in resultado


def test_la_ventana_incluye_el_inicio_y_excluye_el_fin() -> None:
    assert verificar([_cobertura()], DESDE, False)["polizaVigente"] is True
    assert verificar([_cobertura()], HASTA, False)["polizaVigente"] is False


def test_con_varias_prefiere_la_vigente_y_es_determinista() -> None:
    vencida = _cobertura(1, umbral="50", desde=DESDE - timedelta(days=400), hasta=DESDE)
    vigente = _cobertura(2, umbral="130", valor_pago="900000")
    resultado = verificar([vigente, vencida], OBSERVADO, False)
    assert (resultado["umbral"], resultado["valorIndemnizar"]) == ("130", "900000.00")
    assert verificar([vencida, vigente], OBSERVADO, False) == resultado


def test_el_pago_previo_se_pasa_tal_cual() -> None:
    assert verificar([_cobertura()], OBSERVADO, True)["pagoPrevio"] is True


# --- El adaptador --------------------------------------------------------------------------------
class Cursor:
    def __init__(self, filas: list[tuple[Any, ...]]) -> None:
        self.filas = filas

    async def fetchone(self) -> tuple[Any, ...] | None:
        return self.filas[0] if self.filas else None

    async def fetchall(self) -> list[tuple[Any, ...]]:
        return self.filas


FILA = (
    UUID(int=1), "250-2026-000001", "ACTIVA", "LLUVIA_ACUMULADA", "11001", Decimal(120), "mm",
    Decimal("1400000.00"), "COP", "1", DESDE, HASTA, None, DESDE,
)  # fmt: skip


class Conexion:
    def __init__(
        self, candidatas: list[tuple[Any, ...]], pago_previo: bool, error: Exception | None = None
    ) -> None:
        self.candidatas = candidatas
        self.pago_previo = pago_previo
        self.error = error
        self.sentencias: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, sql: str, parametros: tuple[Any, ...]) -> Cursor:
        if self.error:
            raise self.error
        self.sentencias.append((sql, parametros))
        if "EXISTS" in sql:
            return Cursor([(self.pago_previo,)])
        return Cursor(self.candidatas)


class Pool:
    def __init__(self, conexion: Conexion) -> None:
        self.conexion = conexion

    @asynccontextmanager
    async def connection(self) -> Any:
        yield self.conexion


class Reloj:
    def ahora(self) -> datetime:
        return OBSERVADO


def _mensaje(payload: dict[str, Any] | None, **sobre: Any) -> Any:
    cuerpo = json.dumps(
        {"eventId": EVENTO, "type": "EventoParametricoObservado", "payload": payload, **sobre}
    )
    return interpretar("m-1", cuerpo, recibido_en=None, recepciones=5, fallo=None)


def _payload(**cambios: Any) -> dict[str, Any]:
    base = {
        "tipoEvento": "LLUVIA_ACUMULADA",
        "zona": "11001",
        "observadoEn": "2026-11-10T14:00:00Z",
    }
    return {**base, **cambios}


async def test_verifica_por_tipo_y_zona_del_mensaje() -> None:
    conexion = Conexion([FILA], pago_previo=False)
    verificacion = await VerificacionesPg(Pool(conexion), Reloj()).verificar(_mensaje(_payload()))  # type: ignore[arg-type]
    assert verificacion == {
        "polizaVigente": True,
        "coberturaActiva": True,
        "pagoPrevio": False,
        "umbral": "120",
        "valorIndemnizar": "1400000.00",
    }
    candidatas, pago = conexion.sentencias
    assert candidatas[1] == ("LLUVIA_ACUMULADA", "11001")
    assert pago[1] == (UUID(EVENTO),)


async def test_el_pago_previo_es_un_siniestro_ya_creado_para_el_evento() -> None:
    conexion = Conexion([FILA], pago_previo=True)
    verificacion = await VerificacionesPg(Pool(conexion), Reloj()).verificar(_mensaje(_payload()))  # type: ignore[arg-type]
    assert verificacion is not None and verificacion["pagoPrevio"] is True
    sql = conexion.sentencias[1][0]
    assert "siniestro_id IS NOT NULL" in sql


@pytest.mark.parametrize("referencia", ["polizaId", "numeroPoliza"])
async def test_con_una_poliza_nombrada_busca_esa_poliza_y_no_las_de_la_zona(
    referencia: str,
) -> None:
    valor = str(UUID(int=1)) if referencia == "polizaId" else "250-2026-000001"
    conexion = Conexion([FILA], pago_previo=False)
    await VerificacionesPg(Pool(conexion), Reloj()).verificar(
        _mensaje(_payload(**{referencia: valor}))
    )  # type: ignore[arg-type]
    sql, parametros = conexion.sentencias[0]
    assert ("poliza_id = %s" if referencia == "polizaId" else "numero = %s") in sql
    assert parametros == ((UUID(valor) if referencia == "polizaId" else valor),)


async def test_sin_el_instante_observado_usa_el_reloj() -> None:
    conexion = Conexion([FILA], pago_previo=False)
    mensaje = _mensaje({"tipoEvento": "LLUVIA_ACUMULADA", "zona": "11001"})
    verificacion = await VerificacionesPg(Pool(conexion), Reloj()).verificar(mensaje)  # type: ignore[arg-type]
    assert verificacion is not None and verificacion["polizaVigente"] is True


@pytest.mark.parametrize(
    "mensaje",
    [
        interpretar("m", "no es json", recibido_en=None, recepciones=5, fallo=None),
        interpretar("m", "[]", recibido_en=None, recepciones=5, fallo=None),
        interpretar(
            "m",
            json.dumps({"eventId": EVENTO, "payload": "x"}),
            recibido_en=None,
            recepciones=5,
            fallo=None,
        ),
        _mensaje({"zona": "11001"}),
        _mensaje({"tipoEvento": "LLUVIA_ACUMULADA"}),
        _mensaje({"tipoEvento": 7, "zona": ["x"]}),
    ],
)
async def test_si_el_mensaje_no_dice_de_que_evento_es_no_hay_nada_que_verificar(
    mensaje: Any,
) -> None:
    conexion = Conexion([FILA], pago_previo=False)
    assert await VerificacionesPg(Pool(conexion), Reloj()).verificar(mensaje) is None  # type: ignore[arg-type]
    assert conexion.sentencias == []


async def test_un_evento_id_que_no_es_uuid_no_consulta_pagos() -> None:
    cuerpo = json.dumps({"eventId": "no-uuid", "payload": _payload()})
    mensaje = interpretar("m", cuerpo, recibido_en=None, recepciones=5, fallo=None)
    conexion = Conexion([FILA], pago_previo=True)
    verificacion = await VerificacionesPg(Pool(conexion), Reloj()).verificar(mensaje)  # type: ignore[arg-type]
    assert verificacion is not None and verificacion["pagoPrevio"] is False
    assert len(conexion.sentencias) == 1


@pytest.mark.parametrize("error", [psycopg.OperationalError("password=hunter2"), OSError("red")])
async def test_si_la_base_falla_el_detalle_sale_sin_verificacion_y_sin_filtrar_la_causa(
    error: Exception, capturar_logs: Any
) -> None:
    salida = capturar_logs()
    conexion = Conexion([FILA], pago_previo=False, error=error)
    assert await VerificacionesPg(Pool(conexion), Reloj()).verificar(_mensaje(_payload())) is None  # type: ignore[arg-type]
    assert "hunter2" not in salida.getvalue() and "error_type" in salida.getvalue()


async def test_un_instante_sin_zona_horaria_o_ilegible_usa_el_reloj() -> None:
    for observado in ("2026-11-10T14:00:00", "ayer", 5):
        conexion = Conexion([FILA], pago_previo=False)
        mensaje = _mensaje(_payload(observadoEn=observado))
        verificacion = await VerificacionesPg(Pool(conexion), Reloj()).verificar(mensaje)  # type: ignore[arg-type]
        assert verificacion is not None and verificacion["polizaVigente"] is True


def test_los_decimales_de_la_base_salen_con_el_formato_del_contrato() -> None:
    """La base guarda `120.000` y `1400000.00`; el portal y el contrato dicen `120` y `1400000.00`."""
    cobertura = _cobertura(umbral="120.000", valor_pago="1400000")
    resultado = verificar([cobertura], OBSERVADO, False)
    assert (resultado["umbral"], resultado["valorIndemnizar"]) == ("120", "1400000.00")
    con_decimales = verificar(
        [_cobertura(umbral="119.500", valor_pago="900000.5")], OBSERVADO, False
    )
    assert (con_decimales["umbral"], con_decimales["valorIndemnizar"]) == ("119.5", "900000.50")
