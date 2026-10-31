"""CT-W14-01 y CT-W15-01: `bff-web` verifica como proveedor el pacto del portal
(contracts/pact/web-bff-web.json).

El pacto lo genera el portal (`proyecto-final-2-frontend/web`, `npm run test:pact`) y se copia aquí
sin editarlo. La app real de bff-web (rutas, autorización por grupo, DTO y falla cerrada) se sirve
con uvicorn en un hilo; polizas y clientes se simulan con un transporte de httpx. Cada estado del
proveedor siembra la decisión que polizas devolvería para el caso sintético (S01, S02, S04, S06):
las alternativas salen de la semilla de reglas 2026.10.1 de catalogo y la comunicación es la de
docs/hu-w14-comunicacion.md §1.1. HU-W15: los estados de la aceptación hacen que polizas responda
lo que responde en ese caso (201, 200 con la misma clave, 409 OFERTA_VENCIDA o DECISION_NO_APROBADA y
422 DECLARACIONES_INCOMPLETAS si faltan las declaraciones obligatorias). HU-W16 (16.14): los estados
del recaudo hacen que polizas responda el EstadoRecaudo de ese estado o el 404
ACEPTACION_NO_ENCONTRADA; las interacciones las agrega el portal en la 16.15. HU-W17 (17.11): los
estados de la póliza hacen que polizas responda la Poliza EMITIENDO o ACTIVA, el 404
POLIZA_NO_ENCONTRADA, el EnlaceDocumento o el 409 POLIZA_NO_ACTIVA; las interacciones las agrega el
portal en la 17.12. El verificador envía
los encabezados que pone API Gateway para un usuario del grupo `asesores`.
"""

import asyncio
import json
import re
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from bff_web.adapters.inbound import aceptaciones as ruta_aceptacion
from bff_web.adapters.inbound import polizas as ruta_poliza
from bff_web.adapters.inbound import recaudos as ruta_recaudo
from bff_web.adapters.inbound import suscripciones as ruta
from bff_web.adapters.outbound.clientes import ClientesCliente
from bff_web.adapters.outbound.polizas import PolizasCliente
from bff_web.application.aceptacion import Aceptaciones
from bff_web.application.poliza import Polizas
from bff_web.application.recaudo import Recaudos
from bff_web.application.suscripcion import Suscripciones
from bff_web.config import settings
from bff_web.main import app
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pact import Verifier

RAIZ = Path(__file__).parents[4]
PACTO = RAIZ / "contracts" / "pact" / "web-bff-web.json"
SEMILLA = json.loads(
    (
        RAIZ
        / "services/catalogo/src/catalogo/adapters/outbound/semillas/reglas_suscripcion_2026_10_1.json"
    ).read_text(encoding="utf-8")
)
EMISOR = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_back"
EXISTE = "existe la decisión de suscripción"
NO_EXISTE = "no existe la decisión de suscripción"
# HU-W15: estados de la aceptación (web/pact/contratacion.pact.spec.ts).
APROBADA_VIGENTE = "existe una decisión aprobada con la oferta vigente"
MISMA_CLAVE = "la cotización ya tiene una aceptación con la misma clave"
APROBADA_VENCIDA = "existe una decisión aprobada con la oferta vencida"
RECHAZADA = "existe una decisión rechazada"
ESTADOS_ACEPTACION = {APROBADA_VIGENTE, MISMA_CLAVE, APROBADA_VENCIDA, RECHAZADA}
# HU-W16 (16.14): estados del recaudo, para las interacciones del paso 3 que agrega la 16.15
# (GET /v1/cotizaciones/{cotizacionId}/recaudo, parámetro `cotizacionId`).
RECAUDO_PENDIENTE = "la aceptación tiene el recaudo pendiente"
RECAUDO_CONFIRMADO = "la aceptación tiene el recaudo confirmado"
RECAUDO_RECHAZADO = "la aceptación tiene el recaudo rechazado"
SIN_ACEPTACION = "la cotización no tiene aceptación"
ESTADOS_RECAUDO = {RECAUDO_PENDIENTE, RECAUDO_CONFIRMADO, RECAUDO_RECHAZADO, SIN_ACEPTACION}
COTIZACION_RECAUDO = "00000000-0000-4000-a000-000000000001"
# HU-W17 (17.11): estados de la póliza, para las interacciones que agrega la 17.12
# (GET …/poliza, parámetro `cotizacionId`, y GET /v1/polizas/{polizaId}/documento, `polizaId`).
POLIZA_EMITIENDO = "la cotización tiene la póliza emitiéndose"
POLIZA_ACTIVA = "la cotización tiene la póliza activa"
SIN_POLIZA = "la cotización no tiene póliza"
DOCUMENTO_ACTIVA = "la póliza está activa con su documento"
DOCUMENTO_EMITIENDO = "la póliza todavía se está emitiendo"
ESTADOS_POLIZA = {
    POLIZA_EMITIENDO,
    POLIZA_ACTIVA,
    SIN_POLIZA,
    DOCUMENTO_ACTIVA,
    DOCUMENTO_EMITIENDO,
}
POLIZA_ID = "00000000-0000-4000-9000-000000000001"
RUTA_POLIZA = f"/v1/cotizaciones/{COTIZACION_RECAUDO}/poliza"
RUTA_DOCUMENTO = f"/v1/polizas/{POLIZA_ID}/documento"
SIN_DATOS: dict[str, Any] = {
    "vigenteHasta": None,
    "motivoCliente": None,
    "alternativas": [],
    "plazoRespuestaHoras": None,
}


def _reglas(cambios: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Las seis reglas de la semilla como las devuelve polizas a un asesor, con los cambios del caso."""
    reglas = []
    for r in SEMILLA["reglas"]:
        reservada = r["visibilidad"] == "RESERVADA"
        regla = {
            "codigo": r["codigo"],
            "orden": r["orden"],
            "tipo": r["tipo"],
            "descripcion": r["descripcion"],
            "campo": r["campo"],
            "operador": r["operador"],
            "valor": r["valor"],
            "valorCaso": None if reservada else "1",
            "resultado": "CUMPLE",
            "visibilidad": r["visibilidad"],
            "reservada": reservada,
        }
        reglas.append({**regla, **cambios.get(r["codigo"], {})})
    return reglas


def _decision(decision_id: str, caso: str) -> dict[str, Any]:
    """La decisión que polizas devolvería para el caso sintético, con su comunicación."""
    alternativas = SEMILLA["alternativas"]
    por_caso: dict[str, dict[str, Any]] = {
        "S01": {
            "resultado": "APROBADA",
            "reglaDeterminante": None,
            "motivos": [],
            "reglasEvaluadas": _reglas({}),
            "comunicacion": {
                **SIN_DATOS,
                "condicion": "APROBADA_SIN_AJUSTES",
                "admiteContratacion": True,
                "siguientePaso": "ACEPTAR_OFERTA",
                "vigenteHasta": "2026-10-30T14:02:12.564423Z",
            },
        },
        "S02": {
            "resultado": "RECHAZADA",
            "reglaDeterminante": "SUS-001",
            "motivos": ["EDAD_FUERA_DE_RANGO"],
            "reglasEvaluadas": _reglas({"SUS-001": {"valorCaso": "68", "resultado": "NO_CUMPLE"}}),
            "comunicacion": {
                **SIN_DATOS,
                "condicion": "NO_ASEGURABLE",
                "admiteContratacion": False,
                "siguientePaso": "COTIZAR_ALTERNATIVA",
                "motivoCliente": "EDAD_FUERA_DE_RANGO",
                "alternativas": alternativas,
            },
        },
        "S04": {
            "resultado": "RECHAZADA",
            "reglaDeterminante": "SUS-015",
            "motivos": ["NO_ELEGIBLE"],
            "reglasEvaluadas": _reglas({"SUS-015": {"resultado": "NO_CUMPLE"}}),
            "comunicacion": {
                **SIN_DATOS,
                "condicion": "NO_ASEGURABLE",
                "admiteContratacion": False,
                "siguientePaso": "COTIZAR_ALTERNATIVA",
                "motivoCliente": "NO_ELEGIBLE",
                "alternativas": alternativas,
            },
        },
        "S06": {
            "resultado": "REVISION_REQUERIDA",
            "reglaDeterminante": None,
            "motivos": ["SUMA_REQUIERE_REVISION"],
            "reglasEvaluadas": _reglas(
                {"SUS-004": {"valorCaso": "620000000", "resultado": "NO_CUMPLE"}}
            ),
            "comunicacion": {
                **SIN_DATOS,
                "condicion": "EN_REVISION",
                "admiteContratacion": False,
                "siguientePaso": "ESPERAR_REVISION",
                "motivoCliente": "SUMA_REQUIERE_REVISION",
                "plazoRespuestaHoras": 8,
            },
        },
    }
    return {
        "id": decision_id,
        "codigo": "DEC-2026-000020",
        "cotizacionId": "00000000-0000-4000-a000-000000000001",
        "ofertaId": "00000000-0000-4000-b000-000000000001",
        "cotizacionCodigo": "COT-2026-00001",
        "clienteId": "00000000-0000-4000-8000-000000000001",
        "productoId": "vida-hipotecario",
        "tipo": "AUTOMATICA",
        "versionReglas": "2026.10.1",
        "hashReglas": "sha256:reglas-2026-10-1",
        "condiciones": {
            "socioId": None,
            "versionProducto": "2026.10.1",
            "moneda": "COP",
            "periodicidad": "MENSUAL",
            "primaTotal": "64638",
            "versionReglasPrima": "2026.10.1",
            "huellaCalculo": "sha256:calculo",
            "coberturas": [
                {"codigo": "MUERTE", "nombre": "Muerte por cualquier causa", "obligatoria": True}
            ],
            "sumaAsegurada": "180000000",
            "plazoCreditoMeses": 180,
            "calculadoEn": "2026-10-23T14:02:12.564423Z",
            "vigenteHasta": "2026-10-30T14:02:12.564423Z",
        },
        "contexto": {
            "nivelRiesgo": "BAJO",
            "confianza": "0.92",
            "fuentesConsultadas": 4,
            "fuentesDeclaradas": 4,
        },
        "admiteExcepcion": False,
        "decididaEn": "2026-10-23T14:02:12.900000Z",
        "duracionMs": 41,
        **por_caso[caso],
    }


def _error(codigo: str, estado: int, detalles: dict[str, Any] | None = None) -> httpx.Response:
    return httpx.Response(
        estado,
        json={"codigo": codigo, "mensaje": "x", "correlationId": "c", "detalles": detalles or {}},
    )


def _aceptacion(cotizacion_id: str, cuerpo: dict[str, Any]) -> dict[str, Any]:
    """La aceptación que polizas registraría con el cuerpo recibido."""
    return {
        "id": "00000000-0000-4000-f000-000000000001",
        "cotizacionId": cotizacion_id,
        "ofertaId": "00000000-0000-4000-b000-000000000001",
        "decisionId": cuerpo["decisionId"],
        "clienteId": "00000000-0000-4000-8000-000000000001",
        "huellaCalculo": "sha256:calculo",
        "versionDeclaraciones": cuerpo["versionDeclaraciones"],
        "declaraciones": cuerpo["declaraciones"],
        "canal": "PORTAL_ASESOR",
        "actorId": "sub-asesor-pact",
        "aceptadaEn": "2026-10-24T15:00:00.123456Z",
        "huellaEvidencia": "sha256:" + "a" * 64,
        "recaudoHabilitado": True,
        "siguientePaso": "FIRMA",
    }


def _recaudo(estado: str) -> httpx.Response:
    """El EstadoRecaudo que polizas devuelve en el estado (claveRecaudo = id de la aceptación de
    `_aceptacion`, monto y moneda de S01), o el 404 si la cotización no tiene aceptación."""
    pendiente = {
        "estado": "PENDIENTE",
        "claveRecaudo": "00000000-0000-4000-f000-000000000001",
        "monto": "64638",
        "moneda": "COP",
        "transaccionId": None,
        "motivo": None,
        "actualizadoEn": "2026-10-24T15:00:00.123456Z",
    }
    resuelto = {**pendiente, "actualizadoEn": "2026-10-24T15:00:01.250000Z"}
    match estado:
        case "la aceptación tiene el recaudo pendiente":
            return httpx.Response(200, json=pendiente)
        case "la aceptación tiene el recaudo confirmado":
            cuerpo = {**resuelto, "estado": "CONFIRMADO", "transaccionId": "TX-SINTETICA-0001"}
            return httpx.Response(200, json=cuerpo)
        case "la aceptación tiene el recaudo rechazado":
            cuerpo = {**resuelto, "estado": "RECHAZADO", "motivo": "FONDOS_INSUFICIENTES"}
            return httpx.Response(200, json=cuerpo)
    return _error("ACEPTACION_NO_ENCONTRADA", 404)


def _poliza(estado: str) -> httpx.Response:
    """Lo que polizas devuelve en el estado: la Poliza de S01 (la aceptación de `_aceptacion` y el
    recaudo confirmado de `_recaudo`), su EnlaceDocumento, el 409 mientras se emite o el 404."""
    emitiendo = {
        "polizaId": POLIZA_ID,
        "estado": "EMITIENDO",
        "numero": "100-2026-000001",
        "producto": {"id": "vida-hipotecario", "version": "2026.09.1"},
        "ramo": {"codigo": "100", "nombre": "Vida hipotecario"},
        "coberturas": [
            {"codigo": "MUERTE", "nombre": "Muerte por cualquier causa", "obligatoria": True},
            {
                "codigo": "INCAPACIDAD_TOTAL_PERMANENTE",
                "nombre": "Incapacidad total y permanente",
                "obligatoria": True,
            },
        ],
        "vigencia": {"desde": "2026-10-25", "hasta": "2027-10-24"},
        "sumaAsegurada": "180000000",
        "prima": "64638",
        "moneda": "COP",
        "periodicidad": "MENSUAL",
        "aceptadaEn": "2026-10-24T15:00:00.123456Z",
        "recaudadaEn": "2026-10-24T15:00:01.250000Z",
        "emitidaEn": None,
        "documento": None,
        "firma": None,
    }
    match estado:
        case "la cotización tiene la póliza emitiéndose":
            return httpx.Response(200, json=emitiendo)
        case "la cotización tiene la póliza activa":
            activa = {
                **emitiendo,
                "estado": "ACTIVA",
                "emitidaEn": "2026-10-24T15:00:03.500000Z",
                "documento": {"huella": "sha256:" + "b" * 64, "versionId": "v-sintetica-0001"},
                "firma": {"firmaId": "FIR-SINT-C01", "selloTiempo": "2026-01-01T00:00:00Z"},
            }
            return httpx.Response(200, json=activa)
        case "la póliza está activa con su documento":
            url = (
                "https://polizas-documentos.s3.amazonaws.com/polizas/"
                f"{POLIZA_ID}/100-2026-000001.pdf?versionId=v-sintetica-0001"
                "&X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Expires=300&X-Amz-Signature=sintetica"
            )
            return httpx.Response(200, json={"url": url, "venceEn": "2026-10-24T15:05:03.500000Z"})
        case "la póliza todavía se está emitiendo":
            return _error("POLIZA_NO_ACTIVA", 409)
    return _error("POLIZA_NO_ENCONTRADA", 404)


class PolizasSimulado:
    """GET /v1/suscripciones/{id}: la decisión sembrada por el estado, o 404 en formato B2.
    POST /v1/cotizaciones/{id}/aceptacion (HU-W15): lo que polizas responde en el estado sembrado.
    GET /v1/cotizaciones/{id}/recaudo (HU-W16): el estado del recaudo sembrado, o 404.
    GET /v1/cotizaciones/{id}/poliza y /v1/polizas/{id}/documento (HU-W17): la póliza o el enlace
    sembrados, o el error del estado."""

    def __init__(self) -> None:
        self.decision: dict[str, Any] | None = None
        self.aceptacion: str | None = None
        self.recaudo: str | None = None
        self.poliza: str | None = None
        self.roles: list[str] = []

    def aceptar(self, pedido: httpx.Request) -> httpx.Response:
        cotizacion_id = pedido.url.path.split("/")[3]
        cuerpo = json.loads(pedido.content)
        declaraciones = cuerpo["declaraciones"]
        match self.aceptacion:
            case "existe una decisión aprobada con la oferta vencida":
                return _error("OFERTA_VENCIDA", 409, {"accion": "NUEVA_COTIZACION"})
            case "existe una decisión rechazada":
                return _error("DECISION_NO_APROBADA", 409)
            case "la cotización ya tiene una aceptación con la misma clave":
                return httpx.Response(200, json=_aceptacion(cotizacion_id, cuerpo))
            case "existe una decisión aprobada con la oferta vigente":
                faltan = [
                    c for c in ("leidoCondiciones", "informacionVeraz") if not declaraciones[c]
                ]
                if faltan:
                    return _error("DECLARACIONES_INCOMPLETAS", 422, {"campos": faltan})
                return httpx.Response(201, json=_aceptacion(cotizacion_id, cuerpo))
        return _error("DECISION_NO_ENCONTRADA", 404)

    def responder(self, pedido: httpx.Request) -> httpx.Response:
        self.roles.append(pedido.headers.get("X-Actor-Rol", ""))
        if pedido.url.path.endswith(("/poliza", "/documento")):
            return _poliza(self.poliza or SIN_POLIZA)
        if pedido.url.path.endswith("/recaudo"):
            return _recaudo(self.recaudo or SIN_ACEPTACION)
        if pedido.url.path.endswith("/aceptacion"):
            return self.aceptar(pedido)
        decision_id = pedido.url.path.rsplit("/", 1)[-1]
        if self.decision is not None and self.decision["id"] == decision_id:
            return httpx.Response(200, json=self.decision)
        return httpx.Response(
            404,
            json={
                "codigo": "DECISION_NO_ENCONTRADA",
                "mensaje": "La decisión no existe",
                "detalles": {},
            },
        )


@contextmanager
def _servir(aplicacion: FastAPI) -> Iterator[str]:
    zocalo = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    zocalo.bind(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{zocalo.getsockname()[1]}"
    servidor = uvicorn.Server(uvicorn.Config(aplicacion, log_level="warning"))

    def correr() -> None:
        asyncio.run(servidor.serve(sockets=[zocalo]), loop_factory=asyncio.SelectorEventLoop)

    hilo = threading.Thread(target=correr, daemon=True)
    hilo.start()
    limite = time.monotonic() + 30
    while not servidor.started:
        assert hilo.is_alive(), "bff-web no arrancó (ver el log del servidor)"
        assert time.monotonic() < limite, "bff-web no arrancó a tiempo"
        time.sleep(0.05)
    try:
        yield url
    finally:
        servidor.should_exit = True
        hilo.join(timeout=15)


def _sembrar(polizas: PolizasSimulado, state: str, parametros: dict[str, Any]) -> None:
    """Siembra en polizas lo que el estado del proveedor describe; uno desconocido falla."""
    polizas.decision = None
    polizas.aceptacion = None
    polizas.recaudo = None
    polizas.poliza = None
    if state in ESTADOS_ACEPTACION:
        polizas.aceptacion = state
        return
    if state in ESTADOS_RECAUDO:
        polizas.recaudo = state
        return
    if state in ESTADOS_POLIZA:
        polizas.poliza = state
        return
    if state == NO_EXISTE:
        return
    if state != EXISTE:
        raise AssertionError(f"estado del proveedor sin sembrar: {state!r}")
    polizas.decision = _decision(str(parametros["decisionId"]), str(parametros["caso"]))


def test_bff_web_cumple_el_pacto_del_portal(monkeypatch: pytest.MonkeyPatch) -> None:
    assert PACTO.exists(), "Copiar el pacto del portal (web/pacts/portal-web-bff-web.json)"
    monkeypatch.setattr(settings, "jwt_issuers", {"backoffice": EMISOR})
    polizas = PolizasSimulado()
    clientes = httpx.MockTransport(
        lambda pedido: httpx.Response(200, json={"id": pedido.url.path.rsplit("/", 1)[-1]})
    )
    cliente_polizas = PolizasCliente(
        "http://polizas:8080", 1500, httpx.MockTransport(polizas.responder)
    )
    app.dependency_overrides[ruta.suscripciones] = lambda: Suscripciones(
        cliente_polizas, ClientesCliente("http://clientes:8080", 1500, clientes), 200
    )
    app.dependency_overrides[ruta_aceptacion.aceptaciones] = lambda: Aceptaciones(cliente_polizas)
    app.dependency_overrides[ruta_recaudo.recaudos] = lambda: Recaudos(cliente_polizas)
    app.dependency_overrides[ruta_poliza.polizas] = lambda: Polizas(cliente_polizas)
    vistos: list[tuple[str, str | None]] = []

    def estado(state: str, action: str, parameters: dict[str, Any] | None) -> None:
        """Un estado por interacción; uno desconocido falla la verificación."""
        if action != "setup":
            return
        parametros = parameters or {}
        vistos.append((state, parametros.get("caso")))
        _sembrar(polizas, state, parametros)

    try:
        with _servir(app) as url:
            Verifier("bff-web", host="127.0.0.1").add_source(PACTO).add_transport(
                url=url
            ).add_custom_headers(
                {
                    "X-Authenticated-Sub": "sub-asesor-pact",
                    "X-Authenticated-Groups": "[asesores]",
                    "X-Authenticated-Issuer": EMISOR,
                }
            ).state_handler(estado, teardown=True).verify()
    finally:
        app.dependency_overrides.clear()
    assert sorted(vistos, key=str) == sorted(
        [
            (EXISTE, "S01"),
            (EXISTE, "S02"),
            (EXISTE, "S04"),
            (EXISTE, "S06"),
            (NO_EXISTE, None),
            # HU-W15: 201, 422 (misma aprobada), 200, 409 vencida y 409 rechazada.
            (APROBADA_VIGENTE, "S01"),
            (APROBADA_VIGENTE, "S01"),
            (MISMA_CLAVE, "S01"),
            (APROBADA_VENCIDA, "S01"),
            (RECHAZADA, "S02"),
            # HU-W16: 200 PENDIENTE, CONFIRMADO y RECHAZADO, y el 404 sin aceptación.
            (RECAUDO_PENDIENTE, None),
            (RECAUDO_CONFIRMADO, None),
            (RECAUDO_RECHAZADO, None),
            (SIN_ACEPTACION, None),
            # HU-W17: la póliza EMITIENDO, ACTIVA y el 404; su documento, 200 y 409 mientras se emite.
            (POLIZA_EMITIENDO, None),
            (POLIZA_ACTIVA, None),
            (SIN_POLIZA, None),
            (DOCUMENTO_ACTIVA, None),
            (DOCUMENTO_EMITIENDO, None),
        ],
        key=str,
    )
    # El asesor pide como ASESOR: polizas filtra las reglas reservadas (DS2-07, R-08).
    assert set(polizas.roles) == {"ASESOR"}


def test_el_pacto_no_lleva_codigos_de_regla_en_la_comunicacion() -> None:
    """La comunicación que espera el portal no tiene códigos SUS-NNN (DS2-07)."""
    pacto = json.loads(PACTO.read_text(encoding="utf-8"))
    for interaccion in pacto["interactions"]:
        cuerpo = interaccion["response"].get("body", {}).get("content", {})
        comunicacion = json.dumps(cuerpo.get("comunicacion", {}))
        assert not re.search(r"SUS-\d{3}", comunicacion), interaccion["description"]


@pytest.mark.parametrize(
    ("state", "status", "estado"),
    [
        (RECAUDO_PENDIENTE, 200, "PENDIENTE"),
        (RECAUDO_CONFIRMADO, 200, "CONFIRMADO"),
        (RECAUDO_RECHAZADO, 200, "RECHAZADO"),
        (SIN_ACEPTACION, 404, None),
    ],
)
def test_los_estados_del_recaudo_responden_lo_que_promete_bff_web(
    monkeypatch: pytest.MonkeyPatch, state: str, status: int, estado: str | None
) -> None:
    """HU-W16 (16.14): los estados que usará la 16.15 ya existen y pasan por la app real (DTO,
    lista blanca y rol) antes de que el pacto los traiga."""
    monkeypatch.setattr(settings, "jwt_issuers", {"backoffice": EMISOR})
    polizas = PolizasSimulado()
    cliente_polizas = PolizasCliente(
        "http://polizas:8080", 1500, httpx.MockTransport(polizas.responder)
    )
    app.dependency_overrides[ruta_recaudo.recaudos] = lambda: Recaudos(cliente_polizas)
    _sembrar(polizas, state, {"cotizacionId": COTIZACION_RECAUDO})
    try:
        respuesta = TestClient(app).get(
            f"/v1/cotizaciones/{COTIZACION_RECAUDO}/recaudo",
            headers={
                "X-Authenticated-Sub": "sub-asesor-pact",
                "X-Authenticated-Groups": "[asesores]",
                "X-Authenticated-Issuer": EMISOR,
            },
        )
    finally:
        app.dependency_overrides.clear()
    assert respuesta.status_code == status
    cuerpo = respuesta.json()
    if estado is None:
        assert cuerpo["codigo"] == "ACEPTACION_NO_ENCONTRADA"
    else:
        assert cuerpo["estado"] == estado
        assert (cuerpo["transaccionId"] is not None) == (estado == "CONFIRMADO")
        assert (cuerpo["motivo"] is not None) == (estado == "RECHAZADO")
    assert polizas.roles == ["ASESOR"]


@pytest.mark.parametrize(
    ("state", "ruta_", "status", "esperado"),
    [
        (POLIZA_EMITIENDO, RUTA_POLIZA, 200, "EMITIENDO"),
        (POLIZA_ACTIVA, RUTA_POLIZA, 200, "ACTIVA"),
        (SIN_POLIZA, RUTA_POLIZA, 404, "POLIZA_NO_ENCONTRADA"),
        (DOCUMENTO_ACTIVA, RUTA_DOCUMENTO, 200, None),
        (DOCUMENTO_EMITIENDO, RUTA_DOCUMENTO, 409, "POLIZA_NO_ACTIVA"),
    ],
)
def test_los_estados_de_la_poliza_responden_lo_que_promete_bff_web(
    monkeypatch: pytest.MonkeyPatch, state: str, ruta_: str, status: int, esperado: str | None
) -> None:
    """HU-W17 (17.11): los estados que usará la 17.12 ya existen y pasan por la app real (DTO,
    lista blanca y rol) antes de que el pacto los traiga."""
    monkeypatch.setattr(settings, "jwt_issuers", {"backoffice": EMISOR})
    polizas = PolizasSimulado()
    cliente_polizas = PolizasCliente(
        "http://polizas:8080", 1500, httpx.MockTransport(polizas.responder)
    )
    app.dependency_overrides[ruta_poliza.polizas] = lambda: Polizas(cliente_polizas)
    _sembrar(polizas, state, {"cotizacionId": COTIZACION_RECAUDO, "polizaId": POLIZA_ID})
    try:
        respuesta = TestClient(app).get(
            ruta_,
            headers={
                "X-Authenticated-Sub": "sub-asesor-pact",
                "X-Authenticated-Groups": "[asesores]",
                "X-Authenticated-Issuer": EMISOR,
            },
        )
    finally:
        app.dependency_overrides.clear()
    assert respuesta.status_code == status
    cuerpo = respuesta.json()
    if status != 200:
        assert cuerpo["codigo"] == esperado
    elif ruta_ == RUTA_DOCUMENTO:
        assert set(cuerpo) == {"url", "venceEn"}
        assert "versionId=" in cuerpo["url"]
    else:
        assert (cuerpo["estado"], cuerpo["polizaId"]) == (esperado, POLIZA_ID)
        activa = esperado == "ACTIVA"
        assert (cuerpo["emitidaEn"] is not None, cuerpo["documento"] is not None) == (
            activa,
            activa,
        )
        assert (cuerpo["firma"] is not None) == activa
    assert polizas.roles == ["ASESOR"]
