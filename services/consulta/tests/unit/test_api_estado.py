"""API interna de estado y listado de pólizas (HU-W18) sin base: lectura en memoria."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from consulta.application.estado.casos import BuscarPolizas, ConsultarEstadoPoliza
from consulta.application.estado.puertos import FiltroPolizas, ResultadoBusqueda
from consulta.domain.polizas.estado import estado_de
from consulta.domain.polizas.modelos import CoberturaProyectada, PolizaProyectada, Vigencia
from consulta.main import crear_app
from fastapi.testclient import TestClient

AHORA = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)  # 25 de octubre en Bogotá
HOY = date(2026, 10, 25)
ASESOR = {"X-Actor-Id": "sub-1", "X-Actor-Rol": "ASESOR"}
CAMPOS_DE_CLIENTE = ("clienteId", "cliente_id", "documentoHuella", "firmaId")


class RelojFijo:
    def ahora(self) -> datetime:
        return AHORA


def _poliza(
    numero: str, desde: date, hasta: date, *, ramo: str = "VIDA", minutos: int = 0
) -> PolizaProyectada:
    return PolizaProyectada(
        poliza_id=uuid4(),
        cliente_id=uuid4(),
        numero=numero,
        estado="ACTIVA",
        producto_id="vida-deudor",
        producto_version="1.0.0",
        producto_nombre="Vida deudor",
        ramo=ramo,
        vigencia=Vigencia(desde, hasta),
        prima=Decimal("64638.10"),
        suma_asegurada=Decimal(50000000),
        moneda="COP",
        periodicidad="MENSUAL",
        coberturas=(CoberturaProyectada("MUERTE", "Muerte", True),),
        documento_huella="sha256:" + "a" * 64,
        firma_id="FIRMA-0001",
        emitida_en=AHORA - timedelta(minutes=minutos),
        proyectada_en=AHORA + timedelta(seconds=minutos),
    )


class LecturaEnMemoria:
    """Aplica el filtro como el adaptador de Postgres para probar el cableado de la API."""

    def __init__(self, *polizas: PolizaProyectada) -> None:
        self.polizas = polizas
        self.llamadas = 0
        self.filtro: FiltroPolizas | None = None

    async def por_id(self, poliza_id: UUID) -> PolizaProyectada | None:
        self.llamadas += 1
        return next((p for p in self.polizas if p.poliza_id == poliza_id), None)

    async def buscar(self, filtro: FiltroPolizas, pagina: int, tamano: int) -> ResultadoBusqueda:
        self.llamadas += 1
        self.filtro = filtro
        coincidentes = [
            p
            for p in self.polizas
            if (filtro.numero is None or p.numero == filtro.numero)
            and (filtro.ramo is None or p.ramo == filtro.ramo)
            and (filtro.estado is None or estado_de(p.vigencia, filtro.hoy, 30) is filtro.estado)
            and (filtro.vigencia_desde is None or p.vigencia.hasta >= filtro.vigencia_desde)
        ]
        coincidentes.sort(key=lambda p: (-p.emitida_en.timestamp(), str(p.poliza_id)))
        inicio = (pagina - 1) * tamano
        actualizado = max((p.proyectada_en for p in self.polizas), default=None)
        return ResultadoBusqueda(
            tuple(coincidentes[inicio : inicio + tamano]), len(coincidentes), actualizado
        )


def _cliente(*polizas: PolizaProyectada) -> tuple[TestClient, LecturaEnMemoria]:
    app = crear_app()
    lectura = LecturaEnMemoria(*polizas)
    app.state.consultar_estado_poliza = ConsultarEstadoPoliza(lectura, RelojFijo(), 30)
    app.state.buscar_polizas = BuscarPolizas(lectura, RelojFijo(), 30)
    return TestClient(app), lectura


# Vigente (fin lejano), por vencer (faltan 10 días), vencida y no iniciada respecto de HOY.
VIGENTE = _poliza("100-2026-000001", date(2026, 1, 1), date(2027, 6, 30), minutos=1)
POR_VENCER = _poliza("100-2026-000002", date(2025, 11, 5), date(2026, 11, 4), minutos=2)
VENCIDA = _poliza("100-2026-000003", date(2025, 1, 1), date(2025, 12, 31), minutos=3)
NO_INICIADA = _poliza("100-2026-000004", date(2026, 11, 1), date(2027, 10, 31), minutos=4)


@pytest.mark.parametrize(
    ("poliza", "estado"),
    [
        (VIGENTE, "VIGENTE"),
        (POR_VENCER, "POR_VENCER"),
        (VENCIDA, "VENCIDA"),
        (NO_INICIADA, "NO_INICIADA"),
    ],
)
def test_detalle_por_estado(poliza: PolizaProyectada, estado: str) -> None:
    cliente, _ = _cliente(poliza)

    r = cliente.get(f"/v1/polizas/{poliza.poliza_id}/estado", headers=ASESOR)

    assert r.status_code == 200
    cuerpo = r.json()
    assert cuerpo["estado"] == estado
    assert cuerpo["numero"] == poliza.numero
    assert cuerpo["fechaActualizacion"] == poliza.proyectada_en.isoformat().replace("+00:00", "Z")
    assert cuerpo["emitidaEn"] != cuerpo["fechaActualizacion"]
    assert cuerpo["producto"] == "Vida deudor"
    assert cuerpo["vigencia"] == {
        "desde": poliza.vigencia.desde.isoformat(),
        "hasta": poliza.vigencia.hasta.isoformat(),
    }
    assert Decimal(cuerpo["prima"]) == Decimal("64638.10")
    assert cuerpo["coberturas"] == [{"codigo": "MUERTE", "nombre": "Muerte", "obligatoria": True}]
    assert cuerpo["clienteId"] == str(poliza.cliente_id)
    assert set(cuerpo) == {
        "polizaId", "clienteId", "numero", "estado", "fechaActualizacion", "producto", "ramo",
        "vigencia", "prima", "sumaAsegurada", "moneda", "periodicidad", "emitidaEn", "coberturas",
    }  # fmt: skip


def test_detalle_inexistente_es_404_y_el_cuerpo_es_estable() -> None:
    cliente, _ = _cliente(VIGENTE)

    primero = cliente.get(f"/v1/polizas/{uuid4()}/estado", headers=ASESOR)
    segundo = cliente.get(f"/v1/polizas/{uuid4()}/estado", headers=ASESOR)

    assert primero.status_code == 404
    assert primero.json()["codigo"] == "POLIZA_NO_ENCONTRADA"
    assert set(primero.json()) == {"codigo", "mensaje", "correlationId", "detalles"}
    sin_traza = [
        {k: v for k, v in r.json().items() if k != "correlationId"} for r in (primero, segundo)
    ]
    assert sin_traza[0] == sin_traza[1]


@pytest.mark.parametrize("ruta", ["/v1/polizas", f"/v1/polizas/{uuid4()}/estado"])
def test_otro_rol_es_403_y_no_consulta_la_base(ruta: str) -> None:
    cliente, lectura = _cliente(VIGENTE)

    for rol in ("CLIENTE", "SUSCRIPTOR", "asesor"):
        r = cliente.get(ruta, headers={"X-Actor-Id": "sub-1", "X-Actor-Rol": rol})
        assert (r.status_code, r.json()["codigo"]) == (403, "ROL_NO_AUTORIZADO")
        assert VIGENTE.numero not in r.text
    assert lectura.llamadas == 0


@pytest.mark.parametrize("ruta", ["/v1/polizas", f"/v1/polizas/{uuid4()}/estado"])
@pytest.mark.parametrize("encabezados", [{}, {"X-Actor-Id": "sub-1"}, {"X-Actor-Rol": "ASESOR"}])
def test_sin_actor_es_401(ruta: str, encabezados: dict[str, str]) -> None:
    cliente, lectura = _cliente(VIGENTE)

    r = cliente.get(ruta, headers=encabezados)

    assert (r.status_code, r.json()["codigo"]) == (401, "ACTOR_REQUERIDO")
    assert lectura.llamadas == 0


@pytest.mark.parametrize(
    ("ruta", "campo"),
    [
        ("/v1/polizas/no-es-uuid/estado", "poliza_id"),
        ("/v1/polizas?numero=1-2-3", "numero"),
        ("/v1/polizas?numero=100-2026-00000A", "numero"),
        ("/v1/polizas?estado=ARCHIVADA", "estado"),
        ("/v1/polizas?vigenciaDesde=2026-13-45", "vigenciaDesde"),
        ("/v1/polizas?pagina=0", "pagina"),
        ("/v1/polizas?pagina=x", "pagina"),
        ("/v1/polizas?tamano=0", "tamano"),
        ("/v1/polizas?tamano=51", "tamano"),
        ("/v1/polizas?ramo=", "ramo"),
        (f"/v1/polizas?ramo={'R' * 81}", "ramo"),
    ],
)
def test_validaciones_son_422_sin_repetir_el_valor(ruta: str, campo: str) -> None:
    cliente, lectura = _cliente(VIGENTE)

    r = cliente.get(ruta, headers=ASESOR)

    assert (r.status_code, r.json()["codigo"]) == (422, "SOLICITUD_INVALIDA")
    assert r.json()["detalles"] == {"campos": [campo]}
    valor = ruta.split("=", 1)[-1] if "=" in ruta else ruta.split("/")[3]
    assert len(valor) < 4 or valor not in r.text
    assert lectura.llamadas == 0


def _numeros(r: object) -> list[str]:
    return [i["numero"] for i in r.json()["items"]]  # type: ignore[attr-defined]


def test_listado_sin_filtros_ordena_por_emision_descendente_y_resume() -> None:
    cliente, _ = _cliente(VIGENTE, POR_VENCER, VENCIDA, NO_INICIADA)

    r = cliente.get("/v1/polizas", headers=ASESOR)

    assert r.status_code == 200
    cuerpo = r.json()
    # `minutos` mayor = emitida antes.
    assert _numeros(r) == [
        "100-2026-000001",
        "100-2026-000002",
        "100-2026-000003",
        "100-2026-000004",
    ]
    assert (cuerpo["total"], cuerpo["pagina"], cuerpo["tamano"]) == (4, 1, 20)
    assert cuerpo["actualizadoEn"] == NO_INICIADA.proyectada_en.isoformat().replace("+00:00", "Z")
    assert set(cuerpo["items"][0]) == {
        "polizaId", "numero", "ramo", "producto", "prima", "moneda", "vigenciaHasta", "estado",
        "fechaActualizacion",
    }  # fmt: skip
    for campo in CAMPOS_DE_CLIENTE:
        assert campo not in r.text


@pytest.mark.parametrize(
    ("consulta", "esperadas"),
    [
        ("numero=100-2026-000003", ["100-2026-000003"]),
        ("ramo=AUTOS", ["100-2026-000009"]),
        ("estado=VIGENTE", ["100-2026-000001", "100-2026-000009"]),
        ("estado=POR_VENCER", ["100-2026-000002"]),
        ("estado=VENCIDA", ["100-2026-000003"]),
        ("estado=NO_INICIADA", ["100-2026-000004"]),
        (
            "vigenciaDesde=2026-11-04",
            ["100-2026-000001", "100-2026-000002", "100-2026-000004", "100-2026-000009"],
        ),
        ("vigenciaDesde=2026-11-05", ["100-2026-000001", "100-2026-000004", "100-2026-000009"]),
        ("estado=VIGENTE&ramo=VIDA", ["100-2026-000001"]),
    ],
)
def test_listado_con_cada_filtro(consulta: str, esperadas: list[str]) -> None:
    autos = _poliza(
        "100-2026-000009", date(2026, 1, 1), date(2027, 12, 31), ramo="AUTOS", minutos=9
    )
    cliente, lectura = _cliente(VIGENTE, POR_VENCER, VENCIDA, NO_INICIADA, autos)

    r = cliente.get(f"/v1/polizas?{consulta}", headers=ASESOR)

    assert r.status_code == 200
    assert sorted(_numeros(r)) == esperadas
    assert r.json()["total"] == len(esperadas)
    assert lectura.filtro is not None
    assert (lectura.filtro.hoy, lectura.filtro.dias_por_vencer) == (HOY, 30)


def test_paginacion_total_y_pagina_fuera_de_rango() -> None:
    polizas = [
        _poliza(f"100-2026-0000{i:02d}", date(2026, 1, 1), date(2027, 1, 1), minutos=i)
        for i in range(5)
    ]
    cliente, _ = _cliente(*polizas)

    p1 = cliente.get("/v1/polizas?pagina=1&tamano=2", headers=ASESOR)
    p3 = cliente.get("/v1/polizas?pagina=3&tamano=2", headers=ASESOR)
    p9 = cliente.get("/v1/polizas?pagina=9&tamano=2", headers=ASESOR)

    assert _numeros(p1) == ["100-2026-000000", "100-2026-000001"]
    assert _numeros(p3) == ["100-2026-000004"]
    assert p9.json()["items"] == []
    assert [r.json()["total"] for r in (p1, p3, p9)] == [5, 5, 5]
    assert (p3.json()["pagina"], p3.json()["tamano"]) == (3, 2)


def test_listado_vacio() -> None:
    cliente, _ = _cliente()

    r = cliente.get("/v1/polizas", headers=ASESOR)

    assert r.status_code == 200
    assert r.json() == {"items": [], "total": 0, "pagina": 1, "tamano": 20, "actualizadoEn": None}
