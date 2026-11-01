"""La tarea emisora (HU-W17, 17.8; ADR 0014, decisiones 1, 5 y 6), con dobles.

Cada ciclo toma las EMITIENDO (las nunca intentadas de inmediato; las demás, pasado `reintento_s`)
y, por póliza, sin bloqueos durante las llamadas: documento (datos, PDF, S3) → firma de la huella
registrada → ACTIVA con `PolizaEmitida` en el outbox. Cada escritura va en su transacción y con la
guarda; si la guarda no cambia nada, se relee. Un fallo deja EMITIENDO con su código y nunca lanza.
Una medición `emision` por póliza.
"""

import dataclasses
import hashlib
import io
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from polizas.application.poliza.documento import ReunirDatosDocumento
from polizas.application.poliza.emitir import (
    CODIGO_DATOS_INVALIDOS,
    CODIGO_DOCUMENTO_NO_GENERADO,
    CODIGO_INESPERADO,
    MENSAJE_CICLO_FALLIDO,
    MENSAJE_EMITIDA,
    MENSAJE_FALLIDA,
    MENSAJE_INESPERADO,
    MENSAJE_SIN_LIBERAR,
    MENSAJE_VENCIDA,
    OPERACION,
    Emision,
    EmitirPolizas,
    Resultado,
)
from polizas.application.poliza.puertos import FirmaObtenida, SolicitudFirma
from polizas.domain.poliza.documento import DatosDocumento, FuenteDocumento, clave_documento
from polizas.domain.poliza.errores import FalloAlmacenDocumentos, FalloFirma
from polizas.domain.poliza.modelos import (
    Documento,
    EstadoPoliza,
    Firma,
    OrigenPoliza,
    Poliza,
    Producto,
)
from polizas.domain.suscripcion.modelos import Cobertura
from solventa_common import correlation

pytestmark = pytest.mark.anyio
CREADA = datetime(2026, 10, 22, 15, 0, 1, tzinfo=UTC)
SELLO = datetime(2026, 1, 1, tzinfo=UTC)
# Una póliza con un punto de control y sin fallo (otra tarea en curso, o que cayó) se retoma pasado
# `reintento_s` (30 s).
RETOMADA = CREADA + timedelta(seconds=31)
# Valores que nunca deben aparecer en un log.
CLIENTE = UUID("00000000-0000-4000-8000-000000000001")
CORRELACION = "corr-recaudo-17-8"
FUENTE = FuenteDocumento(
    coberturas=(Cobertura("MUERTE", "Muerte por cualquier causa", True),),
    suma_asegurada=Decimal(180000000),
    prima=Decimal("64638.10"),
    periodicidad="MENSUAL",
    moneda="COP",
    socio_id=None,
    aceptada_en=datetime(2026, 10, 22, 15, 0, tzinfo=UTC),
    transaccion_id="TX-SINTETICA-0001",
    version_reglas_prima="2026.09.1",
    version_reglas_suscripcion="2026.10.1",
)


def _emitiendo(
    secuencia: int = 1, *, correlation_id: str | None = CORRELACION, creada: datetime = CREADA
) -> Poliza:
    origen = OrigenPoliza(
        aceptacion_id=uuid4(),
        cotizacion_id=uuid4(),
        cliente_id=CLIENTE,
        producto=Producto("vida-hipotecario", "2026.09.1"),
        aceptada_en=FUENTE.aceptada_en,
    )
    return Poliza.emitiendo(
        origen, secuencia=secuencia, ahora=creada, correlation_id=correlation_id
    )


def _huella(contenido: bytes) -> str:
    return "sha256:" + hashlib.sha256(contenido).hexdigest()


def _documento(
    poliza: Poliza, version: str = "v-registrada", huella: str | None = None
) -> Documento:
    return Documento(
        clave=clave_documento(poliza),
        version_id=version,
        huella=huella or "sha256:" + "a" * 64,
        tamano_bytes=1234,
        fecha_documento=poliza.creada_en,
    )


# --- Dobles ----------------------------------------------------------------------------------
class Tx:
    def __init__(self, numero: int) -> None:
        self.numero = numero
        self.cerrada = False


class Repo:
    """EmisionesRepositorio y FuentesDocumento en memoria, con la guarda de `PolizasPg`: la póliza
    sigue EMITIENDO y sus puntos de control registrados son los de la copia que se escribe. Cada
    escritura queda en `escrituras` con su transacción. `reservas` es `reservada_hasta` (17.8,
    ajuste): se toma si no hay una vigente y se libera solo la propia."""

    def __init__(self, *polizas: Poliza) -> None:
        self.polizas = {p.poliza_id: p for p in polizas}
        self.fuente: FuenteDocumento | None = FUENTE
        self.escrituras: list[tuple[Poliza, Tx]] = []
        self.consultas: list[tuple[datetime, int]] = []
        self.reservas: dict[UUID, datetime] = {}
        self.reservadas: list[tuple[UUID, datetime, datetime]] = []
        self.liberadas: list[tuple[UUID, datetime]] = []
        self.falla_actualizar: Exception | None = None
        self.falla_pendientes: Exception | None = None

    def _libre(self, poliza_id: UUID, ahora: datetime) -> bool:
        hasta = self.reservas.get(poliza_id)
        return hasta is None or hasta < ahora

    async def pendientes(
        self, ahora: datetime, reintento_antes_de: datetime, lote: int, tx: Tx
    ) -> list[Poliza]:
        self.consultas.append((reintento_antes_de, lote))
        if self.falla_pendientes is not None:
            raise self.falla_pendientes
        elegibles = [
            p
            for p in self.polizas.values()
            if p.estado is EstadoPoliza.EMITIENDO
            and self._libre(p.poliza_id, ahora)
            and (p.ultimo_fallo is None or p.actualizada_en < reintento_antes_de)
        ]
        elegibles.sort(key=lambda p: (p.ultimo_fallo is not None, p.actualizada_en))
        return elegibles[:lote]

    async def reservar(
        self, poliza_id: UUID, ahora: datetime, hasta: datetime, tx: Tx
    ) -> Poliza | None:
        assert not tx.cerrada
        poliza = self.polizas[poliza_id]
        if poliza.estado is not EstadoPoliza.EMITIENDO or not self._libre(poliza_id, ahora):
            return None
        self.reservas[poliza_id] = hasta
        self.reservadas.append((poliza_id, ahora, hasta))
        return poliza

    async def liberar(self, poliza_id: UUID, hasta: datetime, tx: Tx) -> None:
        assert not tx.cerrada
        self.liberadas.append((poliza_id, hasta))
        if self.reservas.get(poliza_id) == hasta:
            del self.reservas[poliza_id]

    async def por_id(self, poliza_id: UUID, tx: Tx) -> Poliza | None:
        return self.polizas.get(poliza_id)

    async def actualizar(self, poliza: Poliza, tx: Tx) -> bool:
        assert not tx.cerrada, "escritura fuera de su transacción"
        if self.falla_actualizar is not None:
            raise self.falla_actualizar
        actual = self.polizas[poliza.poliza_id]
        if actual.estado is not EstadoPoliza.EMITIENDO:
            return False
        if actual.documento is not None and actual.documento != poliza.documento:
            return False
        if actual.firma is not None and actual.firma != poliza.firma:
            return False
        self.polizas[poliza.poliza_id] = poliza
        self.escrituras.append((poliza, tx))
        return True

    async def fuente_documento(self, aceptacion_id: UUID, tx: Tx) -> FuenteDocumento | None:
        return self.fuente

    def otra_tarea(self, poliza: Poliza) -> None:
        """Lo que escribe otra tarea emisora (sin pasar por la guarda de esta)."""
        self.polizas[poliza.poliza_id] = poliza


class Transacciones:
    def __init__(self) -> None:
        self.abiertas: list[Tx] = []

    @asynccontextmanager
    async def transaccion(self) -> AsyncIterator[Tx]:
        tx = Tx(len(self.abiertas))
        self.abiertas.append(tx)
        try:
            yield tx
        finally:
            tx.cerrada = True


class Generador:
    def __init__(self) -> None:
        self.datos: list[DatosDocumento] = []
        self.falla: Exception | None = None

    def generar(self, datos: DatosDocumento) -> bytes:
        self.datos.append(datos)
        if self.falla is not None:
            raise self.falla
        return f"%PDF {datos.numero} {datos.fecha_documento.isoformat()}".encode()


class Almacen:
    def __init__(self) -> None:
        self.guardados: list[tuple[str, bytes]] = []
        self.falla: Exception | None = None
        self.al_guardar: Callable[[], None] | None = None

    async def guardar(self, clave: str, contenido: bytes) -> str:
        self.guardados.append((clave, contenido))
        if self.falla is not None:
            raise self.falla
        if self.al_guardar is not None:
            self.al_guardar()
        return f"v-{len(self.guardados)}"

    async def url_descarga(self, clave: str, version_id: str, vence_s: int) -> str:
        raise AssertionError("la emisión no pide URL")


class Firmador:
    def __init__(self) -> None:
        self.solicitudes: list[SolicitudFirma] = []
        self.correlaciones: list[str | None] = []
        self.falla: Exception | None = None
        self.huella: str | None = None
        self.al_firmar: Callable[[], None] | None = None

    async def firmar(self, solicitud: SolicitudFirma) -> FirmaObtenida:
        self.solicitudes.append(solicitud)
        self.correlaciones.append(correlation.obtener())
        if self.falla is not None:
            raise self.falla
        if self.al_firmar is not None:
            self.al_firmar()
        return FirmaObtenida(
            documento_id=solicitud.documento_id,
            huella=self.huella or solicitud.huella,
            firma_id="FIR-SINT-C01",
            sello_tiempo=SELLO,
        )


class Eventos:
    def __init__(self) -> None:
        self.emitidas: list[tuple[Poliza, DatosDocumento, str, Tx]] = []

    async def emitida(
        self, poliza: Poliza, datos: DatosDocumento, correlation_id: str, tx: Tx
    ) -> None:
        assert not tx.cerrada, "evento fuera de su transacción"
        self.emitidas.append((poliza, datos, correlation_id, tx))


class Reloj:
    def __init__(self, instante: datetime) -> None:
        self.instante = instante

    def ahora(self) -> datetime:
        return self.instante


@dataclasses.dataclass
class Arnes:
    repo: Repo
    transacciones: Transacciones
    generador: Generador
    almacen: Almacen
    firma: Firmador
    eventos: Eventos
    reloj: Reloj
    caso: EmitirPolizas[Any]


def _arnes(*polizas: Poliza, ahora: datetime | None = None, **config: Any) -> Arnes:
    repo, transacciones = Repo(*polizas), Transacciones()
    generador, almacen, firma, eventos = Generador(), Almacen(), Firmador(), Eventos()
    reloj = Reloj(ahora or CREADA + timedelta(seconds=1))
    parametros = {
        "intervalo_s": 0.01,
        "reintento_s": 30,
        "reserva_s": 60,
        "lote": 10,
        "alerta_tras_s": 3600,
    }
    caso: EmitirPolizas[Any] = EmitirPolizas(
        polizas=repo,
        transacciones=transacciones,
        datos=ReunirDatosDocumento(repo),
        generador=generador,
        almacen=almacen,
        firma=firma,
        eventos=eventos,
        reloj=reloj,
        **(parametros | config),
    )
    return Arnes(repo, transacciones, generador, almacen, firma, eventos, reloj, caso)


def _errores(telemetria: Any, operacion: str) -> int:
    """Suma de OperationErrors con la dimensión Operation."""
    datos = telemetria.lector.get_metrics_data()
    if datos is None:
        return 0
    return sum(
        punto.value
        for recurso in datos.resource_metrics
        for alcance in recurso.scope_metrics
        for metrica in alcance.metrics
        if metrica.name == "OperationErrors"
        for punto in metrica.data.data_points
        if dict(punto.attributes or {}).get("Operation") == operacion
    )


def _logs(salida: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(linea) for linea in salida.getvalue().splitlines() if linea.strip()]


# --- El camino feliz -------------------------------------------------------------------------
async def test_una_nueva_queda_activa_con_su_evento(telemetria: Any) -> None:
    poliza = _emitiendo()
    a = _arnes(poliza)
    [emision] = await a.caso.emitir_una_vez()

    assert emision == Emision(poliza.poliza_id, poliza.aceptacion_id, Resultado.OK, None)
    activa = a.repo.polizas[poliza.poliza_id]
    assert activa.estado is EstadoPoliza.ACTIVA
    # fechaDocumento = creada_en (17.7) y emitida_en = fecha del documento (17.6).
    [datos] = a.generador.datos
    assert datos.fecha_documento == poliza.creada_en == activa.emitida_en
    assert datos.numero == poliza.numero
    # El PDF, una sola vez y en su clave; el documento registrado es ese PDF.
    [(clave, pdf)] = a.almacen.guardados
    assert clave == clave_documento(poliza)
    assert activa.documento == Documento(clave, "v-1", _huella(pdf), len(pdf), poliza.creada_en)
    # Se firma la huella registrada, con la póliza como documento y el cliente como firmante.
    [solicitud] = a.firma.solicitudes
    assert solicitud == SolicitudFirma(
        documento_id=poliza.poliza_id,
        huella=_huella(pdf),
        firmante_ref=CLIENTE,
        correlation_id=CORRELACION,
    )
    assert activa.firma == Firma("FIR-SINT-C01", SELLO)
    # Tres escrituras (documento, firma y ACTIVA), cada una en su transacción.
    escritas = [p for p, _ in a.repo.escrituras]
    assert [p.documento is not None for p in escritas] == [True, True, True]
    assert [p.firma is not None for p in escritas] == [False, True, True]
    assert [p.estado for p in escritas][-1] is EstadoPoliza.ACTIVA
    assert len({tx.numero for _, tx in a.repo.escrituras}) == 3
    # El evento, en la transacción de ACTIVA, con la póliza activa y los datos del documento.
    [(evento_poliza, evento_datos, evento_correlacion, evento_tx)] = a.eventos.emitidas
    assert evento_poliza == activa
    assert evento_tx is a.repo.escrituras[-1][1]
    assert evento_datos == dataclasses.replace(datos)
    assert evento_correlacion == CORRELACION
    assert telemetria.resultados(OPERACION) == ["ok"]
    assert _errores(telemetria, OPERACION) == 0
    [log] = [log for log in telemetria.logs() if log["message"] == MENSAJE_EMITIDA]
    assert (log["poliza_id"], log["clave_recaudo"], log["resultado"]) == (
        str(poliza.poliza_id),
        str(poliza.aceptacion_id),
        "ok",
    )


async def test_la_operacion_se_llama_emision() -> None:
    # infra/envs/int/apps.tfvars:81 (latency_targets: service polizas, operation emision).
    assert OPERACION == "emision"


async def test_con_documento_registrado_no_lo_regenera_y_firma_su_huella(telemetria: Any) -> None:
    poliza = _emitiendo()
    documento = _documento(poliza)
    con_documento = poliza.registrar_documento(documento, CREADA)
    a = _arnes(con_documento, ahora=RETOMADA)
    [emision] = await a.caso.emitir_una_vez()

    assert emision.resultado is Resultado.OK
    assert a.generador.datos == [] and a.almacen.guardados == []
    [solicitud] = a.firma.solicitudes
    assert solicitud.huella == documento.huella
    activa = a.repo.polizas[poliza.poliza_id]
    assert activa.documento == documento
    [(_, datos, _, _)] = a.eventos.emitidas
    assert datos.fecha_documento == documento.fecha_documento


async def test_con_firma_registrada_no_vuelve_a_firmar(telemetria: Any) -> None:
    poliza = _emitiendo()
    documento = _documento(poliza)
    firmada = poliza.registrar_documento(documento, CREADA).registrar_firma(
        Firma("FIR-PREVIA", SELLO), documento.huella, CREADA
    )
    a = _arnes(firmada, ahora=RETOMADA)
    [emision] = await a.caso.emitir_una_vez()

    assert emision.resultado is Resultado.OK
    assert a.almacen.guardados == [] and a.firma.solicitudes == []
    activa = a.repo.polizas[poliza.poliza_id]
    assert (activa.estado, activa.firma) == (EstadoPoliza.ACTIVA, Firma("FIR-PREVIA", SELLO))
    assert len(a.eventos.emitidas) == 1


# --- Fallos: sigue EMITIENDO con su código ----------------------------------------------------
@pytest.mark.parametrize(
    ("preparar", "codigo"),
    [
        (lambda a: setattr(a.repo, "fuente", None), "DATOS_DOCUMENTO_NO_DISPONIBLES"),
        (
            lambda a: setattr(a.repo, "fuente", dataclasses.replace(FUENTE, prima=Decimal(0))),
            CODIGO_DATOS_INVALIDOS,
        ),
        (
            lambda a: setattr(a.generador, "falla", ValueError("reportlab")),
            CODIGO_DOCUMENTO_NO_GENERADO,
        ),
        (
            lambda a: setattr(
                a.almacen, "falla", FalloAlmacenDocumentos(FalloAlmacenDocumentos.NO_DISPONIBLE)
            ),
            "ALMACEN_NO_DISPONIBLE",
        ),
        (
            lambda a: setattr(
                a.almacen,
                "falla",
                FalloAlmacenDocumentos(FalloAlmacenDocumentos.RECHAZADO, "AccessDenied"),
            ),
            "ALMACEN_RECHAZADO",
        ),
        (
            lambda a: setattr(a.firma, "falla", FalloFirma(FalloFirma.TIEMPO_AGOTADO)),
            "FIRMA_TIEMPO_AGOTADO",
        ),
        (
            lambda a: setattr(a.firma, "falla", FalloFirma(FalloFirma.NO_DISPONIBLE)),
            "FIRMA_NO_DISPONIBLE",
        ),
        (
            lambda a: setattr(a.firma, "falla", FalloFirma(FalloFirma.RESPUESTA_INVALIDA)),
            "FIRMA_RESPUESTA_INVALIDA",
        ),
        (lambda a: setattr(a.firma, "huella", "sha256:" + "f" * 64), "HUELLA_NO_COINCIDE"),
    ],
    ids=[
        "sin_datos",
        "datos_invalidos",
        "pdf",
        "s3_no_disponible",
        "s3_rechazado",
        "firma_tiempo_agotado",
        "firma_no_disponible",
        "firma_respuesta_invalida",
        "firma_otra_huella",
    ],
)
async def test_cada_fallo_deja_emitiendo_con_su_codigo_sin_evento_ni_error(
    telemetria: Any, preparar: Callable[[Arnes], None], codigo: str
) -> None:
    poliza, otra = _emitiendo(1), _emitiendo(2)
    a = _arnes(poliza, otra)
    preparar(a)
    emisiones = await a.caso.emitir_una_vez()

    assert [e.resultado for e in emisiones] == [Resultado.PENDIENTE, Resultado.PENDIENTE]
    assert [e.codigo for e in emisiones] == [codigo, codigo]
    for p in (poliza, otra):
        guardada = a.repo.polizas[p.poliza_id]
        assert (guardada.estado, guardada.ultimo_fallo) == (EstadoPoliza.EMITIENDO, codigo)
        assert guardada.actualizada_en == a.reloj.ahora()
    assert a.eventos.emitidas == []
    # Pendiente de reintento: no cuenta en OperationErrors.
    assert telemetria.resultados(OPERACION) == ["pendiente", "pendiente"]
    assert _errores(telemetria, OPERACION) == 0
    fallidas = [log for log in telemetria.logs() if log["message"] == MENSAJE_FALLIDA]
    assert [log["codigo"] for log in fallidas] == [codigo, codigo]


async def test_un_fallo_de_firma_conserva_el_documento_registrado(telemetria: Any) -> None:
    poliza = _emitiendo()
    a = _arnes(poliza)
    a.firma.falla = FalloFirma(FalloFirma.TIEMPO_AGOTADO)
    await a.caso.emitir_una_vez()
    guardada = a.repo.polizas[poliza.poliza_id]
    assert guardada.documento is not None and guardada.documento.version_id == "v-1"
    assert guardada.firma is None

    # Reintento pasado reintento_s: la misma versión, sin otro PDF ni otra subida.
    a.firma.falla = None
    a.reloj.instante += timedelta(seconds=31)
    [emision] = await a.caso.emitir_una_vez()
    assert emision.resultado is Resultado.OK
    assert len(a.almacen.guardados) == 1 and len(a.generador.datos) == 1
    assert [s.huella for s in a.firma.solicitudes] == [guardada.documento.huella] * 2
    activa = a.repo.polizas[poliza.poliza_id]
    assert activa.documento == guardada.documento
    [(evento, _, _, _)] = a.eventos.emitidas
    assert evento.documento is not None and evento.documento.version_id == "v-1"


# --- La guarda --------------------------------------------------------------------------------
async def test_si_otra_tarea_registro_el_documento_relee_y_firma_el_registrado(
    telemetria: Any,
) -> None:
    poliza = _emitiendo()
    a = _arnes(poliza)
    del_otro = _documento(poliza, version="v-otra", huella="sha256:" + "b" * 64)
    a.almacen.al_guardar = lambda: a.repo.otra_tarea(poliza.registrar_documento(del_otro, CREADA))
    [emision] = await a.caso.emitir_una_vez()

    assert emision.resultado is Resultado.OK
    # La guarda no escribió nuestro documento; se firmó la huella del registrado.
    [solicitud] = a.firma.solicitudes
    assert solicitud.huella == del_otro.huella
    activa = a.repo.polizas[poliza.poliza_id]
    assert activa.documento == del_otro
    [(evento, _, _, _)] = a.eventos.emitidas
    assert evento.documento == del_otro


async def test_si_otra_tarea_registro_la_firma_relee_y_activa_con_la_registrada(
    telemetria: Any,
) -> None:
    poliza = _emitiendo()
    a = _arnes(poliza)

    def otra_firma() -> None:
        actual = a.repo.polizas[poliza.poliza_id]
        assert actual.documento is not None
        a.repo.otra_tarea(
            actual.registrar_firma(Firma("FIR-OTRA", SELLO), actual.documento.huella, CREADA)
        )

    a.firma.al_firmar = otra_firma
    [emision] = await a.caso.emitir_una_vez()
    assert emision.resultado is Resultado.OK
    activa = a.repo.polizas[poliza.poliza_id]
    assert activa.firma == Firma("FIR-OTRA", SELLO)
    [(evento, _, _, _)] = a.eventos.emitidas
    assert evento.firma == Firma("FIR-OTRA", SELLO)


async def test_la_guarda_sin_filas_no_escribe_evento(telemetria: Any) -> None:
    poliza = _emitiendo()
    a = _arnes(poliza)

    def otra_activa() -> None:
        actual = a.repo.polizas[poliza.poliza_id]
        assert actual.documento is not None
        a.repo.otra_tarea(
            actual.registrar_firma(
                Firma("FIR-SINT-C01", SELLO), actual.documento.huella, CREADA
            ).activar(CREADA)
        )

    # Otra tarea activa la póliza mientras esta firma: la guarda de la firma no cambia nada.
    a.firma.al_firmar = otra_activa
    [emision] = await a.caso.emitir_una_vez()

    assert emision.resultado is Resultado.YA_EMITIDA
    assert a.eventos.emitidas == []
    assert a.repo.polizas[poliza.poliza_id].estado is EstadoPoliza.ACTIVA
    assert telemetria.resultados(OPERACION) == ["ya_emitida"]
    assert _errores(telemetria, OPERACION) == 0


async def test_la_guarda_de_activar_sin_filas_no_escribe_evento(telemetria: Any) -> None:
    poliza = _emitiendo()
    documento = _documento(poliza)
    firmada = poliza.registrar_documento(documento, CREADA).registrar_firma(
        Firma("FIR-SINT-C01", SELLO), documento.huella, CREADA
    )
    a = _arnes(firmada, ahora=RETOMADA)
    original = a.repo.fuente_documento

    async def activada_por_otra(aceptacion_id: UUID, tx: Tx) -> FuenteDocumento | None:
        # Entre la lectura de los datos y la escritura de ACTIVA, otra tarea la activa.
        a.repo.otra_tarea(firmada.activar(CREADA))
        return await original(aceptacion_id, tx)

    a.repo.fuente_documento = activada_por_otra  # type: ignore[method-assign]
    [emision] = await a.caso.emitir_una_vez()
    assert emision.resultado is Resultado.YA_EMITIDA
    assert a.eventos.emitidas == [] and a.repo.escrituras == []


async def test_un_fallo_tras_el_avance_de_otra_tarea_no_pisa_sus_puntos_de_control(
    telemetria: Any,
) -> None:
    poliza = _emitiendo()
    a = _arnes(poliza)
    del_otro = _documento(poliza, version="v-otra")

    def avanza_y_falla() -> None:
        a.repo.otra_tarea(poliza.registrar_documento(del_otro, CREADA))
        raise FalloAlmacenDocumentos(FalloAlmacenDocumentos.NO_DISPONIBLE)

    a.almacen.al_guardar = avanza_y_falla
    [emision] = await a.caso.emitir_una_vez()
    assert (emision.resultado, emision.codigo) == (Resultado.PENDIENTE, "ALMACEN_NO_DISPONIBLE")
    # El fallo no se escribió sobre el documento de la otra tarea (la guarda lo impide).
    assert a.repo.polizas[poliza.poliza_id].documento == del_otro


@pytest.mark.parametrize(
    ("fuente", "codigo"),
    [
        (None, "DATOS_DOCUMENTO_NO_DISPONIBLES"),
        (dataclasses.replace(FUENTE, moneda="cop"), CODIGO_DATOS_INVALIDOS),
    ],
    ids=["sin_datos", "datos_invalidos"],
)
async def test_si_los_datos_fallan_al_activar_sigue_emitiendo_con_su_firma(
    telemetria: Any, fuente: FuenteDocumento | None, codigo: str
) -> None:
    poliza = _emitiendo()
    documento = _documento(poliza)
    firmada = poliza.registrar_documento(documento, CREADA).registrar_firma(
        Firma("FIR-SINT-C01", SELLO), documento.huella, CREADA
    )
    a = _arnes(firmada, ahora=RETOMADA)
    a.repo.fuente = fuente
    [emision] = await a.caso.emitir_una_vez()
    assert (emision.resultado, emision.codigo) == (Resultado.PENDIENTE, codigo)
    guardada = a.repo.polizas[poliza.poliza_id]
    assert (guardada.estado, guardada.firma, guardada.ultimo_fallo) == (
        EstadoPoliza.EMITIENDO,
        firmada.firma,
        codigo,
    )
    assert a.eventos.emitidas == []


async def test_si_la_guarda_de_activar_falla_y_sigue_emitiendo_queda_pendiente(
    telemetria: Any,
) -> None:
    poliza = _emitiendo()
    documento = _documento(poliza)
    firmada = poliza.registrar_documento(documento, CREADA).registrar_firma(
        Firma("FIR-SINT-C01", SELLO), documento.huella, CREADA
    )
    a = _arnes(firmada, ahora=RETOMADA)

    async def nunca(poliza: Poliza, tx: Tx) -> bool:
        return False

    a.repo.actualizar = nunca  # type: ignore[method-assign]
    [emision] = await a.caso.emitir_una_vez()
    assert (emision.resultado, emision.codigo) == (Resultado.PENDIENTE, None)
    assert a.eventos.emitidas == []
    assert _errores(telemetria, OPERACION) == 0


async def test_una_poliza_que_desaparece_al_releer_es_un_fallo_inesperado(
    telemetria: Any,
) -> None:
    poliza = _emitiendo()
    a = _arnes(poliza)

    async def sin_fila(poliza_id: UUID, tx: Tx) -> Poliza | None:
        return None

    a.almacen.al_guardar = lambda: a.repo.otra_tarea(
        poliza.registrar_documento(_documento(poliza, version="v-otra"), CREADA)
    )
    a.repo.por_id = sin_fila  # type: ignore[method-assign]
    [emision] = await a.caso.emitir_una_vez()
    assert (emision.resultado, emision.codigo) == (Resultado.ERROR, CODIGO_INESPERADO)
    assert _errores(telemetria, OPERACION) == 1


# --- Vencida e inesperado ---------------------------------------------------------------------
async def test_una_pendiente_de_mas_de_una_hora_se_mide_como_error(telemetria: Any) -> None:
    poliza = _emitiendo()
    a = _arnes(poliza, ahora=CREADA + timedelta(seconds=3601))
    a.firma.falla = FalloFirma(FalloFirma.NO_DISPONIBLE)
    [emision] = await a.caso.emitir_una_vez()

    assert emision == Emision(
        poliza.poliza_id, poliza.aceptacion_id, Resultado.ERROR, "FIRMA_NO_DISPONIBLE", vencida=True
    )
    # Sigue EMITIENDO y se sigue reintentando: sin tope.
    assert a.repo.polizas[poliza.poliza_id].estado is EstadoPoliza.EMITIENDO
    assert telemetria.resultados(OPERACION) == ["error"]
    assert _errores(telemetria, OPERACION) == 1
    [log] = [log for log in telemetria.logs() if log["message"] == MENSAJE_VENCIDA]
    assert (log["level"], log["codigo"], log["antiguedad_s"]) == (
        "ERROR",
        "FIRMA_NO_DISPONIBLE",
        3601,
    )


async def test_una_pendiente_de_justo_una_hora_no_es_error(telemetria: Any) -> None:
    poliza = _emitiendo()
    a = _arnes(poliza, ahora=CREADA + timedelta(seconds=3600))
    a.firma.falla = FalloFirma(FalloFirma.NO_DISPONIBLE)
    [emision] = await a.caso.emitir_una_vez()
    assert (emision.resultado, emision.vencida) == (Resultado.PENDIENTE, False)
    assert _errores(telemetria, OPERACION) == 0


async def test_una_vieja_que_se_activa_es_ok(telemetria: Any) -> None:
    poliza = _emitiendo()
    a = _arnes(poliza, ahora=CREADA + timedelta(hours=5))
    [emision] = await a.caso.emitir_una_vez()
    assert emision.resultado is Resultado.OK
    assert _errores(telemetria, OPERACION) == 0


async def test_un_fallo_inesperado_se_mide_como_error_y_sigue_con_la_siguiente(
    telemetria: Any,
) -> None:
    rota, sana = _emitiendo(1), _emitiendo(2)
    a = _arnes(rota, sana)
    original = a.repo.actualizar
    llamadas = {"n": 0}

    async def falla_la_primera(poliza: Poliza, tx: Tx) -> bool:
        llamadas["n"] += 1
        if llamadas["n"] == 1:
            raise RuntimeError("base caída; con datos 00000000-0000-4000-8000-000000000001")
        return await original(poliza, tx)

    a.repo.actualizar = falla_la_primera  # type: ignore[method-assign]
    emisiones = await a.caso.emitir_una_vez()

    assert [e.resultado for e in emisiones] == [Resultado.ERROR, Resultado.OK]
    assert emisiones[0].codigo == CODIGO_INESPERADO
    # El fallo se registró (la base respondió en el segundo intento), así que espera reintento_s.
    assert a.repo.polizas[rota.poliza_id].ultimo_fallo == CODIGO_INESPERADO
    assert a.repo.polizas[sana.poliza_id].estado is EstadoPoliza.ACTIVA
    assert telemetria.resultados(OPERACION) == ["error", "ok"]
    assert _errores(telemetria, OPERACION) == 1
    [log] = [log for log in telemetria.logs() if log["message"] == MENSAJE_INESPERADO]
    assert (log["level"], log["error_type"]) == ("ERROR", "RuntimeError")
    assert "base caída" not in telemetria.texto_logs()


async def test_si_tampoco_se_puede_registrar_el_fallo_no_lanza(telemetria: Any) -> None:
    poliza = _emitiendo()
    a = _arnes(poliza)
    a.repo.falla_actualizar = RuntimeError("base caída")
    [emision] = await a.caso.emitir_una_vez()
    assert (emision.resultado, emision.codigo) == (Resultado.ERROR, CODIGO_INESPERADO)
    assert a.repo.polizas[poliza.poliza_id] == poliza


# --- Selección: inmediatas, reintentos, lote y parada ------------------------------------------
async def test_toma_las_nuevas_de_inmediato_y_las_fallidas_pasado_reintento_s() -> None:
    nueva = _emitiendo(1)
    fallida = _emitiendo(2).registrar_fallo("FIRMA_TIEMPO_AGOTADO", CREADA)
    a = _arnes(nueva, fallida, ahora=CREADA + timedelta(seconds=30))
    # 30 s justos: la fallida todavía no (estrictamente más de reintento_s).
    primeras = await a.caso.emitir_una_vez()
    assert [e.poliza_id for e in primeras] == [nueva.poliza_id]
    assert a.repo.consultas == [(CREADA, 10)]
    a.reloj.instante = CREADA + timedelta(seconds=31)
    segundas = await a.caso.emitir_una_vez()
    assert [e.poliza_id for e in segundas] == [fallida.poliza_id]


async def test_respeta_el_lote_y_la_parada_entre_polizas() -> None:
    import asyncio

    polizas = [_emitiendo(n) for n in range(1, 5)]
    a = _arnes(*polizas, lote=3)
    assert len(await a.caso.emitir_una_vez()) == 3
    assert a.repo.consultas[-1][1] == 3

    otra = _arnes(*(_emitiendo(n) for n in range(1, 4)))
    parada = asyncio.Event()
    otra.firma.al_firmar = parada.set
    # La parada llega durante la primera: se termina esa y no se toma la segunda.
    emisiones = await otra.caso.emitir_una_vez(parada)
    assert [e.resultado for e in emisiones] == [Resultado.OK]


# --- correlationId y logs ---------------------------------------------------------------------
async def test_propaga_el_correlation_id_del_recaudo(telemetria: Any) -> None:
    poliza = _emitiendo()
    a = _arnes(poliza)
    await a.caso.emitir_una_vez()
    assert a.firma.correlaciones == [CORRELACION]
    assert [s.correlation_id for s in a.firma.solicitudes] == [CORRELACION]
    assert [c for _, _, c, _ in a.eventos.emitidas] == [CORRELACION]
    [log] = [log for log in telemetria.logs() if log["message"] == MENSAJE_EMITIDA]
    assert log["correlation_id"] == CORRELACION
    # Fuera de la póliza, el contexto queda como estaba.
    assert correlation.obtener() is None


async def test_sin_correlation_id_usa_el_id_de_la_poliza(telemetria: Any) -> None:
    poliza = _emitiendo(correlation_id=None)
    a = _arnes(poliza)
    await a.caso.emitir_una_vez()
    assert a.firma.correlaciones == [str(poliza.poliza_id)]
    assert [c for _, _, c, _ in a.eventos.emitidas] == [str(poliza.poliza_id)]


async def test_los_logs_no_llevan_pii_ni_la_huella(telemetria: Any) -> None:
    poliza = _emitiendo()
    a = _arnes(poliza)
    await a.caso.emitir_una_vez()
    otra = _arnes(_emitiendo(2))
    otra.firma.falla = FalloFirma(FalloFirma.TIEMPO_AGOTADO)
    await otra.caso.emitir_una_vez()
    texto = telemetria.texto_logs()
    activa = a.repo.polizas[poliza.poliza_id]
    assert activa.documento is not None
    huella = activa.documento.huella.removeprefix("sha256:")
    for centinela in (str(CLIENTE), huella, huella[:16], "64638", "180000000", "TX-SINTETICA"):
        assert centinela not in texto, centinela
    assert str(poliza.poliza_id) in texto  # la coincidencia está armada


# --- El bucle ---------------------------------------------------------------------------------
async def test_el_bucle_espera_el_intervalo_emite_y_se_detiene_con_la_parada() -> None:
    import asyncio

    poliza = _emitiendo()
    a = _arnes(poliza)
    parada = asyncio.Event()
    tarea = asyncio.create_task(a.caso.ejecutar(parada))
    for _ in range(200):
        if a.repo.polizas[poliza.poliza_id].estado is EstadoPoliza.ACTIVA:
            break
        await asyncio.sleep(0.01)
    parada.set()
    await asyncio.wait_for(tarea, 1)
    assert a.repo.polizas[poliza.poliza_id].estado is EstadoPoliza.ACTIVA


async def test_un_ciclo_fallido_se_registra_y_el_bucle_sigue(
    capturar_logs: Callable[..., io.StringIO],
) -> None:
    import asyncio

    salida = capturar_logs()
    a = _arnes(_emitiendo())
    a.repo.falla_pendientes = RuntimeError("sin base")
    parada = asyncio.Event()
    tarea = asyncio.create_task(a.caso.ejecutar(parada))
    for _ in range(200):
        if len(a.repo.consultas) >= 2:
            break
        await asyncio.sleep(0.01)
    parada.set()
    await asyncio.wait_for(tarea, 1)
    assert len(a.repo.consultas) >= 2
    errores = [log for log in _logs(salida) if log["message"] == MENSAJE_CICLO_FALLIDO]
    assert errores and errores[0]["error_type"] == "RuntimeError"
    assert "sin base" not in salida.getvalue()


async def test_la_parada_antes_del_primer_ciclo_no_emite() -> None:
    import asyncio

    a = _arnes(_emitiendo(), intervalo_s=10)
    parada = asyncio.Event()
    parada.set()
    await asyncio.wait_for(a.caso.ejecutar(parada), 1)
    assert a.repo.consultas == []


# --- Reserva (17.8, ajuste) -------------------------------------------------------------------
async def test_reserva_antes_de_procesar_y_la_libera_al_terminar(telemetria: Any) -> None:
    poliza = _emitiendo()
    a = _arnes(poliza)
    ahora = a.reloj.ahora()
    reservas_al_subir: list[dict[UUID, datetime]] = []
    a.almacen.al_guardar = lambda: reservas_al_subir.append(dict(a.repo.reservas))
    [emision] = await a.caso.emitir_una_vez()

    assert emision.resultado is Resultado.OK
    hasta = ahora + timedelta(seconds=60)
    assert a.repo.reservadas == [(poliza.poliza_id, ahora, hasta)]
    # Reservada mientras sube (y firma); liberada al terminar, solo la propia.
    assert reservas_al_subir == [{poliza.poliza_id: hasta}]
    assert a.repo.liberadas == [(poliza.poliza_id, hasta)]
    assert a.repo.reservas == {}


async def test_con_una_reserva_ajena_vigente_no_la_toma(telemetria: Any) -> None:
    poliza = _emitiendo()
    a = _arnes(poliza)
    ajena = a.reloj.ahora() + timedelta(seconds=10)
    a.repo.reservas[poliza.poliza_id] = ajena
    assert await a.caso.emitir_una_vez() == []
    # Tampoco si la selección la devolviera (la tomó otra tarea entre la lectura y la reserva).
    original = a.repo.pendientes

    async def sin_filtro(ahora: datetime, antes_de: datetime, lote: int, tx: Tx) -> list[Poliza]:
        return [poliza]

    a.repo.pendientes = sin_filtro  # type: ignore[method-assign]
    assert await a.caso.emitir_una_vez() == []
    a.repo.pendientes = original  # type: ignore[method-assign]
    assert a.generador.datos == [] and a.almacen.guardados == [] and a.firma.solicitudes == []
    assert a.repo.reservas == {poliza.poliza_id: ajena} and a.repo.liberadas == []
    # Sin medición: no la procesó.
    assert telemetria.resultados(OPERACION) == []


async def test_una_reserva_ajena_vencida_se_toma(telemetria: Any) -> None:
    poliza = _emitiendo()
    a = _arnes(poliza)
    a.repo.reservas[poliza.poliza_id] = a.reloj.ahora() - timedelta(microseconds=1)
    [emision] = await a.caso.emitir_una_vez()
    assert emision.resultado is Resultado.OK
    assert len(a.almacen.guardados) == 1
    assert a.repo.reservas == {}


@pytest.mark.parametrize(
    "preparar",
    [
        lambda a: setattr(a.firma, "falla", FalloFirma(FalloFirma.NO_DISPONIBLE)),
        lambda a: setattr(a.generador, "falla", ValueError("reportlab")),
        lambda a: setattr(a.repo, "falla_actualizar", RuntimeError("base caída")),
    ],
    ids=["fallo_de_firma", "fallo_del_pdf", "fallo_inesperado"],
)
async def test_libera_la_reserva_tambien_con_un_fallo(
    telemetria: Any, preparar: Callable[[Arnes], None]
) -> None:
    poliza = _emitiendo()
    a = _arnes(poliza)
    preparar(a)
    [emision] = await a.caso.emitir_una_vez()
    assert emision.resultado in (Resultado.PENDIENTE, Resultado.ERROR)
    assert a.repo.reservas == {}
    assert [p for p, _ in a.repo.liberadas] == [poliza.poliza_id]


async def test_si_no_se_puede_liberar_se_registra_y_la_reserva_vence(telemetria: Any) -> None:
    poliza = _emitiendo()
    a = _arnes(poliza)

    async def caida(poliza_id: UUID, hasta: datetime, tx: Tx) -> None:
        raise RuntimeError("base caída al liberar")

    a.repo.liberar = caida  # type: ignore[method-assign]
    [emision] = await a.caso.emitir_una_vez()
    assert emision.resultado is Resultado.OK
    [log] = [log for log in telemetria.logs() if log["message"] == MENSAJE_SIN_LIBERAR]
    assert (log["level"], log["error_type"]) == ("WARNING", "RuntimeError")
    assert "base caída" not in telemetria.texto_logs()


async def test_una_caida_sin_fallo_se_retoma_al_vencer_la_reserva() -> None:
    # Una tarea cayó con la póliza reservada y el documento registrado, sin fallo.
    poliza = _emitiendo()
    con_documento = poliza.registrar_documento(_documento(poliza), CREADA)
    a = _arnes(con_documento)
    a.repo.reservas[poliza.poliza_id] = a.reloj.ahora() + timedelta(seconds=5)
    assert await a.caso.emitir_una_vez() == []
    a.reloj.instante += timedelta(seconds=6)
    [emision] = await a.caso.emitir_una_vez()
    assert emision.resultado is Resultado.OK
    assert a.almacen.guardados == []
