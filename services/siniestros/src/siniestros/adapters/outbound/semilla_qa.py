"""Semilla sintética de QA de siniestros (HU-W19, DS3-07). Solo local e INT: la enciende
`SOLVENTA_SEMILLA_SINIESTROS_QA=true` y está apagada por omisión. Nunca en producción.

Inserta los 8 casos de `semillas/siniestros_r7.json` (copia de
`contracts/datos-sinteticos/siniestros-r7.json`) y sube sus objetos a
`s3://<bucket>/siniestros/<siniestroId>/<evidenciaId>`:

- **Idempotente.** `ON CONFLICT DO NOTHING` en cada fila; los instantes y las fechas son relativos al
  momento de sembrar, así que un caso ya sembrado conserva los suyos.
- **Objetos.** Cada `PutObject` lleva `ChecksumSHA256` y `ServerSideEncryption =
  evidencias_bucket_sse` (la política del bucket rechaza cualquier otro). El objeto «ausente» no se
  sube y el «alterado» se sube con otro contenido que el registrado. Solo se escribe bajo
  `siniestros/`: nunca en `tmp/` ni en `telemetria/`.
- **Analista de QA.** El del archivo (`Luis Mora`) o, en INT, el `sub` real del usuario de prueba
  (`SOLVENTA_SEMILLA_SINIESTROS_ANALISTA_ID`).
- Datos 100 % sintéticos; sin nombres de clientes.
"""

import asyncio
import base64
import json
import logging
from datetime import date, datetime, timedelta
from importlib import resources
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from siniestros.adapters.outbound.mapeo import copia_a_json
from siniestros.domain.siniestro.modelos import (
    Amparo,
    Beneficiario,
    CopiaPoliza,
    Vigencia,
)

logger = logging.getLogger("siniestros.semilla")

PREFIJO_OBJETOS = "siniestros"
MINUTO = timedelta(minutes=1)

_SINIESTRO = (
    "INSERT INTO siniestros (id, codigo, origen, estado, poliza_id, cliente_id, copia_poliza, "
    "causa, fecha_evento, ubicacion_declarada, reportado_en, analista_id, asignado_en, "
    "correlation_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
    "ON CONFLICT (id) DO NOTHING"
)
_EVIDENCIA = (
    "INSERT INTO evidencias (id, siniestro_id, nombre_archivo, tipo, soporte_tipo, clave_objeto, "
    "sha256, capturada_en, ubicacion, tamano_bytes, tipo_contenido) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING"
)
_HISTORIAL = (
    "INSERT INTO historial_siniestro (id, siniestro_id, accion, estado_anterior, estado_nuevo, "
    "actor_id, actor_rol, actor_nombre, ocurrido_en) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING"
)


def cargar_semilla() -> dict[str, Any]:
    texto = (
        resources.files("siniestros.semillas")
        .joinpath("siniestros_r7.json")
        .read_text(encoding="utf-8")
    )
    semilla: dict[str, Any] = json.loads(texto)
    return semilla


def clave_objeto(siniestro_id: UUID | str, evidencia_id: UUID | str) -> str:
    return f"{PREFIJO_OBJETOS}/{siniestro_id}/{evidencia_id}"


def copia_de_semilla(poliza: dict[str, Any], hoy: date) -> CopiaPoliza:
    return CopiaPoliza(
        numero=poliza["numero"],
        ramo=poliza["ramo"]["nombre"],
        moneda=poliza["moneda"],
        amparos=tuple(
            Amparo(a["codigo"], a["nombre"], a["sumaAsegurada"]) for a in poliza["amparos"]
        ),
        vigencia=Vigencia(
            hoy - timedelta(days=poliza["vigenciaDesdeHaceDias"]),
            hoy + timedelta(days=poliza["vigenciaHastaEnDias"]),
        ),
        carencia_dias=poliza["carenciaDias"],
        plazo_aviso_dias=poliza["plazoAvisoDias"],
        primas_al_dia=poliza["primasAlDia"],
        beneficiario=Beneficiario(
            poliza["beneficiario"]["beneficiarioRef"], poliza["beneficiario"]["cuentaMascarada"]
        ),
    )


def _instante(base: datetime, minutos: int) -> datetime:
    return base + minutos * MINUTO


def filas_del_caso(
    caso: dict[str, Any],
    objetos: dict[str, Any],
    ahora: datetime,
    analista_id: UUID,
) -> dict[str, list[tuple[Any, ...]]]:
    """Las filas de un caso (siniestro, evidencias e historial), con sus instantes relativos a
    `ahora`."""
    reportado = ahora - caso["reportadoHaceMin"] * MINUTO
    asignado = next(
        (
            _instante(reportado, h["minDespuesDeReporte"])
            for h in caso["historial"]
            if h["accion"] == "ASIGNADO"
        ),
        None,
    )
    analista = analista_id if caso["estado"] == "EN_EVALUACION" else None
    siniestro_id = UUID(caso["id"])
    copia = copia_de_semilla(caso["poliza"], ahora.date())
    fila_siniestro = (
        siniestro_id,
        caso["codigo"],
        caso["origen"],
        caso["estado"],
        UUID(caso["poliza"]["polizaId"]),
        UUID(caso["clienteId"]),
        Jsonb(copia_a_json(copia)),
        caso["causa"],
        reportado - caso["fechaEventoMinAntesDeReporte"] * MINUTO,
        Jsonb(caso["ubicacionDeclarada"]),
        reportado,
        analista,
        asignado if analista else None,
        "semilla-qa",
    )
    evidencias = []
    for e in caso["evidencias"]:
        objeto = objetos[e["objetoRegistrado"]]
        evidencias.append(
            (
                UUID(e["id"]),
                siniestro_id,
                e["nombreArchivo"],
                e["tipo"],
                e["soporteTipo"],
                clave_objeto(siniestro_id, e["id"]),
                objeto["sha256"],
                reportado - e["capturadaMinAntesDeReporte"] * MINUTO,
                Jsonb(e["ubicacion"]),
                objeto["tamanoBytes"],
                objeto["tipoContenido"],
            )
        )
    historial = []
    for orden, h in enumerate(caso["historial"]):
        actor = str(analista_id) if h["accion"] == "ASIGNADO" else h["actorId"]
        historial.append(
            (
                uuid5(NAMESPACE_URL, f"siniestros-semilla/{siniestro_id}/{orden}"),
                siniestro_id,
                h["accion"],
                h["estadoAnterior"],
                h["estadoNuevo"],
                actor,
                h["actorRol"],
                h["actorNombre"],
                _instante(reportado, h["minDespuesDeReporte"]),
            )
        )
    return {"siniestro": [fila_siniestro], "evidencias": evidencias, "historial": historial}


def objetos_a_subir(semilla: dict[str, Any]) -> list[tuple[str, dict[str, Any], bytes]]:
    """(clave, objeto, contenido) de cada archivo que la semilla sube. Omite el «ausente»; para el
    «alterado» el contenido es el de otro objeto, no el registrado."""
    objetos = semilla["objetos"]
    subidos = []
    for caso in semilla["casos"]:
        for e in caso["evidencias"]:
            if e["objetoSubido"] is None:
                continue
            objeto = objetos[e["objetoSubido"]]
            subidos.append(
                (clave_objeto(caso["id"], e["id"]), objeto, base64.b64decode(objeto["base64"]))
            )
    return subidos


def subir_objetos(cliente: Any, bucket: str, sse: str, semilla: dict[str, Any]) -> int:
    """Sube los objetos con su checksum y el cifrado exigido por la política del bucket."""
    subidos = objetos_a_subir(semilla)
    for clave, objeto, contenido in subidos:
        assert clave.startswith(f"{PREFIJO_OBJETOS}/")
        cliente.put_object(
            Bucket=bucket,
            Key=clave,
            Body=contenido,
            ContentType=objeto["tipoContenido"],
            ChecksumSHA256=base64.b64encode(bytes.fromhex(objeto["sha256"])).decode(),
            ServerSideEncryption=sse,
        )
    return len(subidos)


async def sembrar_base(
    pool: "AsyncConnectionPool[Any]", semilla: dict[str, Any], ahora: datetime, analista_id: UUID
) -> int:
    """Inserta los casos que aún no existen, en una sola transacción."""
    filas = [filas_del_caso(c, semilla["objetos"], ahora, analista_id) for c in semilla["casos"]]
    async with pool.connection() as conexion, conexion.transaction():
        for caso in filas:
            await conexion.execute(_SINIESTRO, caso["siniestro"][0])
            for fila in caso["evidencias"]:
                await conexion.execute(_EVIDENCIA, fila)
            for fila in caso["historial"]:
                await conexion.execute(_HISTORIAL, fila)
    return len(filas)


async def sembrar(
    pool: "AsyncConnectionPool[Any]",
    cliente_s3: Any | None,
    bucket: str | None,
    sse: str,
    ahora: datetime,
    analista_id: UUID | None = None,
) -> None:
    """Siembra la base y, si hay bucket, S3. Sin bucket solo la base (los archivos quedan
    `NO_VERIFICABLE`)."""
    semilla = cargar_semilla()
    analista = analista_id or UUID(semilla["analistaQa"]["analistaId"])
    casos = await sembrar_base(pool, semilla, ahora, analista)
    archivos = 0
    if cliente_s3 is not None and bucket:
        archivos = await asyncio.to_thread(subir_objetos, cliente_s3, bucket, sse, semilla)
    logger.info("semilla de QA aplicada", extra={"casos": casos, "archivos": archivos})


async def sembrar_con_reintentos(
    parada: asyncio.Event,
    pool: "AsyncConnectionPool[Any]",
    cliente_s3: Any | None,
    bucket: str | None,
    sse: str,
    reloj: Any,
    analista_id: UUID | None = None,
    intentos: int = 30,
    espera_s: float = 2.0,
) -> None:
    """En local, LocalStack puede tardar más que el servicio en crear el bucket: reintenta."""
    for intento in range(1, intentos + 1):
        try:
            await sembrar(pool, cliente_s3, bucket, sse, reloj.ahora(), analista_id)
            return
        except Exception as error:  # noqa: BLE001 - el mensaje puede traer datos
            logger.warning(
                "semilla de QA: no se pudo aplicar",
                extra={"intento": intento, "error_type": type(error).__name__},
            )
        try:
            await asyncio.wait_for(parada.wait(), espera_s)
            return
        except TimeoutError:
            continue
    logger.error("semilla de QA: se agotaron los intentos")
