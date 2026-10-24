"""CT-W14-01 y CT-W15-01: `bff-web` verifica como proveedor el pacto del portal
(contracts/pact/web-bff-web.json).

El pacto lo genera el portal (`proyecto-final-2-frontend/web`, `npm run test:pact`) y se copia aquí
sin editarlo. La app real de bff-web (rutas, autorización por grupo, DTO y falla cerrada) se sirve
con uvicorn en un hilo; polizas y clientes se simulan con un transporte de httpx. Cada estado del
proveedor siembra la decisión que polizas devolvería para el caso sintético (S01, S02, S04, S06):
las alternativas salen de la semilla de reglas 2026.10.1 de catalogo y la comunicación es la de
docs/hu-w14-comunicacion.md §1.1. HU-W15: los estados de la aceptación hacen que polizas responda
lo que responde en ese caso (201, 200 con la misma clave, 409 OFERTA_VENCIDA o DECISION_NO_APROBADA y
422 DECLARACIONES_INCOMPLETAS si faltan las declaraciones obligatorias). El verificador envía los
encabezados que pone API Gateway para un usuario del grupo `asesores`.
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
from bff_web.adapters.inbound import suscripciones as ruta
from bff_web.adapters.outbound.clientes import ClientesCliente
from bff_web.adapters.outbound.polizas import PolizasCliente
from bff_web.application.aceptacion import Aceptaciones
from bff_web.application.suscripcion import Suscripciones
from bff_web.config import settings
from bff_web.main import app
from fastapi import FastAPI
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


class PolizasSimulado:
    """GET /v1/suscripciones/{id}: la decisión sembrada por el estado, o 404 en formato B2.
    POST /v1/cotizaciones/{id}/aceptacion (HU-W15): lo que polizas responde en el estado sembrado."""

    def __init__(self) -> None:
        self.decision: dict[str, Any] | None = None
        self.aceptacion: str | None = None
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
    vistos: list[tuple[str, str | None]] = []

    def estado(state: str, action: str, parameters: dict[str, Any] | None) -> None:
        """Un estado por interacción; uno desconocido falla la verificación."""
        if action != "setup":
            return
        parametros = parameters or {}
        vistos.append((state, parametros.get("caso")))
        polizas.decision = None
        polizas.aceptacion = None
        if state in ESTADOS_ACEPTACION:
            polizas.aceptacion = state
            return
        if state == NO_EXISTE:
            return
        if state != EXISTE:
            raise AssertionError(f"estado del proveedor sin sembrar: {state!r}")
        polizas.decision = _decision(str(parametros["decisionId"]), str(parametros["caso"]))

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
