"""Semilla de QA (DS3-07): qué inserta, qué sube y con qué garantías. Sin base ni S3."""

import asyncio
import base64
import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from siniestros.adapters.outbound import semilla_qa
from siniestros.adapters.outbound.semilla_qa import (
    cargar_semilla,
    clave_objeto,
    copia_de_semilla,
    filas_del_caso,
    objetos_a_subir,
    sembrar,
    sembrar_base,
    sembrar_con_reintentos,
    subir_objetos,
)

pytestmark = pytest.mark.anyio
AHORA = datetime(2026, 11, 5, 15, 0, tzinfo=UTC)
ANALISTA_QA = UUID("a0a0a0a0-0000-4000-8000-000000000001")
OTRO_ANALISTA = UUID("b0b0b0b0-0000-4000-8000-000000000009")
CONTRATO = Path(__file__).parents[5] / "contracts" / "datos-sinteticos" / "siniestros-r7.json"


def caso(clave: str) -> dict[str, Any]:
    return next(c for c in cargar_semilla()["casos"] if c["clave"] == clave)


# --- El archivo ------------------------------------------------------------------------------------
def test_la_semilla_empaquetada_es_la_del_contrato() -> None:
    assert cargar_semilla() == json.loads(CONTRATO.read_text(encoding="utf-8"))


def test_son_ocho_casos_y_nueve_objetos_con_su_hash() -> None:
    semilla = cargar_semilla()
    assert [c["clave"] for c in semilla["casos"]] == [
        "SIN-01",
        "SIN-02",
        "SIN-03",
        "EVA-01",
        "EVA-02",
        "AUS-01",
        "ALT-01",
        "VEN-01",
    ]
    assert len(semilla["objetos"]) == 9
    for nombre, objeto in semilla["objetos"].items():
        contenido = base64.b64decode(objeto["base64"])
        assert hashlib.sha256(contenido).hexdigest() == objeto["sha256"], nombre
        assert len(contenido) == objeto["tamanoBytes"], nombre


def test_los_codigos_de_la_semilla_estan_en_el_rango_reservado() -> None:
    for c in cargar_semilla()["casos"]:
        assert 1 <= int(c["codigo"].rsplit("-", 1)[1]) <= 999


# --- Filas -----------------------------------------------------------------------------------------
def test_un_reportado_no_tiene_analista_y_sus_instantes_son_relativos() -> None:
    sin = caso("SIN-01")
    fila = filas_del_caso(sin, cargar_semilla()["objetos"], AHORA, ANALISTA_QA)["siniestro"][0]
    reportado = AHORA - timedelta(minutes=360)
    assert fila[1:4] == ("88-2026-0431", "MOVIL", "REPORTADO")
    assert (fila[11], fila[12]) == (None, None)
    assert fila[10] == reportado
    assert fila[8] == reportado - timedelta(minutes=41)
    assert fila[13] == "semilla-qa"


def test_un_caso_en_evaluacion_queda_asignado_dos_horas_despues_del_reporte() -> None:
    fila = filas_del_caso(caso("EVA-01"), cargar_semilla()["objetos"], AHORA, ANALISTA_QA)[
        "siniestro"
    ][0]
    reportado = AHORA - timedelta(minutes=840)
    assert (fila[3], fila[11], fila[12]) == (
        "EN_EVALUACION",
        ANALISTA_QA,
        reportado + timedelta(minutes=120),
    )


def test_el_analista_de_qa_se_puede_reemplazar() -> None:
    filas = filas_del_caso(caso("EVA-01"), cargar_semilla()["objetos"], AHORA, OTRO_ANALISTA)
    assert filas["siniestro"][0][11] == OTRO_ANALISTA
    asignado = next(h for h in filas["historial"] if h[2] == "ASIGNADO")
    assert asignado[5] == str(OTRO_ANALISTA)
    assert asignado[7] == "Luis Mora"


def test_las_evidencias_apuntan_a_su_objeto_y_registran_el_hash_del_registrado() -> None:
    semilla = cargar_semilla()
    filas = filas_del_caso(caso("ALT-01"), semilla["objetos"], AHORA, ANALISTA_QA)
    primera = filas["evidencias"][0]
    assert primera[5] == clave_objeto("5e000000-0000-4000-8000-000007000000", primera[0])
    assert primera[6] == semilla["objetos"]["png-dano-1"]["sha256"]
    assert primera[9] == semilla["objetos"]["png-dano-1"]["tamanoBytes"]
    assert primera[10] == "image/png"


def test_los_soportes_llevan_su_tipo_y_las_fotos_no() -> None:
    filas = filas_del_caso(caso("EVA-01"), cargar_semilla()["objetos"], AHORA, ANALISTA_QA)
    assert [(f[3], f[4]) for f in filas["evidencias"]] == [
        ("FOTOGRAFIA", None),
        ("FOTOGRAFIA", None),
        ("VIDEO", None),
        ("SOPORTE", "FACTURA_COMPRA"),
        ("SOPORTE", "DOCUMENTO_IDENTIDAD"),
    ]


def test_el_historial_tiene_ids_estables_para_poder_repetir_la_semilla() -> None:
    uno = filas_del_caso(caso("EVA-01"), cargar_semilla()["objetos"], AHORA, ANALISTA_QA)
    otro = filas_del_caso(
        caso("EVA-01"), cargar_semilla()["objetos"], AHORA + timedelta(days=3), ANALISTA_QA
    )
    assert [h[0] for h in uno["historial"]] == [h[0] for h in otro["historial"]]
    assert len({h[0] for h in uno["historial"]}) == 3


def test_la_poliza_vencida_termina_antes_del_evento() -> None:
    copia = copia_de_semilla(caso("VEN-01")["poliza"], AHORA.date())
    assert copia.vigencia is not None
    assert copia.vigencia.hasta == AHORA.date() - timedelta(days=25)
    sana = copia_de_semilla(caso("EVA-01")["poliza"], AHORA.date())
    assert sana.vigencia is not None
    assert sana.vigencia.desde == AHORA.date() - timedelta(days=180)
    assert sana.vigencia.hasta > AHORA.date()
    assert sana.ramo == "Dispositivos"
    assert isinstance(sana.vigencia.desde, date)


# --- Objetos ---------------------------------------------------------------------------------------
def test_se_omite_el_ausente_y_el_alterado_lleva_otro_contenido() -> None:
    semilla = cargar_semilla()
    subidos = objetos_a_subir(semilla)
    por_clave = {clave: (objeto, contenido) for clave, objeto, contenido in subidos}
    ausente = caso("AUS-01")
    video = next(e for e in ausente["evidencias"] if e["nombreArchivo"].endswith(".mp4"))
    assert clave_objeto(ausente["id"], video["id"]) not in por_clave
    alterado = caso("ALT-01")["evidencias"][0]
    objeto, contenido = por_clave[clave_objeto(caso("ALT-01")["id"], alterado["id"])]
    registrado = semilla["objetos"][alterado["objetoRegistrado"]]
    assert hashlib.sha256(contenido).hexdigest() != registrado["sha256"]
    assert objeto["sha256"] == hashlib.sha256(contenido).hexdigest()


def test_todo_se_escribe_bajo_siniestros_y_nunca_en_tmp_ni_telemetria() -> None:
    claves = [clave for clave, _, _ in objetos_a_subir(cargar_semilla())]
    assert claves
    assert all(c.startswith("siniestros/") for c in claves)
    assert not any(c.startswith(("tmp/", "telemetria/")) for c in claves)
    assert len(set(claves)) == len(claves)


class S3Falso:
    def __init__(self) -> None:
        self.puestos: list[dict[str, Any]] = []

    def put_object(self, **argumentos: Any) -> None:
        self.puestos.append(argumentos)


def test_cada_put_object_lleva_checksum_y_el_cifrado_exigido() -> None:
    cliente = S3Falso()
    total = subir_objetos(cliente, "bucket-evidencias", "aws:kms", cargar_semilla())
    assert total == len(cliente.puestos) == len(objetos_a_subir(cargar_semilla()))
    for puesto in cliente.puestos:
        assert puesto["ServerSideEncryption"] == "aws:kms"
        assert puesto["Bucket"] == "bucket-evidencias"
        assert (
            puesto["ChecksumSHA256"]
            == base64.b64encode(hashlib.sha256(puesto["Body"]).digest()).decode()
        )
        assert puesto["ContentType"]


# --- Base ------------------------------------------------------------------------------------------
class ConexionFalsa:
    def __init__(self) -> None:
        self.sentencias: list[tuple[str, Any]] = []
        self.transacciones = 0

    async def execute(self, sql: str, parametros: Any = None) -> None:
        self.sentencias.append((sql, parametros))

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        self.transacciones += 1
        yield


class PoolFalso:
    def __init__(self) -> None:
        self.conexion = ConexionFalsa()

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[ConexionFalsa]:
        yield self.conexion


async def test_inserta_todo_en_una_transaccion_y_sin_pisar_lo_existente() -> None:
    pool = PoolFalso()
    casos = await sembrar_base(pool, cargar_semilla(), AHORA, ANALISTA_QA)  # type: ignore[arg-type]
    assert casos == 8
    assert pool.conexion.transacciones == 1
    sentencias = [s for s, _ in pool.conexion.sentencias]
    assert all("ON CONFLICT (id) DO NOTHING" in s for s in sentencias)
    assert sum(s.startswith("INSERT INTO siniestros") for s in sentencias) == 8
    assert sum(s.startswith("INSERT INTO evidencias") for s in sentencias) == 30
    assert sum(s.startswith("INSERT INTO historial_siniestro") for s in sentencias) == 8 * 2 + 5


async def test_sin_bucket_solo_siembra_la_base() -> None:
    pool, cliente = PoolFalso(), S3Falso()
    await sembrar(pool, None, None, "AES256", AHORA)  # type: ignore[arg-type]
    await sembrar(pool, cliente, None, "AES256", AHORA)  # type: ignore[arg-type]
    assert cliente.puestos == []
    assert pool.conexion.transacciones == 2


async def test_con_bucket_siembra_la_base_y_sube_los_archivos() -> None:
    pool, cliente = PoolFalso(), S3Falso()
    await sembrar(pool, cliente, "bucket", "AES256", AHORA, OTRO_ANALISTA)  # type: ignore[arg-type]
    assert len(cliente.puestos) == len(objetos_a_subir(cargar_semilla()))
    asignados = [p for s, p in pool.conexion.sentencias if s.startswith("INSERT INTO siniestros")]
    assert {p[11] for p in asignados} == {None, OTRO_ANALISTA}


class RelojFijo:
    def ahora(self) -> datetime:
        return AHORA


async def test_reintenta_hasta_que_el_bucket_exista(monkeypatch: pytest.MonkeyPatch) -> None:
    llamadas = []

    async def sembrar_falso(*argumentos: Any) -> None:
        llamadas.append(argumentos)
        if len(llamadas) < 3:
            raise RuntimeError("NoSuchBucket con datos")

    monkeypatch.setattr(semilla_qa, "sembrar", sembrar_falso)
    await sembrar_con_reintentos(
        asyncio.Event(), None, None, "b", "AES256", RelojFijo(), espera_s=0.01
    )  # type: ignore[arg-type]
    assert len(llamadas) == 3


async def test_se_detiene_si_la_app_se_apaga(monkeypatch: pytest.MonkeyPatch) -> None:
    llamadas = []

    async def sembrar_falso(*argumentos: Any) -> None:
        llamadas.append(1)
        raise RuntimeError("falla")

    monkeypatch.setattr(semilla_qa, "sembrar", sembrar_falso)
    parada = asyncio.Event()
    parada.set()
    await sembrar_con_reintentos(parada, None, None, "b", "AES256", RelojFijo(), espera_s=5)  # type: ignore[arg-type]
    assert llamadas == [1]


async def test_agotados_los_intentos_lo_registra_y_termina(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def siempre_falla(*argumentos: Any) -> None:
        raise RuntimeError("secreto")

    monkeypatch.setattr(semilla_qa, "sembrar", siempre_falla)
    await sembrar_con_reintentos(
        asyncio.Event(),
        None,
        None,
        "b",
        "AES256",
        RelojFijo(),
        intentos=2,
        espera_s=0.01,  # type: ignore[arg-type]
    )
    mensajes = [r.getMessage() for r in caplog.records]
    assert mensajes.count("semilla de QA: no se pudo aplicar") == 2
    assert "semilla de QA: se agotaron los intentos" in mensajes
    assert "secreto" not in " ".join(mensajes)
