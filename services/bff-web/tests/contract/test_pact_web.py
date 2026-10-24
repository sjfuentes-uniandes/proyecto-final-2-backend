"""CT-W14-01: `bff-web` verifica como proveedor el pacto del portal (contracts/pact/web-bff-web.json).

El pacto lo genera el portal (`proyecto-final-2-frontend/web`, `npm run test:pact`) y se copia aquí
sin editarlo. La app real de bff-web (rutas, autorización por grupo, DTO y falla cerrada) se sirve
con uvicorn en un hilo; polizas y clientes se simulan con un transporte de httpx. Cada estado del
proveedor siembra la decisión que polizas devolvería para el caso sintético (S01, S02, S04, S06):
las alternativas salen de la semilla de reglas 2026.10.1 de catalogo y la comunicación es la de
docs/hu-w14-comunicacion.md §1.1. El verificador envía los encabezados que pone API Gateway para un
usuario del grupo `asesores`.
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
from bff_web.adapters.inbound import suscripciones as ruta
from bff_web.adapters.outbound.clientes import ClientesCliente
from bff_web.adapters.outbound.polizas import PolizasCliente
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


class PolizasSimulado:
    """GET /v1/suscripciones/{id}: la decisión sembrada por el estado, o 404 en formato B2."""

    def __init__(self) -> None:
        self.decision: dict[str, Any] | None = None
        self.roles: list[str] = []

    def responder(self, pedido: httpx.Request) -> httpx.Response:
        self.roles.append(pedido.headers.get("X-Actor-Rol", ""))
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
    app.dependency_overrides[ruta.suscripciones] = lambda: Suscripciones(
        PolizasCliente("http://polizas:8080", 1500, httpx.MockTransport(polizas.responder)),
        ClientesCliente("http://clientes:8080", 1500, clientes),
        200,
    )
    vistos: list[tuple[str, str | None]] = []

    def estado(state: str, action: str, parameters: dict[str, Any] | None) -> None:
        """Un estado por interacción; uno desconocido falla la verificación."""
        if action != "setup":
            return
        parametros = parameters or {}
        vistos.append((state, parametros.get("caso")))
        polizas.decision = None
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
        [(EXISTE, "S01"), (EXISTE, "S02"), (EXISTE, "S04"), (EXISTE, "S06"), (NO_EXISTE, None)],
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
