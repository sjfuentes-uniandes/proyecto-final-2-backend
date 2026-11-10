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
portal en la 17.12. HU-W18: el pacto trae también el listado y el estado de las pólizas (consulta
simulada) y HU-W19 (CT-W19-02): la bandeja, el detalle con su 404 y su 403, y el enlace de una evidencia
con su 404 EVIDENCIA_NO_DISPONIBLE (siniestros, clientes y consulta simulados). HU-W23 (CT-W23-01):
el pago de la indemnización (H08) confirmado, rechazado y pendiente, su 404 INDEMNIZACION_NO_ENCONTRADA
y su 403 (el detalle de siniestros se simula con el caso APROBADO y su `pago`). El verificador envía
los encabezados que pone API Gateway para un usuario de los grupos `asesores` y
`analistas-siniestros`; el estado del 403 hace que la ruta exija otro grupo mientras dura.
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
from bff_web.adapters.inbound import estado_polizas as ruta_estado
from bff_web.adapters.inbound import polizas as ruta_poliza
from bff_web.adapters.inbound import recaudos as ruta_recaudo
from bff_web.adapters.inbound import siniestros as ruta_siniestros
from bff_web.adapters.inbound import suscripciones as ruta
from bff_web.adapters.outbound.clientes import ClientesCliente
from bff_web.adapters.outbound.consulta import ConsultaCliente
from bff_web.adapters.outbound.polizas import PolizasCliente
from bff_web.adapters.outbound.siniestros import SiniestrosCliente
from bff_web.application.aceptacion import Aceptaciones
from bff_web.application.estado_poliza import EstadoPolizas
from bff_web.application.poliza import Polizas
from bff_web.application.recaudo import Recaudos
from bff_web.application.siniestros import Siniestros
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
# 17.11c: el titular de la póliza y lo que clientes responde para él (`ClienteLecturaDto`,
# clientes.yaml) en el estado de la póliza activa; de ahí sale `titularNombre`.
TITULAR_ID = "00000000-0000-4000-8000-000000000001"
TITULAR_NOMBRE = "Natalia González Ruiz"
TITULAR = {
    "clienteId": TITULAR_ID,
    "estado": "VERIFICADO",
    "verificadoEn": "2026-10-20T14:00:00Z",
    "proveedor": "KYC",
    "referencia": "kyc-ref-001",
    "nombreCompleto": TITULAR_NOMBRE,
}
# HU-W18: las interacciones de la consulta de pólizas que agrega el portal.
POLIZAS_PROYECTADAS = "existen pólizas proyectadas para el asesor"
POLIZA_PROYECTADA = "existe la póliza proyectada"
POLIZA_NO_PROYECTADA = "la póliza no existe, es ajena o aún no se proyecta"
ESTADOS_CONSULTA = {POLIZAS_PROYECTADAS, POLIZA_PROYECTADA, POLIZA_NO_PROYECTADA}
# HU-W19 (CT-W19-02): siniestros del analista (web/pact/siniestros.pact.spec.ts).
ANALISTA_CON_CASOS = "el analista tiene siniestros asignados"
EXISTE_SINIESTRO = "existe el siniestro con sus evidencias"
NO_EXISTE_SINIESTRO = "no existe el siniestro"
SIN_GRUPO_ANALISTAS = "el usuario no pertenece al grupo analistas-siniestros"
EVIDENCIA_CON_ARCHIVO = "existe la evidencia del siniestro con su archivo"
EVIDENCIA_SIN_ARCHIVO = "la evidencia existe pero su archivo no está disponible"
# HU-W20 (CT-W20-01): la decisión del analista (web/pact/siniestros.pact.spec.ts).
PUEDE_DECIDIR = "el analista puede decidir siniestros"
EN_EVALUACION_ASIGNADO = "el analista tiene un siniestro EN_EVALUACION asignado"
YA_APROBADO = "el siniestro ya fue aprobado"
# HU-W23 (CT-W23-01): el pago de la indemnización, H08 (web/pact/siniestros.pact.spec.ts).
PAGO_CONFIRMADO = "el siniestro está aprobado y su pago fue confirmado"
PAGO_RECHAZADO = "el siniestro está aprobado y su pago fue rechazado por la pasarela"
PAGO_PENDIENTE = "el siniestro está aprobado y su pago aún no tiene resultado"
NO_APROBADO = "el siniestro existe pero no está aprobado"
ESTADOS_PAGO = {PAGO_CONFIRMADO, PAGO_RECHAZADO, PAGO_PENDIENTE, NO_APROBADO}
ESTADOS_SINIESTROS = {
    ANALISTA_CON_CASOS,
    EXISTE_SINIESTRO,
    NO_EXISTE_SINIESTRO,
    SIN_GRUPO_ANALISTAS,
    EVIDENCIA_CON_ARCHIVO,
    EVIDENCIA_SIN_ARCHIVO,
    PUEDE_DECIDIR,
    EN_EVALUACION_ASIGNADO,
    YA_APROBADO,
    *ESTADOS_PAGO,
}
GRUPO_SIN_MIEMBROS = "grupo-sin-miembros"
SINIESTRO_ID = "5e000000-0000-4000-8000-000004000000"
EVIDENCIA_ID = "e0000000-0000-4000-8000-000004000001"
ANALISTA_ID = "a0a0a0a0-0000-4000-8000-000000000001"
CLIENTE_SINIESTRO = "00000000-0000-4000-8000-000000000001"
POLIZA_SINIESTRO = "a0000000-0000-4000-8000-000004000000"
RESUMEN_SINIESTRO: dict[str, Any] = {
    "id": SINIESTRO_ID,
    "codigo": "88-2026-0442",
    "polizaId": POLIZA_SINIESTRO,
    "polizaNumero": "220-2026-011294",
    "clienteId": CLIENTE_SINIESTRO,
    "ramo": "Dispositivos",
    "origen": "MOVIL",
    "estado": "EN_EVALUACION",
    "prioridad": "BAJA",
    "reportadoEn": "2026-11-05T01:00:00Z",
    "analistaId": ANALISTA_ID,
    "tiempoRestanteMin": 2040,
    "relojPausado": False,
}
DETALLE_SINIESTRO: dict[str, Any] = {
    **RESUMEN_SINIESTRO,
    "causa": "DANO_ACCIDENTAL",
    "fechaEvento": "2026-11-05T00:19:00Z",
    "ubicacionDeclarada": {"descripcion": "Bogotá D.C.", "lat": 4.652, "lon": -74.0555},
    "poliza": {
        "numero": "220-2026-011294",
        "ramo": "Dispositivos",
        "vigencia": {"desde": "2026-05-09", "hasta": "2027-05-09"},
        "amparos": [
            {
                "codigo": "DANO_ACCIDENTAL",
                "nombre": "Daño accidental",
                "sumaAsegurada": "3000000.00",
            }
        ],
        "moneda": "COP",
        "carenciaDias": 30,
        "plazoAvisoDias": 30,
        "primasAlDia": True,
        "beneficiario": {"beneficiarioRef": "ben-sint-004", "cuentaMascarada": "****1004"},
    },
    "verificacionCobertura": [
        {"codigo": "VIGENCIA", "resultado": "CUMPLE", "detalle": {"fechaEvento": "2026-11-04"}}
    ],
    "coherencia": {
        "fechaDeclarada": "2026-11-05T00:19:00Z",
        "primeraCaptura": "2026-11-05T00:53:00Z",
        "diferenciaMin": 34,
        "ubicacionCoincide": True,
        "archivosAlterados": 0,
        "resultado": "COHERENTE",
    },
    "evidencias": [
        {
            "id": EVIDENCIA_ID,
            "nombreArchivo": "evidencia_dano_01.jpg",
            "tipo": "FOTOGRAFIA",
            "capturadaEn": "2026-11-05T00:53:00Z",
            "ubicacion": {"descripcion": None, "lat": 4.6512, "lon": -74.056},
            "integridad": "VERIFICADA",
        }
    ],
    "soportesPendientes": [{"tipo": "CERTIFICADO_REPARACION", "estado": "FALTA"}],
    # El pacto fija la forma de cada elemento con el ejemplo del portal (actorNombre y ocurridoEn
    # con valor), por eso el estado siembra un solo paso: el ACTUAL (ver bitácora R-54).
    "tramite": [
        {
            "paso": "EN_EVALUACION",
            "estado": "ACTUAL",
            "ocurridoEn": "2026-11-05T03:00:00Z",
            "actorNombre": "Luis Mora",
        }
    ],
    "historial": [
        {
            "id": "00000000-0000-4000-8000-0000000000a3",
            "accion": "ASIGNADO",
            "estadoAnterior": "REPORTADO",
            "estadoNuevo": "EN_EVALUACION",
            "actorId": ANALISTA_ID,
            "actorRol": "ANALISTA_SINIESTROS",
            "actorNombre": "Luis Mora",
            "justificacion": None,
            "ocurridoEn": "2026-11-05T03:00:00Z",
        }
    ],
    "liquidacion": None,
    "evento": None,
    "pago": None,
    # HU-W20 (R-63): campos aditivos del detalle.
    "valorReclamado": None,
    "referenciaPagoPrevista": "IND-88-2026-0442-01",
    "plazoSoporteVenceEn": None,
}
CATALOGOS_DECISION: dict[str, Any] = {
    "causales": [
        "POLIZA_NO_VIGENTE",
        "EVENTO_NO_CUBIERTO",
        "PERIODO_DE_CARENCIA",
        "AVISO_EXTEMPORANEO",
        "PRIMAS_EN_MORA",
        "EVIDENCIA_INCONSISTENTE",
        "EXCLUSION_CONTRACTUAL",
    ],
    "documentos": [
        "CERTIFICADO_REPARACION",
        "COTIZACION_REPUESTOS",
        "DENUNCIA_AUTORIDAD",
        "PERITAJE",
    ],
    "canales": ["PUSH_Y_CORREO", "PUSH", "CORREO"],
    "plazosDiasHabiles": [5, 10, 15],
}


def _detalle_aprobado(estado: str) -> dict[str, Any]:
    """El detalle de un caso APROBADO como lo entrega siniestros (HU-W23): la liquidación, la entrada
    `APROBADO` del historial y el `pago` derivado o guardado desde los eventos `Indemnizacion*`."""
    referencia = "IND-88-2026-0442-01"
    aprobado = {
        "id": "00000000-0000-4000-8000-0000000000a4",
        "accion": "APROBADO",
        "estadoAnterior": "EN_EVALUACION",
        "estadoNuevo": "APROBADO",
        "actorId": ANALISTA_ID,
        "actorRol": "ANALISTA_SINIESTROS",
        "actorNombre": "Luis Mora",
        "justificacion": "Daño verificado",
        "ocurridoEn": "2026-11-05T18:10:00Z",
    }
    resultado = {
        "id": "00000000-0000-4000-8000-0000000000a5",
        "accion": "PAGO_CONFIRMADO" if estado == PAGO_CONFIRMADO else "PAGO_RECHAZADO",
        "estadoAnterior": None,
        "estadoNuevo": None,
        "actorId": None,
        "actorRol": "SISTEMA_PAGOS",
        "actorNombre": None,
        "justificacion": None,
        "ocurridoEn": "2026-11-05T18:12:00Z",
    }
    pago: dict[str, Any] = {
        "estado": "PENDIENTE",
        "referencia": referencia,
        "transaccionId": None,
        "motivo": None,
        "intentos": 0,
        "duplicadosDescartados": 0,
    }
    historial = [*DETALLE_SINIESTRO["historial"], aprobado]
    if estado == PAGO_CONFIRMADO:
        pago = {**pago, "estado": "CONFIRMADO", "transaccionId": "TRX-SINT-0442", "intentos": 1}
        historial.append(resultado)
    elif estado == PAGO_RECHAZADO:
        pago = {
            **pago,
            "estado": "RECHAZADO",
            "motivo": "BENEFICIARIO_NO_HABILITADO",
            "intentos": 1,
        }
        historial.append(resultado)
    return {
        **DETALLE_SINIESTRO,
        "estado": "APROBADO",
        "valorReclamado": "2850000.00",
        "liquidacion": {
            "moneda": "COP",
            "valorReclamado": "2850000.00",
            "deducible": "350000.00",
            "valorIndemnizar": "2500000.00",
            "referenciaPago": referencia,
            "aprobadaEn": "2026-11-05T18:10:00Z",
        },
        "historial": historial,
        "pago": pago,
    }


# Lo que responde siniestros a cada decisión (el cuerpo de ResultadoDecisionSiniestro).
RESULTADO_DECISION: dict[str, Any] = {
    "siniestroId": SINIESTRO_ID,
    "decisionId": "d0000000-0000-4000-8000-000000000001",
    "registradaEn": "2026-11-05T15:00:00Z",
}
RESULTADOS_POR_TIPO: dict[str, dict[str, Any]] = {
    "APROBAR": {
        "estadoNuevo": "APROBADO",
        "referenciaPago": "IND-88-2026-0442-01",
        "plazoSoporteVenceEn": None,
    },
    "RECHAZAR": {"estadoNuevo": "RECHAZADO", "referenciaPago": None, "plazoSoporteVenceEn": None},
    "SOLICITAR_SOPORTE": {
        "estadoNuevo": "FALTA_SOPORTE",
        "referenciaPago": None,
        "plazoSoporteVenceEn": "2026-11-19T15:00:00Z",
    },
}
ENLACE_SINIESTRO = {
    "url": "https://evidencias.s3.amazonaws.com/o?X-Amz-Expires=300&X-Amz-Signature=sintetica",
    "expiraEn": "2026-11-05T15:05:00Z",
    "tipoContenido": "image/jpeg",
}
POLIZA_CONSULTA: dict[str, Any] = {
    "polizaId": POLIZA_SINIESTRO,
    "numero": "220-2026-011294",
    "estado": "VIGENTE",
    "productoId": "dispositivos",
    "productoVersion": "1",
    "productoNombre": "Dispositivos protegidos",
    "ramo": "Dispositivos",
    "vigencia": {"desde": "2026-05-09", "hasta": "2027-05-09"},
    "vigente": True,
    "prima": "10000.00",
    "sumaAsegurada": "3000000.00",
    "moneda": "COP",
    "periodicidad": "MENSUAL",
    "coberturas": [],
    "documentoHuella": "h" * 64,
    "firmaId": "FIR-SINT",
    "emitidaEn": "2026-05-09T10:00:00Z",
}
ESTADO_CONSULTA: dict[str, Any] = {
    "polizaId": "00000000-0000-4000-b000-000000000001",
    "numero": "100-2026-000001",
    "estado": "VIGENTE",
    "fechaActualizacion": "2026-10-24T15:00:05.250000Z",
    "producto": "Vida hipotecario",
    "ramo": "Vida",
    "vigencia": {"desde": "2026-10-25", "hasta": "2027-10-24"},
    "prima": "64638.10",
    "sumaAsegurada": "180000000",
    "moneda": "COP",
    "periodicidad": "MENSUAL",
    "emitidaEn": "2026-10-24T15:00:03.500000Z",
    "coberturas": [{"codigo": "MUERTE", "nombre": "Muerte", "obligatoria": True}],
    "clienteId": TITULAR_ID,
}
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
        "clienteId": TITULAR_ID,
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


class ClientesSimulado:
    """GET /v1/clientes/{id}: el titular sembrado (17.11c) o, para cualquier otro cliente, una
    lectura sin nombre (el `clienteNombre` de la suscripción sale null, como antes)."""

    def __init__(self) -> None:
        self.titular: dict[str, Any] | None = None
        self.pedidos: list[str] = []

    def responder(self, pedido: httpx.Request) -> httpx.Response:
        cliente_id = pedido.url.path.rsplit("/", 1)[-1]
        self.pedidos.append(cliente_id)
        if self.titular is not None and self.titular["clienteId"] == cliente_id:
            return httpx.Response(200, json=self.titular)
        return httpx.Response(200, json={"id": cliente_id})


class SiniestrosSimulado:
    """siniestros (HU-W19): la bandeja, el detalle y el enlace según el estado sembrado, o el 404 del
    contrato. Acepta el aviso de acceso denegado (DS3-09) y guarda cada actor y rol recibidos."""

    def __init__(self) -> None:
        self.estado: str | None = None
        self.actores: list[tuple[str, str]] = []
        self.avisos = 0
        # HU-W20: cada decisión que llega (tipo y Idempotency-Key) y los GET de catálogos.
        self.decisiones: list[tuple[str, str]] = []
        self.catalogos = 0

    def _decidir(self, pedido: httpx.Request) -> httpx.Response:
        cuerpo = json.loads(pedido.content)
        self.decisiones.append((cuerpo["tipo"], pedido.headers.get("Idempotency-Key", "")))
        if self.estado == YA_APROBADO:
            return _error("TRANSICION_NO_PERMITIDA", 409, {"estadoActual": "APROBADO"})
        if self.estado == EN_EVALUACION_ASIGNADO:
            return httpx.Response(
                200, json={**RESULTADO_DECISION, **RESULTADOS_POR_TIPO[cuerpo["tipo"]]}
            )
        return _error("SINIESTRO_NO_ENCONTRADO", 404)

    def responder(self, pedido: httpx.Request) -> httpx.Response:
        ruta_ = pedido.url.path
        if ruta_.endswith("/accesos-denegados"):
            self.avisos += 1
            return httpx.Response(202)
        self.actores.append(
            (pedido.headers.get("X-Actor-Id", ""), pedido.headers.get("X-Actor-Rol", ""))
        )
        if ruta_.endswith("/enlace"):
            if self.estado == EVIDENCIA_CON_ARCHIVO:
                return httpx.Response(200, json=ENLACE_SINIESTRO)
            if self.estado == EVIDENCIA_SIN_ARCHIVO:
                return _error("EVIDENCIA_NO_DISPONIBLE", 404)
            return _error("SINIESTRO_NO_ENCONTRADO", 404)
        if ruta_ == "/v1/siniestros/catalogos-decision":
            self.catalogos += 1
            if self.estado == PUEDE_DECIDIR:
                return httpx.Response(200, json=CATALOGOS_DECISION)
            return _error("SOLICITUD_INVALIDA", 422)
        if ruta_.endswith("/decision") and pedido.method == "POST":
            return self._decidir(pedido)
        if ruta_ == "/v1/siniestros":
            cuerpo = {"items": [RESUMEN_SINIESTRO], "siguienteCursor": None, "total": 1}
            return httpx.Response(200, json=cuerpo)
        if self.estado in (PAGO_CONFIRMADO, PAGO_RECHAZADO, PAGO_PENDIENTE) and ruta_.endswith(
            SINIESTRO_ID
        ):
            return httpx.Response(200, json=_detalle_aprobado(self.estado))
        # Un caso que existe pero no está aprobado no tiene pago ordenado (H08: 404 del BFF).
        if self.estado in (EXISTE_SINIESTRO, NO_APROBADO) and ruta_.endswith(SINIESTRO_ID):
            return httpx.Response(200, json=DETALLE_SINIESTRO)
        return _error("SINIESTRO_NO_ENCONTRADO", 404)


class ConsultaSimulado:
    """consulta: la póliza de un cliente (encabezado de siniestros, HU-W19) y, de HU-W18, el
    listado y el estado de las pólizas proyectadas según el estado sembrado."""

    def __init__(self) -> None:
        self.estado: str | None = None
        self.parametros: dict[str, Any] = {}

    def responder(self, pedido: httpx.Request) -> httpx.Response:
        ruta_ = pedido.url.path
        if ruta_.startswith("/v1/clientes/"):
            return httpx.Response(200, json=POLIZA_CONSULTA)
        if ruta_ == "/v1/polizas" and self.estado == POLIZAS_PROYECTADAS:
            resumen = {
                "polizaId": ESTADO_CONSULTA["polizaId"],
                "numero": ESTADO_CONSULTA["numero"],
                "ramo": ESTADO_CONSULTA["ramo"],
                "producto": ESTADO_CONSULTA["producto"],
                "prima": ESTADO_CONSULTA["prima"],
                "moneda": "COP",
                "vigenciaHasta": ESTADO_CONSULTA["vigencia"]["hasta"],
                "estado": "VIGENTE",
                "fechaActualizacion": ESTADO_CONSULTA["fechaActualizacion"],
            }
            return httpx.Response(
                200,
                json={
                    "items": [resumen],
                    "total": 1,
                    "pagina": 1,
                    "tamano": 20,
                    "actualizadoEn": ESTADO_CONSULTA["fechaActualizacion"],
                },
            )
        if ruta_.endswith("/estado") and self.estado == POLIZA_PROYECTADA:
            cuerpo = {**ESTADO_CONSULTA, "polizaId": ruta_.split("/")[-2]}
            return httpx.Response(200, json={**cuerpo, "estado": self.parametros["estado"]})
        return _error("POLIZA_NO_ENCONTRADA", 404)


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


def _sembrar(
    polizas: PolizasSimulado, clientes: ClientesSimulado, state: str, parametros: dict[str, Any]
) -> None:
    """Siembra en polizas (y en clientes, el titular de la póliza activa) lo que el estado del
    proveedor describe; uno desconocido falla."""
    polizas.decision = None
    polizas.aceptacion = None
    polizas.recaudo = None
    polizas.poliza = None
    clientes.titular = None
    if state in ESTADOS_ACEPTACION:
        polizas.aceptacion = state
        return
    if state in ESTADOS_RECAUDO:
        polizas.recaudo = state
        return
    if state in ESTADOS_POLIZA:
        polizas.poliza = state
        if state == POLIZA_ACTIVA:
            clientes.titular = TITULAR
        return
    if state == NO_EXISTE:
        return
    if state != EXISTE:
        raise AssertionError(f"estado del proveedor sin sembrar: {state!r}")
    polizas.decision = _decision(str(parametros["decisionId"]), str(parametros["caso"]))


def _sembrar_siniestros_y_consulta(
    siniestros: SiniestrosSimulado,
    consulta: ConsultaSimulado,
    clientes: ClientesSimulado,
    state: str,
    parametros: dict[str, Any],
) -> bool:
    """Siembra los estados de HU-W18 y HU-W19; devuelve False si el estado es de otra historia."""
    siniestros.estado = None
    consulta.estado = None
    clientes.titular = None
    if state in ESTADOS_SINIESTROS:
        siniestros.estado = state
        # El nombre del cliente sale de `clientes` (CF-S3-09), no de siniestros.
        clientes.titular = TITULAR
        return True
    if state in ESTADOS_CONSULTA:
        consulta.estado = state
        consulta.parametros = parametros
        return True
    return False


def test_bff_web_cumple_el_pacto_del_portal(monkeypatch: pytest.MonkeyPatch) -> None:
    assert PACTO.exists(), "Copiar el pacto del portal (web/pacts/portal-web-bff-web.json)"
    monkeypatch.setattr(settings, "jwt_issuers", {"backoffice": EMISOR})
    polizas = PolizasSimulado()
    clientes = ClientesSimulado()
    cliente_polizas = PolizasCliente(
        "http://polizas:8080", 1500, httpx.MockTransport(polizas.responder)
    )
    cliente_clientes = ClientesCliente(
        "http://clientes:8080", 1500, httpx.MockTransport(clientes.responder)
    )
    app.dependency_overrides[ruta.suscripciones] = lambda: Suscripciones(
        cliente_polizas, cliente_clientes, 200
    )
    app.dependency_overrides[ruta_aceptacion.aceptaciones] = lambda: Aceptaciones(cliente_polizas)
    app.dependency_overrides[ruta_recaudo.recaudos] = lambda: Recaudos(cliente_polizas)
    app.dependency_overrides[ruta_poliza.polizas] = lambda: Polizas(
        cliente_polizas, cliente_clientes, 200
    )
    siniestros = SiniestrosSimulado()
    consulta = ConsultaSimulado()
    cliente_consulta = ConsultaCliente(
        "http://consulta:8080", 700, httpx.MockTransport(consulta.responder)
    )
    app.dependency_overrides[ruta_siniestros.siniestros_servicio] = lambda: Siniestros(
        SiniestrosCliente(
            "http://siniestros:8080", 1500, httpx.MockTransport(siniestros.responder)
        ),
        cliente_clientes,
        cliente_consulta,
        500,
        700,
        300,
    )
    app.dependency_overrides[ruta_estado.estados] = lambda: EstadoPolizas(
        cliente_consulta, cliente_clientes, 500
    )
    grupo_original = settings.grupo_analistas_siniestros
    vistos: list[tuple[str, str | None]] = []

    def estado(state: str, action: str, parameters: dict[str, Any] | None) -> None:
        """Un estado por interacción; uno desconocido falla la verificación."""
        if action != "setup":
            # El 403 de siniestros exige otro grupo solo mientras dura su estado.
            settings.grupo_analistas_siniestros = grupo_original
            return
        parametros = parameters or {}
        vistos.append((state, parametros.get("caso")))
        if state == SIN_GRUPO_ANALISTAS:
            settings.grupo_analistas_siniestros = GRUPO_SIN_MIEMBROS
        if _sembrar_siniestros_y_consulta(siniestros, consulta, clientes, state, parametros):
            return
        _sembrar(polizas, clientes, state, parametros)

    try:
        with _servir(app) as url:
            Verifier("bff-web", host="127.0.0.1").add_source(PACTO).add_transport(
                url=url
            ).add_custom_headers(
                {
                    "X-Authenticated-Sub": "sub-asesor-pact",
                    "X-Authenticated-Groups": "[asesores, analistas-siniestros]",
                    "X-Authenticated-Issuer": EMISOR,
                }
            ).state_handler(estado, teardown=True).verify()
    finally:
        app.dependency_overrides.clear()
        settings.grupo_analistas_siniestros = grupo_original
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
            # HU-W18: el listado, el estado de una póliza y su 404.
            (POLIZAS_PROYECTADAS, None),
            (POLIZA_PROYECTADA, None),
            (POLIZA_NO_PROYECTADA, None),
            # HU-W19: la bandeja, el detalle (200, 404 y 403) y el enlace (200 y 404).
            (ANALISTA_CON_CASOS, None),
            (EXISTE_SINIESTRO, None),
            (NO_EXISTE_SINIESTRO, None),
            (SIN_GRUPO_ANALISTAS, None),
            (EVIDENCIA_CON_ARCHIVO, None),
            (EVIDENCIA_SIN_ARCHIVO, None),
            # HU-W20: los catálogos, las tres decisiones y el 409 de un caso ya decidido.
            (PUEDE_DECIDIR, None),
            (EN_EVALUACION_ASIGNADO, None),
            (EN_EVALUACION_ASIGNADO, None),
            (EN_EVALUACION_ASIGNADO, None),
            (YA_APROBADO, None),
            # HU-W23: el pago confirmado, rechazado y pendiente, el 404 de un caso sin aprobar y el 403.
            (PAGO_CONFIRMADO, None),
            (PAGO_RECHAZADO, None),
            (PAGO_PENDIENTE, None),
            (NO_APROBADO, None),
            (SIN_GRUPO_ANALISTAS, None),
        ],
        key=str,
    )
    # siniestros recibe siempre al analista autenticado con el rol del servicio (DS3-10).
    assert {rol for _, rol in siniestros.actores} == {"ANALISTA_SINIESTROS"}
    # Los dos 403 por grupo (el detalle y el pago) no llegan a siniestros, pero el BFF le avisa de cada
    # acceso denegado (DS3-09).
    assert siniestros.avisos == 2
    assert {actor for actor, _ in siniestros.actores} <= {"sub-asesor-pact"}
    # HU-W20: siniestros recibió las tres decisiones y el intento sobre el caso ya aprobado, cada una con
    # la Idempotency-Key del portal, y los catálogos una sola vez.
    assert sorted(tipo for tipo, _ in siniestros.decisiones) == [
        "APROBAR",
        "APROBAR",
        "RECHAZAR",
        "SOLICITAR_SOPORTE",
    ]
    assert {clave for _, clave in siniestros.decisiones} == {"c0c0c0c0-0000-4000-8000-000000000001"}
    assert siniestros.catalogos == 1
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
    _sembrar(polizas, ClientesSimulado(), state, {"cotizacionId": COTIZACION_RECAUDO})
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
    lista blanca y rol) antes de que el pacto los traiga. 17.11c: la póliza activa, con el nombre
    del titular sembrado en clientes, para que la 17.12b pueda pactar `titularNombre`."""
    monkeypatch.setattr(settings, "jwt_issuers", {"backoffice": EMISOR})
    polizas = PolizasSimulado()
    clientes = ClientesSimulado()
    cliente_polizas = PolizasCliente(
        "http://polizas:8080", 1500, httpx.MockTransport(polizas.responder)
    )
    cliente_clientes = ClientesCliente(
        "http://clientes:8080", 1500, httpx.MockTransport(clientes.responder)
    )
    app.dependency_overrides[ruta_poliza.polizas] = lambda: Polizas(
        cliente_polizas, cliente_clientes, 200
    )
    _sembrar(polizas, clientes, state, {"cotizacionId": COTIZACION_RECAUDO, "polizaId": POLIZA_ID})
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
        # El nombre del titular solo con la póliza activa; el `clienteId` no llega al portal.
        assert cuerpo["titularNombre"] == (TITULAR_NOMBRE if activa else None)
        assert "clienteId" not in cuerpo
        assert clientes.pedidos == ([TITULAR_ID] if activa else [])
    assert polizas.roles == ["ASESOR"]
