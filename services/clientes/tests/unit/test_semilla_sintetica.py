"""Semilla de los clientes sintéticos (DEF-01) con una conexión falsa; el SQL real y las decisiones
de la verificación los cubre tests/module/test_semilla_sintetica_postgres.py."""

import io
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, Self
from uuid import UUID

import httpx
import pytest
from clientes.adapters.outbound import postgres, semilla_sintetica
from clientes.adapters.outbound.semilla_sintetica import (
    CLIENTES_SINTETICOS,
    FECHA_NACIMIENTO,
    OTORGADO_EN,
    TODOS_LOS_ALCANCES,
    VENCIDO_VIGENTE_HASTA,
    VERIFICADO_EN,
    VIGENTE_HASTA,
    Situacion,
    sembrar,
)
from clientes.config import Settings
from clientes.domain.identidad.modelos import EstadoIdentidad
from clientes.main import crear_app
from fastapi.testclient import TestClient
from pydantic import ValidationError

POR_CODIGO = {c.codigo: c for c in CLIENTES_SINTETICOS}
SECRETO = "secreto-sintetico-de-prueba-0123456789"


class ConexionFalsa:
    """Registra las sentencias; cada INSERT de cliente "crea" una fila salvo que se indique."""

    def __init__(self, filas_por_cliente: int = 1) -> None:
        self.filas_por_cliente = filas_por_cliente
        self.sentencias: list[tuple[str, tuple[Any, ...]]] = []
        self.transacciones = 0
        self.rowcount = 0

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.transacciones += 1
        yield

    def execute(self, sql: str, parametros: tuple[Any, ...]) -> Self:
        self.sentencias.append((sql, parametros))
        self.rowcount = self.filas_por_cliente if "INTO cliente" in sql else 1
        return self

    def de(self, tabla: str) -> list[tuple[Any, ...]]:
        return [p for sql, p in self.sentencias if f"INTO {tabla} " in sql]


@pytest.fixture
def conexion(monkeypatch: pytest.MonkeyPatch) -> ConexionFalsa:
    falsa = ConexionFalsa()
    monkeypatch.setattr(semilla_sintetica.psycopg, "connect", lambda _url: falsa)
    return falsa


def test_son_los_27_casos_con_su_uuid_y_su_documento() -> None:
    assert [c.codigo for c in CLIENTES_SINTETICOS] == [f"C{n:02d}" for n in range(1, 28)]
    # UUID con NN hexadecimal y documento con NN decimal (01 §3.5).
    esperados = {
        "C01": ("00000000-0000-4000-8000-000000000001", "9000000001"),
        "C10": ("00000000-0000-4000-8000-00000000000a", "9000000010"),
        "C16": ("00000000-0000-4000-8000-000000000010", "9000000016"),
        "C19": ("00000000-0000-4000-8000-000000000013", "9000000019"),
        # HU-W06: casos de fallback.
        "C20": ("00000000-0000-4000-8000-000000000014", "9000000020"),
        "C27": ("00000000-0000-4000-8000-00000000001b", "9000000027"),
    }
    for codigo, (uuid, documento) in esperados.items():
        cliente = POR_CODIGO[codigo]
        assert (cliente.id, cliente.numero_documento) == (UUID(uuid), documento)
    assert POR_CODIGO["C10"].consentimiento_id == UUID("00000000-0000-4000-9000-00000000000a")
    assert POR_CODIGO["C07"].nombre_completo == "Cliente Prueba 07"
    assert len({c.id for c in CLIENTES_SINTETICOS}) == 27
    assert len({c.numero_documento for c in CLIENTES_SINTETICOS}) == 27


def test_identidad_y_consentimiento_de_cada_caso() -> None:
    identidades = {c.codigo: c.identidad for c in CLIENTES_SINTETICOS}
    assert identidades.pop("C11") is EstadoIdentidad.RECHAZADO
    assert identidades.pop("C12") is EstadoIdentidad.PENDIENTE
    assert set(identidades.values()) == {EstadoIdentidad.VERIFICADO}

    situaciones = {c.codigo: c.situacion for c in CLIENTES_SINTETICOS}
    assert situaciones.pop("C08") is Situacion.REVOCADO
    assert situaciones.pop("C09") is Situacion.VENCIDO
    assert situaciones.pop("C10") is Situacion.NINGUNO
    assert set(situaciones.values()) == {Situacion.VIGENTE}

    parciales = {c.codigo: c.alcances for c in CLIENTES_SINTETICOS}
    assert "FA_INGRESOS_AGREGADOS" not in parciales.pop("C02")
    assert "FA_HISTORIAL_PAGOS_12M" not in parciales.pop("C16")
    assert parciales.pop("C17") == ("DA_REGISTROS_PUBLICOS",)
    assert set(parciales.values()) == {TODOS_LOS_ALCANCES}


def test_sembrar_inserta_todo_en_una_transaccion(conexion: ConexionFalsa) -> None:
    assert sembrar("postgresql://falsa") == 27

    assert conexion.transacciones == 1
    clientes = {p[0]: p for p in conexion.de("cliente")}
    assert len(clientes) == 27
    assert clientes[POR_CODIGO["C01"].id][1:] == (
        "CC",
        "9000000001",
        "Cliente Prueba 01",
        FECHA_NACIMIENTO,
        "VERIFICADO",
        VERIFICADO_EN,
        "SEMILLA_SINTETICA",
        "SEMILLA-C01",
    )
    # RECHAZADO deja la traza del proveedor sin fecha; PENDIENTE no tiene ninguna.
    assert clientes[POR_CODIGO["C11"].id][5:] == (
        "RECHAZADO",
        None,
        "SEMILLA_SINTETICA",
        "SEMILLA-C11",
    )
    assert clientes[POR_CODIGO["C12"].id][5:] == ("PENDIENTE", None, None, None)

    consentimientos = {p[-1]: p for p in conexion.de("consentimiento")}
    assert len(consentimientos) == 26  # C10 no tiene ninguno
    assert POR_CODIGO["C10"].id not in consentimientos
    c01_c = consentimientos[POR_CODIGO["C01"].id]
    assert (c01_c[5], c01_c[7], c01_c[8]) == ("ACTIVO", VIGENTE_HASTA, None)
    c08 = consentimientos[POR_CODIGO["C08"].id]
    assert c08[5] == "REVOCADO" and c08[8] is not None
    c09 = consentimientos[POR_CODIGO["C09"].id]
    # ACTIVO ya vencido sin materializar: la verificación lo decide como VENCIDO.
    assert (c09[5], c09[7]) == ("ACTIVO", VENCIDO_VIGENTE_HASTA)
    assert VENCIDO_VIGENTE_HASTA < OTORGADO_EN


def test_sembrar_otra_vez_no_crea_nada(conexion: ConexionFalsa) -> None:
    conexion.filas_por_cliente = 0

    assert sembrar("postgresql://falsa") == 0


def test_la_semilla_solo_registra_el_conteo(
    conexion: ConexionFalsa, capturar_logs: Callable[..., io.StringIO]
) -> None:
    salida = capturar_logs()

    sembrar("postgresql://falsa")

    texto = salida.getvalue()
    assert '"clientes_creados": 27' in texto
    assert "9000000001" not in texto and "Cliente Prueba" not in texto


@pytest.mark.parametrize("valor", [None, "false"])
def test_por_defecto_y_en_false_no_siembra(
    monkeypatch: pytest.MonkeyPatch, valor: str | None
) -> None:
    monkeypatch.delenv("CLIENTES_SEMILLA_SINTETICA", raising=False)
    if valor is not None:
        monkeypatch.setenv("CLIENTES_SEMILLA_SINTETICA", valor)

    assert Settings().semilla_sintetica is False


@pytest.mark.parametrize(
    "nombre", ["CLIENTES_SEMILLA_SINTETICA", "SOLVENTA_CLIENTES_SEMILLA_SINTETICA"]
)
def test_se_activa_con_la_variable_de_la_infra(
    monkeypatch: pytest.MonkeyPatch, nombre: str
) -> None:
    monkeypatch.setenv(nombre, "true")
    monkeypatch.setenv("ENVIRONMENT", "int")
    monkeypatch.setenv("REGISTRO_TOKEN_SECRET", SECRETO)

    assert Settings().semilla_sintetica is True


@pytest.mark.parametrize("ambiente", ["prod", "stg", "pruebas"])
def test_fuera_de_int_y_local_no_arranca_con_la_semilla(
    monkeypatch: pytest.MonkeyPatch, ambiente: str
) -> None:
    monkeypatch.setenv("CLIENTES_SEMILLA_SINTETICA", "true")
    monkeypatch.setenv("ENVIRONMENT", ambiente)
    monkeypatch.setenv("REGISTRO_TOKEN_SECRET", SECRETO)

    with pytest.raises(ValidationError, match="CLIENTES_SEMILLA_SINTETICA"):
        Settings()


@pytest.mark.parametrize("activa", [True, False])
def test_el_arranque_siembra_despues_de_migrar_solo_con_la_bandera(
    monkeypatch: pytest.MonkeyPatch, pools: list[Any], activa: bool
) -> None:
    pasos: list[str] = []
    monkeypatch.setattr(postgres, "migrar", lambda _url: pasos.append("migrar"))
    monkeypatch.setattr(semilla_sintetica, "sembrar", lambda _url: pasos.append("sembrar"))
    settings = Settings(CLIENTES_SEMILLA_SINTETICA=activa)
    app = crear_app(settings, transport=httpx.MockTransport(lambda _: httpx.Response(200)))

    with TestClient(app):
        pass

    assert pasos == (["migrar", "sembrar"] if activa else ["migrar"])
