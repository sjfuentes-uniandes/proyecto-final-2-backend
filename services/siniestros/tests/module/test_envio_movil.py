import asyncio
import base64
import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from importlib import resources
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from siniestros.adapters.outbound import outbox_pg
from siniestros.adapters.outbound.envios_pg import EnviosPg
from siniestros.adapters.outbound.siniestros_pg import SiniestrosPg
from siniestros.application.siniestro.borradores import Borradores
from siniestros.application.siniestro.enlace import EmitirEnlaceCliente
from siniestros.application.siniestro.envio import EnviarBorrador
from siniestros.application.siniestro.errores import (
    EvidenciaNoCargada,
    EvidenciaNoDisponible,
    IntegridadEvidenciaInvalida,
)
from siniestros.application.siniestro.tomar import TomarSiguiente
from siniestros.application.siniestro.vistas import Actor, Plazos
from siniestros.config import Settings
from siniestros.domain.siniestro.modelos import IntegridadEvidencia
from siniestros.main import crear_app

pytestmark = pytest.mark.anyio
ACTOR = str(uuid4())
CLIENTE = uuid4()
POLIZA = uuid4()
AHORA = datetime(2026, 11, 18, 15, tzinfo=UTC)
SHA = "a" * 64
COPIA = {
    "polizaId": str(POLIZA),
    "numero": "100-2026-001000",
    "estado": "ACTIVA",
    "producto": "Producto",
    "ramo": "100",
    "vigencia": {"desde": "2026-01-01", "hasta": "2026-12-31"},
    "vigente": True,
    "prima": "100.00",
    "sumaAsegurada": "10000.00",
    "moneda": "COP",
    "periodicidad": "MENSUAL",
    "coberturas": [{"codigo": "COLISION", "nombre": "Colision", "obligatoria": True}],
}
UBICACION = {
    "permiso": "PRECISO",
    "lat": 4.60971,
    "lon": -74.08175,
    "precisionM": 100.0,
    "capturadaEn": "2026-11-17T10:00:00-05:00",
}


class Reloj:
    instante = AHORA

    def ahora(self) -> datetime:
        return self.instante


class Almacen:
    def __init__(self) -> None:
        self.archivos: dict[str, tuple[str, int, str]] = {}
        self.conservados: list[str] = []
        self.antes_de_conservar: Any = None

    async def firmar_carga(self, clave: str, *args: Any) -> tuple[str, dict[str, str]]:
        return "https://s3.test/upload", {"key": clave}

    async def firmar_lectura(self, *args: Any) -> str:
        return "https://s3.test/read"

    async def firmar_descarga(self, clave: str, sha: str, ttl: int, tipo: str, nombre: str) -> str:
        if clave not in self.archivos:
            raise EvidenciaNoDisponible
        return "https://s3.test/read?X-Amz-Signature=secreto-descarga"

    async def verificar(self, clave: str, sha: str) -> IntegridadEvidencia:
        archivo = self.archivos.get(clave)
        if archivo is None:
            return IntegridadEvidencia.NO_DISPONIBLE
        return IntegridadEvidencia.VERIFICADA if archivo[0] == sha else IntegridadEvidencia.ALTERADA

    async def conservar(
        self, origen: str, destino: str, sha: str, tamano: int, tipo: str, cifrado: str
    ) -> None:
        await asyncio.sleep(0)
        if self.antes_de_conservar is not None:
            accion, self.antes_de_conservar = self.antes_de_conservar, None
            await accion()
        archivo = self.archivos.get(destino) or self.archivos.get(origen)
        if archivo is None:
            raise EvidenciaNoCargada
        if archivo != (sha, tamano, tipo):
            raise IntegridadEvidenciaInvalida
        self.archivos[destino] = archivo
        self.conservados.append(destino)


class Flujo:
    def __init__(
        self,
        http: httpx.AsyncClient,
        repo: SiniestrosPg,
        almacen: Almacen,
        reloj: Reloj,
        settings: Settings,
    ) -> None:
        self.http, self.repo, self.almacen, self.reloj = http, repo, almacen, reloj
        self.settings = settings

    async def crear(
        self, con_evidencia: bool = True, cargar: bool = True, con_ubicacion: bool = True
    ) -> tuple[str, str | None]:
        r = await self.http.post(
            "/v1/siniestros/borradores",
            headers={"Idempotency-Key": str(uuid4())},
            json={
                "clienteId": str(CLIENTE),
                "descripcion": "Impacto en la puerta.",
                "copiaPoliza": COPIA,
            },
        )
        assert r.status_code == 201, r.text
        borrador = r.json()["borradorId"]
        r = await self.http.patch(
            f"/v1/siniestros/borradores/{borrador}",
            json={
                "causa": "COLISION",
                "fechaEvento": "2026-11-17T10:00:00-05:00",
                **({"ubicacion": UBICACION} if con_ubicacion else {}),
            },
        )
        assert r.status_code == 200, r.text
        if not con_evidencia:
            return borrador, None
        r = await self.http.post(
            f"/v1/siniestros/borradores/{borrador}/evidencias",
            json={
                "nombreArchivo": "foto.jpg",
                "tipo": "FOTOGRAFIA",
                "tipoContenido": "image/jpeg",
                "tamanoBytes": 123,
                "sha256": SHA,
            },
        )
        assert r.status_code == 201, r.text
        evidencia = r.json()["evidenciaId"]
        if cargar:
            self.almacen.archivos[r.json()["carga"]["campos"]["key"]] = (SHA, 123, "image/jpeg")
            r = await self.http.post(
                f"/v1/siniestros/borradores/{borrador}/evidencias/{evidencia}/confirmacion"
            )
            assert r.status_code == 200, r.text
        return borrador, evidencia

    async def enviar(
        self, borrador: str, clave: UUID | None = None, **cambios: Any
    ) -> httpx.Response:
        return await self.http.post(
            f"/v1/siniestros/borradores/{borrador}/envio",
            headers={"Idempotency-Key": str(clave or uuid4())},
            json={"copiaPoliza": {**COPIA, **cambios}},
        )


@pytest.fixture
async def flujo(pool: Any) -> AsyncIterator[Flujo]:
    repo, almacen, reloj = SiniestrosPg(pool), Almacen(), Reloj()
    app = crear_app()
    settings = Settings()
    app.state.borradores = Borradores(repo, almacen, reloj, settings)
    app.state.enviar_borrador = EnviarBorrador(
        EnviosPg(pool, settings), almacen, reloj, "AES256", settings
    )
    app.state.enlace_evidencia_cliente = EmitirEnlaceCliente(repo, almacen, reloj, 300)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://siniestros.test",
        headers={"X-Actor-Id": ACTOR, "X-Actor-Rol": "CLIENTE", "X-Correlation-Id": "corr-envio"},
    ) as http:
        yield Flujo(http, repo, almacen, reloj, settings)


async def test_envio_completo_y_confirmacion_perdida(
    flujo: Flujo, conexion: psycopg.Connection[Any]
) -> None:
    borrador, evidencia = await flujo.crear()
    clave = uuid4()
    respuesta = await flujo.enviar(borrador, clave)
    assert respuesta.status_code == 201, respuesta.text
    resultado = respuesta.json()
    siniestro_id = UUID(resultado["siniestroId"])
    assert resultado["estado"] == "REPORTADO"
    assert resultado["codigo"] == "88-2026-1000"
    assert (await flujo.http.get("/v1/siniestros/borradores")).json()["items"] == []
    detalle = (await flujo.http.get(f"/v1/siniestros/borradores/{borrador}")).json()
    assert detalle["estado"] == "CONVERTIDO" and detalle["envio"] == resultado
    leido = await flujo.repo.detalle(siniestro_id)
    assert leido is not None and leido.siniestro.origen.value == "MOVIL"
    assert leido.siniestro.causa == "COLISION"
    assert leido.siniestro.fecha_evento == AHORA - timedelta(days=1)
    assert {h.accion for h in leido.historial} == {"REPORTADO", "EVIDENCIAS_VALIDADAS"}
    assert str(leido.evidencias[0].id) == evidencia
    assert leido.evidencias[0].clave_objeto.startswith("siniestros/")
    filas = conexion.execute("SELECT event_type, correlation_id FROM outbox").fetchall()
    assert filas == [("SiniestroReportado", "corr-envio")]
    assert conexion.execute("SELECT descripcion FROM siniestros").fetchone() == (
        "Impacto en la puerta.",
    )
    # La misma confirmacion se recupera incluso sin poliza y tras vencer su vigencia.
    flujo.reloj.instante = datetime(2027, 1, 2, tzinfo=UTC)
    flujo.settings.ubicacion_precision_max_m = 1
    for repetida in (clave, uuid4()):
        r = await flujo.http.post(
            f"/v1/siniestros/borradores/{borrador}/envio",
            headers={"Idempotency-Key": str(repetida)},
            json={},
        )
        assert r.status_code == 201 and r.json() == resultado
    assert conexion.execute("SELECT count(*) FROM siniestros").fetchone() == (1,)
    analista = uuid4()
    tomado = await TomarSiguiente(flujo.repo, flujo.reloj, Plazos(48, 480, 1440)).ejecutar(
        Actor(str(analista), "ANALISTA_SINIESTROS", None), analista, uuid4()
    )
    assert tomado.siniestro.id == siniestro_id
    listado = await flujo.http.get(f"/v1/clientes/{CLIENTE}/siniestros")
    assert listado.json()["items"][0]["estado"] == "EN_EVALUACION"
    propio = await flujo.http.get(f"/v1/clientes/{CLIENTE}/siniestros/{siniestro_id}")
    assert propio.json()["estado"] == "EN_EVALUACION"
    assert "claveObjeto" not in propio.text
    assert propio.json()["ubicacion"] == {**UBICACION, "estado": "DISPONIBLE"}
    assert detalle["ubicacion"] == propio.json()["ubicacion"]
    assert leido.siniestro.ubicacion_declarada.lat == UBICACION["lat"]


@pytest.mark.parametrize("permiso", ["DENEGADO", "DENEGADO_PERMANENTEMENTE", "APROXIMADO"])
async def test_ubicacion_no_disponible_se_conserva_y_bloquea_envio_obligatorio(
    flujo: Flujo,
    conexion: psycopg.Connection[Any],
    permiso: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    borrador, _ = await flujo.crear()
    ruta = f"/v1/siniestros/borradores/{borrador}"
    archivos = dict(flujo.almacen.archivos)
    respuesta = await flujo.http.patch(ruta, json={"ubicacion": {"permiso": permiso}})
    assert respuesta.status_code == 200, respuesta.text
    ubicacion = respuesta.json()["ubicacion"]
    assert ubicacion["estado"] == "NO_DISPONIBLE"
    assert ubicacion["lat"] is None and ubicacion["lon"] is None
    assert (await flujo.http.get(ruta)).json()["ubicacion"] == ubicacion
    assert (await flujo.http.get("/v1/siniestros/borradores")).json()["items"][0][
        "ubicacion"
    ] == ubicacion
    envio = await flujo.enviar(borrador)
    assert (envio.status_code, envio.json()["codigo"]) == (422, "SOLICITUD_INVALIDA")
    assert envio.json()["detalles"]["campos"] == ["ubicacion"]
    assert envio.json()["correlationId"] == "corr-envio"
    assert conexion.execute("SELECT count(*) FROM siniestros").fetchone() == (0,)
    assert conexion.execute("SELECT count(*) FROM outbox").fetchone() == (0,)
    assert flujo.almacen.archivos == archivos
    assert "4.60971" not in caplog.text and "-74.08175" not in caplog.text
    flujo.settings.ubicacion_tipos_opcionales = ("COLISION",)
    enviado = await flujo.enviar(borrador)
    assert enviado.status_code == 201, enviado.text
    detalle = await flujo.http.get(
        f"/v1/clientes/{CLIENTE}/siniestros/{enviado.json()['siniestroId']}"
    )
    assert detalle.json()["ubicacion"] == ubicacion


async def test_precision_invalida_se_guarda_y_se_corrige_en_el_mismo_borrador(
    flujo: Flujo,
    conexion: psycopg.Connection[Any],
) -> None:
    borrador, _ = await flujo.crear()
    ruta = f"/v1/siniestros/borradores/{borrador}"
    imprecisa = {**UBICACION, "permiso": "APROXIMADO", "precisionM": 100.01}
    r = await flujo.http.patch(ruta, json={"ubicacion": imprecisa})
    assert r.status_code == 200 and r.json()["ubicacion"] == {**imprecisa, "estado": "NO_CONFIABLE"}
    clave = uuid4()
    assert (await flujo.enviar(borrador, clave)).status_code == 422
    corregida = {**imprecisa, "precisionM": 100}
    r = await flujo.http.patch(ruta, json={"ubicacion": corregida})
    assert r.status_code == 200 and r.json()["ubicacion"]["estado"] == "DISPONIBLE"
    enviado = await flujo.enviar(borrador, clave)
    assert enviado.status_code == 201, enviado.text
    assert (await flujo.enviar(borrador, clave)).json() == enviado.json()
    assert conexion.execute("SELECT count(*) FROM siniestros").fetchone() == (1,)
    payload = str(conexion.execute("SELECT payload FROM outbox").fetchone())
    assert "4.60971" not in payload and "-74.08175" not in payload
    assert (
        await flujo.http.patch(ruta, json={"ubicacion": {"permiso": "DENEGADO"}})
    ).status_code == 409


async def test_ubicacion_ajena_o_sin_consentimiento_no_modifica_borrador(
    flujo: Flujo,
) -> None:
    borrador, _ = await flujo.crear()
    ruta = f"/v1/siniestros/borradores/{borrador}"
    antes = (await flujo.http.get(ruta)).json()
    ajeno = await flujo.http.patch(
        ruta,
        json={"ubicacion": {"permiso": "DENEGADO"}},
        headers={"X-Actor-Id": str(uuid4())},
    )
    assert ajeno.status_code == 404
    sin_permiso = await flujo.http.patch(
        ruta,
        json={"ubicacion": {**UBICACION, "permiso": "DENEGADO"}},
    )
    assert sin_permiso.status_code == 422
    assert "4.60971" not in sin_permiso.text and "-74.08175" not in sin_permiso.text
    assert (await flujo.http.get(ruta)).json() == antes


async def test_envio_actualiza_confianza_si_cambio_el_umbral(flujo: Flujo) -> None:
    borrador, _ = await flujo.crear()
    ruta = f"/v1/siniestros/borradores/{borrador}"
    r = await flujo.http.patch(ruta, json={"ubicacion": {**UBICACION, "precisionM": 150}})
    assert r.json()["ubicacion"]["estado"] == "NO_CONFIABLE"
    flujo.settings.ubicacion_precision_max_m = 200
    enviado = await flujo.enviar(borrador)
    assert enviado.status_code == 201, enviado.text
    definitivo = await flujo.http.get(
        f"/v1/clientes/{CLIENTE}/siniestros/{enviado.json()['siniestroId']}"
    )
    guardada = (await flujo.http.get(ruta)).json()["ubicacion"]
    assert guardada == definitivo.json()["ubicacion"]
    assert guardada["estado"] == "DISPONIBLE" and guardada["precisionM"] == 150


async def test_borrador_anterior_sin_ubicacion_solo_se_envia_si_el_tipo_lo_admite(
    flujo: Flujo,
) -> None:
    borrador, _ = await flujo.crear(con_ubicacion=False)
    assert (await flujo.enviar(borrador)).status_code == 422
    flujo.settings.ubicacion_tipos_opcionales = ("COLISION",)
    enviado = await flujo.enviar(borrador)
    assert enviado.status_code == 201, enviado.text
    borrador_leido = await flujo.http.get(f"/v1/siniestros/borradores/{borrador}")
    definitivo = await flujo.http.get(
        f"/v1/clientes/{CLIENTE}/siniestros/{enviado.json()['siniestroId']}"
    )
    assert borrador_leido.json()["ubicacion"] is None
    assert definitivo.json()["ubicacion"] is None


@pytest.mark.parametrize("misma_clave", [True, False])
async def test_envios_concurrentes_crean_un_solo_caso(
    flujo: Flujo, conexion: psycopg.Connection[Any], misma_clave: bool
) -> None:
    borrador, _ = await flujo.crear()
    clave = uuid4()
    respuestas = await asyncio.gather(
        *(flujo.enviar(borrador, clave if misma_clave else uuid4()) for _ in range(10))
    )
    assert all(r.status_code == 201 for r in respuestas), [r.text for r in respuestas]
    assert all(r.json() == respuestas[0].json() for r in respuestas)
    for tabla, total in (
        ("siniestros", 1),
        ("evidencias", 1),
        ("historial_siniestro", 2),
        ("outbox", 1),
    ):
        assert conexion.execute(f"SELECT count(*) FROM {tabla}").fetchone() == (total,)


async def test_falla_de_carga_y_reintento_conservan_el_mismo_borrador(flujo: Flujo) -> None:
    borrador, evidencia = await flujo.crear(cargar=False)
    r = await flujo.enviar(borrador)
    assert (r.status_code, r.json()["codigo"]) == (409, "EVIDENCIA_NO_CARGADA")
    ruta = f"/v1/siniestros/borradores/{borrador}"
    r = await flujo.http.post(f"{ruta}/evidencias/{evidencia}/confirmacion")
    assert r.status_code == 409
    recuperado = (await flujo.http.get(ruta)).json()
    assert recuperado["descripcion"] == "Impacto en la puerta."
    assert recuperado["evidencias"][0]["evidenciaId"] == evidencia
    flujo.almacen.archivos[f"tmp/{borrador}/{evidencia}"] = (SHA, 123, "image/jpeg")
    assert (await flujo.http.post(f"{ruta}/evidencias/{evidencia}/confirmacion")).status_code == 200
    assert (await flujo.enviar(borrador)).status_code == 201


@pytest.mark.parametrize(
    ("cambios", "campo"),
    [
        ({"causa": None}, "causa"),
        ({"causa": "descripcion libre"}, "causa"),
        ({"fechaEvento": None}, "fechaEvento"),
        ({"fechaEvento": "2026-11-19T10:00:00-05:00"}, "fechaEvento"),
        ({"fechaEvento": "2025-12-31T23:59:59-05:00"}, "fechaEvento"),
    ],
)
async def test_datos_invalidos_no_cierran_el_borrador_y_se_pueden_corregir(
    flujo: Flujo, cambios: dict[str, Any], campo: str
) -> None:
    borrador, _ = await flujo.crear()
    ruta = f"/v1/siniestros/borradores/{borrador}"
    assert (await flujo.http.patch(ruta, json=cambios)).status_code == 200
    r = await flujo.enviar(borrador)
    assert (r.status_code, r.json()["codigo"]) == (422, "SOLICITUD_INVALIDA")
    assert campo in r.json()["detalles"]["campos"]
    assert r.json()["correlationId"] == "corr-envio"
    assert (await flujo.http.get(ruta)).json()["estado"] == "ABIERTO"
    await flujo.http.patch(
        ruta, json={"causa": "COLISION", "fechaEvento": "2026-11-17T10:00:00-05:00"}
    )
    assert (await flujo.enviar(borrador)).status_code == 201


async def test_sin_evidencias_o_poliza_no_vigente_no_se_envia(flujo: Flujo) -> None:
    borrador, _ = await flujo.crear(con_evidencia=False)
    r = await flujo.enviar(borrador)
    assert r.status_code == 422 and r.json()["detalles"]["campos"] == ["evidencias"]
    for cambios in ({"estado": "EMITIDA"}, {"vigente": False}):
        r = await flujo.enviar(borrador, **cambios)
        assert (r.status_code, r.json()["codigo"]) == (409, "POLIZA_NO_VIGENTE")


async def test_fallo_del_outbox_revierte_todo_y_reintenta(
    flujo: Flujo, conexion: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    borrador, _ = await flujo.crear()
    clave = uuid4()
    with monkeypatch.context() as cambios:
        cambios.setattr(
            outbox_pg, "insertar", AsyncMock(side_effect=RuntimeError("fallo-transaccional"))
        )
        with pytest.raises(RuntimeError, match="fallo-transaccional"):
            await flujo.enviar(borrador, clave)
    for tabla in ("siniestros", "evidencias", "historial_siniestro", "outbox"):
        assert conexion.execute(f"SELECT count(*) FROM {tabla}").fetchone() == (0,)
    assert conexion.execute("SELECT estado FROM borradores").fetchone() == ("ABIERTO",)
    assert (
        conexion.execute(
            "SELECT 1 FROM idempotencia WHERE clave = %s", (f"envio:{clave}",)
        ).fetchone()
        is None
    )
    assert (await flujo.enviar(borrador, clave)).status_code == 201


async def test_otro_titular_y_edicion_de_convertido_no_modifican_nada(flujo: Flujo) -> None:
    borrador, evidencia = await flujo.crear()
    ruta = f"/v1/siniestros/borradores/{borrador}"
    for metodo, sufijo, cuerpo in (
        ("GET", "", None),
        ("PATCH", "", {"descripcion": "otra"}),
        ("POST", "/envio", {"copiaPoliza": COPIA}),
        ("DELETE", f"/evidencias/{evidencia}", None),
    ):
        r = await flujo.http.request(
            metodo,
            ruta + sufijo,
            json=cuerpo,
            headers={"X-Actor-Id": str(uuid4()), "Idempotency-Key": str(uuid4())},
        )
        assert r.status_code == 404, r.text
    enviado = (await flujo.enviar(borrador)).json()
    assert (await flujo.http.patch(ruta, json={"descripcion": "otra"})).status_code == 409
    assert (await flujo.http.delete(f"{ruta}/evidencias/{evidencia}")).status_code == 409
    r = await flujo.http.get(f"/v1/clientes/{uuid4()}/siniestros/{enviado['siniestroId']}")
    assert r.status_code == 404


async def test_misma_clave_con_otro_borrador_es_conflicto(flujo: Flujo) -> None:
    uno, _ = await flujo.crear()
    dos, _ = await flujo.crear()
    clave = uuid4()
    assert (await flujo.enviar(uno, clave)).status_code == 201
    r = await flujo.enviar(dos, clave)
    assert (r.status_code, r.json()["codigo"]) == (422, "IDEMPOTENCIA_CONFLICTO")
    assert (await flujo.http.get(f"/v1/siniestros/borradores/{dos}")).json()["estado"] == "ABIERTO"


async def test_edicion_durante_preparacion_no_confirma_datos_obsoletos(flujo: Flujo) -> None:
    borrador, _ = await flujo.crear()

    async def editar() -> None:
        await flujo.repo.actualizar_borrador(ACTOR, UUID(borrador), {"descripcion": "Corregida."})

    flujo.almacen.antes_de_conservar = editar
    r = await flujo.enviar(borrador)
    assert (r.status_code, r.json()["codigo"]) == (409, "BORRADOR_MODIFICADO")
    assert (await flujo.enviar(borrador)).status_code == 201


async def test_quitar_carga_fallida_y_paginar_solo_reportes_propios(flujo: Flujo) -> None:
    uno, _ = await flujo.crear()
    dos, _ = await flujo.crear()
    ruta = f"/v1/siniestros/borradores/{uno}/evidencias"
    extra = await flujo.http.post(
        ruta,
        json={
            "nombreArchivo": "pendiente.jpg",
            "tipo": "FOTOGRAFIA",
            "tipoContenido": "image/jpeg",
            "tamanoBytes": 123,
            "sha256": SHA,
        },
    )
    assert extra.status_code == 201
    assert (await flujo.enviar(uno)).status_code == 409
    assert (await flujo.http.delete(f"{ruta}/{extra.json()['evidenciaId']}")).status_code == 204
    resultados = [(await flujo.enviar(b)).json()["siniestroId"] for b in (uno, dos)]
    pagina = (await flujo.http.get(f"/v1/clientes/{CLIENTE}/siniestros?limite=1")).json()
    assert len(pagina["items"]) == 1 and pagina["siguienteCursor"]
    siguiente = (
        await flujo.http.get(
            f"/v1/clientes/{CLIENTE}/siniestros",
            params={"limite": 1, "cursor": pagina["siguienteCursor"]},
        )
    ).json()
    assert siguiente["siguienteCursor"] is None
    assert {pagina["items"][0]["siniestroId"], siguiente["items"][0]["siniestroId"]} == set(
        resultados
    )
    assert (await flujo.http.get(f"/v1/clientes/{uuid4()}/siniestros")).json()["items"] == []


async def test_reversa_no_pierde_confirmaciones(
    flujo: Flujo, conexion: psycopg.Connection[Any]
) -> None:
    borrador, _ = await flujo.crear()
    resultado = (await flujo.enviar(borrador)).json()
    reversa = (
        resources.files("siniestros.adapters.outbound")
        .joinpath("migraciones", "down", "006_envio_borrador.sql")
        .read_text(encoding="utf-8")
    )
    with pytest.raises(psycopg.errors.RaiseException, match="existen borradores convertidos"):
        conexion.execute(reversa)
    assert (await flujo.enviar(borrador)).json() == resultado


@pytest.mark.parametrize("convertido", [False, True])
async def test_reversa_007_no_elimina_ubicacion_guardada(
    flujo: Flujo,
    conexion: psycopg.Connection[Any],
    convertido: bool,
) -> None:
    borrador, _ = await flujo.crear()
    if convertido:
        assert (await flujo.enviar(borrador)).status_code == 201
    ruta = f"/v1/siniestros/borradores/{borrador}"
    antes = (await flujo.http.get(ruta)).json()
    reversa = (
        resources.files("siniestros.adapters.outbound")
        .joinpath("migraciones", "down", "007_ubicacion_reporte.sql")
        .read_text(encoding="utf-8")
    )
    with pytest.raises(psycopg.errors.RaiseException, match="existen metadatos de ubicacion"):
        conexion.execute(reversa)
    assert (await flujo.http.get(ruta)).json() == antes


async def test_descarga_valida_titular_pertenencia_y_confirmacion(
    flujo: Flujo, conexion: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    uno, evidencia = await flujo.crear()
    dos, ajena = await flujo.crear()
    caso = (await flujo.enviar(uno)).json()["siniestroId"]
    await flujo.enviar(dos)
    ruta = f"/v1/clientes/{CLIENTE}/siniestros/{caso}/evidencias/{evidencia}/enlace"
    firma = AsyncMock(wraps=flujo.almacen.firmar_descarga)
    monkeypatch.setattr(flujo.almacen, "firmar_descarga", firma)
    for fallida in (
        ruta.replace(str(CLIENTE), str(uuid4())),
        ruta.replace(caso, str(uuid4())),
        ruta.replace(str(evidencia), str(ajena)),
        ruta.replace(str(evidencia), str(uuid4())),
    ):
        respuesta = await flujo.http.post(fallida)
        assert respuesta.status_code == 404, respuesta.text
        assert "url" not in respuesta.json()
    firma.assert_not_awaited()
    conexion.execute(
        "UPDATE borrador_evidencias SET estado_carga = 'PENDIENTE', carga_confirmada_en = NULL WHERE id = %s",
        (evidencia,),
    )
    pendiente = await flujo.http.post(ruta)
    assert (pendiente.status_code, pendiente.json()["codigo"]) == (409, "EVIDENCIA_NO_CARGADA")
    firma.assert_not_awaited()
    conexion.execute(
        "UPDATE borrador_evidencias SET estado_carga = 'CARGADA', carga_confirmada_en = %s WHERE id = %s",
        (AHORA, evidencia),
    )
    respuesta = await flujo.http.post(ruta)
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json() == {
        "url": "https://s3.test/read?X-Amz-Signature=secreto-descarga",
        "expiraEn": "2026-11-18T15:05:00Z",
        "nombreArchivo": "foto.jpg",
        "tipoContenido": "image/jpeg",
    }
    eventos = conexion.execute(
        "SELECT payload::text FROM outbox WHERE event_type = 'EvidenciaConsultada'"
    ).fetchall()
    assert len(eventos) == 1 and "secreto-descarga" not in eventos[0][0]
    flujo.almacen.archivos.clear()
    ausente = await flujo.http.post(ruta)
    assert (ausente.status_code, ausente.json()["codigo"]) == (404, "EVIDENCIA_NO_DISPONIBLE")


async def test_descarga_https_real_con_nombre_mime_y_expiracion(
    flujo: Flujo,
    pool: Any,
    conexion: psycopg.Connection[Any],
    s3: Any,
    bucket: str,
    localstack: str,
    credenciales: None,
) -> None:
    from siniestros.adapters.outbound.evidencias_s3 import EvidenciasS3, cliente_s3

    borrador, evidencia = await flujo.crear()
    caso = (await flujo.enviar(borrador)).json()["siniestroId"]
    clave = conexion.execute(
        "SELECT clave_objeto FROM evidencias WHERE id = %s", (evidencia,)
    ).fetchone()[0]
    contenido = b"evidencia sintetica"
    sha = hashlib.sha256(contenido).hexdigest()
    s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
    s3.put_object(
        Bucket=bucket,
        Key=clave,
        Body=contenido,
        ContentType="application/octet-stream",
        ChecksumSHA256=base64.b64encode(bytes.fromhex(sha)).decode(),
        ServerSideEncryption="AES256",
    )
    conexion.execute("UPDATE evidencias SET sha256 = %s WHERE id = %s", (sha, evidencia))
    almacen = EvidenciasS3(
        cliente_s3("us-east-1", 2000, localstack),
        bucket,
        2000,
        cliente_s3("us-east-1", 2000, localstack.replace("http://", "https://")),
    )
    app = crear_app()
    app.state.enlace_evidencia_cliente = EmitirEnlaceCliente(flujo.repo, almacen, flujo.reloj, 2)
    ruta = f"/v1/clientes/{CLIENTE}/siniestros/{caso}/evidencias/{evidencia}/enlace"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://siniestros.test",
        headers={"X-Actor-Id": ACTOR, "X-Actor-Rol": "CLIENTE"},
    ) as interno:
        respuesta = await interno.post(ruta)
        assert respuesta.status_code == 200, respuesta.text
        url = respuesta.json()["url"]
        assert url.startswith("https://") and "X-Amz-Expires=2" in url
        # Solo el cliente de prueba omite verificar el certificado autofirmado de LocalStack.
        async with httpx.AsyncClient(verify=False) as descarga:
            archivo = await descarga.get(url)
            assert archivo.status_code == 200 and archivo.content == contenido
            assert archivo.headers["content-type"] == "image/jpeg"
            assert 'filename="foto.jpg"' in archivo.headers["content-disposition"]
            s3.put_object(Bucket=bucket, Key=clave, Body=b"version posterior")
            assert (await descarga.get(url)).content == contenido
            await asyncio.sleep(3.5)
            assert (await descarga.get(url)).status_code == 403
        s3.delete_object(Bucket=bucket, Key=clave)
        ausente = await interno.post(ruta)
        assert (ausente.status_code, ausente.json()["codigo"]) == (404, "EVIDENCIA_NO_DISPONIBLE")
