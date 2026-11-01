"""La lectura de la póliza con los dobles (HU-W17, 17.9): ConsultarPoliza, DescargarDocumento y
VerificarPoliza, con su medición y sus logs."""

import dataclasses
import logging
from collections.abc import Callable
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from polizas.application.aceptacion.registrar import (
    ComandoAceptacion,
    ConfigAceptacion,
    RegistrarAceptacion,
)
from polizas.application.poliza.consultar import (
    OPERACION_CONSULTAR,
    OPERACION_DOCUMENTO,
    OPERACION_VERIFICAR,
    ConsultarPoliza,
    DescargarDocumento,
    VerificarPoliza,
)
from polizas.application.suscripcion.solicitar_suscripcion import (
    ComandoSuscripcion,
    ConfigSuscripcion,
    SolicitarSuscripcion,
)
from polizas.domain.aceptacion.errores import AceptacionNoEncontrada
from polizas.domain.aceptacion.modelos import Aceptacion, Declaraciones
from polizas.domain.poliza.consulta import EnlaceDocumento, Verificacion
from polizas.domain.poliza.errores import (
    FalloAlmacenDocumentos,
    PolizaNoActiva,
    PolizaNoEncontrada,
)
from polizas.domain.poliza.modelos import EstadoPoliza, Poliza
from polizas.domain.recaudo.modelos import SituacionRecaudo
from polizas.domain.suscripcion.errores import DependenciaNoDisponible
from polizas.domain.suscripcion.modelos import Actor, RolActor

pytestmark = pytest.mark.anyio
ASESOR = Actor("sub-asesor-1", RolActor.ASESOR)
HUELLA = "sha256:" + "ab" * 32
OTRA_HUELLA = "sha256:" + "cd" * 32
URL = "https://polizas.s3.amazonaws.com/polizas/x.pdf?versionId=v&X-Amz-Signature=secreta"


async def _aceptada(dobles: Any, sinteticos: Any) -> Aceptacion:
    decision, _ = await SolicitarSuscripcion(
        dobles.insumos,
        dobles.reglas,
        dobles.decisiones,
        dobles.codigos,
        dobles.reloj,
        ConfigSuscripcion(),
    ).ejecutar(ComandoSuscripcion(UUID(sinteticos.cotizacion_id("S01")), uuid4(), ASESOR, "c"))
    dobles.reloj.instante += timedelta(minutes=3)
    aceptacion, _ = await RegistrarAceptacion(
        dobles.decisiones, dobles.aceptaciones, dobles.reloj, ConfigAceptacion()
    ).ejecutar(
        ComandoAceptacion(
            decision.cotizacion_id,
            decision.id,
            "2026.10.1",
            Declaraciones(True, True, False, True),
            uuid4(),
            ASESOR,
            "corr",
        )
    )
    return aceptacion


def _otra(aceptacion: Aceptacion) -> Aceptacion:
    """Otra aceptación (solo S01 sale APROBADA y se acepta una vez): para una segunda póliza."""
    return dataclasses.replace(aceptacion, id=uuid4(), cotizacion_id=uuid4())


class AlmacenFalso:
    """AlmacenDocumentos: anota cada URL pedida; `falla` la hace fallar."""

    def __init__(self, falla: Exception | None = None) -> None:
        self.falla = falla
        self.pedidas: list[tuple[str, str, int]] = []

    async def guardar(self, clave: str, contenido: bytes) -> str:
        raise AssertionError("la lectura no sube documentos")

    async def url_descarga(self, clave: str, version_id: str, vence_s: int) -> str:
        self.pedidas.append((clave, version_id, vence_s))
        if self.falla is not None:
            raise self.falla
        return URL


def _consultar(dobles: Any) -> ConsultarPoliza:
    return ConsultarPoliza(
        dobles.aceptaciones, dobles.decisiones, dobles.recaudos, dobles.puertos().polizas
    )


def _descargar(dobles: Any, almacen: AlmacenFalso | None, vence_s: int = 300) -> DescargarDocumento:
    return DescargarDocumento(dobles.puertos().polizas, almacen, dobles.reloj, vence_s)


def _verificar(dobles: Any) -> VerificarPoliza:
    return VerificarPoliza(dobles.puertos().polizas)


# --- ConsultarPoliza --------------------------------------------------------------------------
async def test_sin_aceptacion_no_encontrada(dobles: Any, telemetria: Any) -> None:
    with pytest.raises(AceptacionNoEncontrada):
        await _consultar(dobles).por_cotizacion(uuid4())
    assert telemetria.resultados(OPERACION_CONSULTAR) == ["rechazado"]


async def test_aceptada_sin_poliza_es_poliza_no_encontrada(
    dobles: Any, sinteticos: Any, telemetria: Any
) -> None:
    # AC2: sin RecaudoConfirmado (pendiente o rechazado) no hay póliza.
    aceptacion = await _aceptada(dobles, sinteticos)
    with pytest.raises(PolizaNoEncontrada):
        await _consultar(dobles).por_cotizacion(aceptacion.cotizacion_id)
    assert telemetria.resultados(OPERACION_CONSULTAR) == ["rechazado"]


@pytest.mark.parametrize("etapa", ["EMITIENDO", "FIRMADA", "ACTIVA"])
async def test_la_poliza_con_la_oferta_aceptada_y_el_recaudo(
    dobles: Any,
    sinteticos: Any,
    registrar_poliza: Callable[..., Poliza],
    telemetria: Any,
    etapa: str,
) -> None:
    aceptacion = await _aceptada(dobles, sinteticos)
    poliza = registrar_poliza(aceptacion, etapa)
    vista = await _consultar(dobles).por_cotizacion(aceptacion.cotizacion_id)
    insumos = sinteticos.insumos("S01")
    assert vista.poliza == poliza
    assert vista.coberturas == insumos.coberturas
    assert (vista.suma_asegurada, vista.prima) == (insumos.suma_asegurada, insumos.prima_total)
    assert (vista.moneda, vista.periodicidad) == (insumos.moneda, insumos.periodicidad)
    assert vista.aceptada_en == aceptacion.aceptada_en
    assert vista.recaudada_en == dobles.recaudos.resultados[aceptacion.id].ocurrido_en
    assert telemetria.resultados(OPERACION_CONSULTAR) == ["ok"]


async def test_sin_la_decision_es_dependencia_no_disponible(
    dobles: Any, sinteticos: Any, registrar_poliza: Callable[..., Poliza], telemetria: Any
) -> None:
    # No pasa (llave e inmutabilidad); si pasara, 503 y no 500.
    aceptacion = await _aceptada(dobles, sinteticos)
    registrar_poliza(aceptacion)
    dobles.decisiones.decisiones.clear()
    with pytest.raises(DependenciaNoDisponible) as error:
        await _consultar(dobles).por_cotizacion(aceptacion.cotizacion_id)
    assert error.value.dependencia == "polizas.decisiones"
    assert telemetria.resultados(OPERACION_CONSULTAR) == ["error"]


@pytest.mark.parametrize("recaudo", ["sin_proyeccion", "rechazado"])
async def test_sin_el_recaudo_confirmado_es_dependencia_no_disponible(
    dobles: Any,
    sinteticos: Any,
    registrar_poliza: Callable[..., Poliza],
    telemetria: Any,
    recaudo: str,
) -> None:
    # No pasa: la póliza nace con el RecaudoConfirmado, en la misma transacción.
    aceptacion = await _aceptada(dobles, sinteticos)
    registrar_poliza(aceptacion)
    if recaudo == "sin_proyeccion":
        del dobles.recaudos.resultados[aceptacion.id]
    else:
        previo = dobles.recaudos.resultados[aceptacion.id]
        dobles.recaudos.resultados[aceptacion.id] = dataclasses.replace(
            previo, estado=SituacionRecaudo.RECHAZADO
        )
    with pytest.raises(DependenciaNoDisponible) as error:
        await _consultar(dobles).por_cotizacion(aceptacion.cotizacion_id)
    assert error.value.dependencia == "polizas.recaudos"
    assert telemetria.resultados(OPERACION_CONSULTAR) == ["error"]


# --- DescargarDocumento -----------------------------------------------------------------------
async def test_la_url_es_la_de_la_version_registrada_con_su_vencimiento(
    dobles: Any, sinteticos: Any, registrar_poliza: Callable[..., Poliza], telemetria: Any
) -> None:
    aceptacion = await _aceptada(dobles, sinteticos)
    poliza = registrar_poliza(aceptacion)
    almacen = AlmacenFalso()
    enlace = await _descargar(dobles, almacen, vence_s=120).ejecutar(poliza.poliza_id)
    assert poliza.documento is not None
    assert almacen.pedidas == [(poliza.documento.clave, "v-registrada", 120)]
    # Desde la hora previa a firmarla: nunca después de su vencimiento real.
    assert enlace == EnlaceDocumento(
        url=URL, vence_en=dobles.reloj.ahora() + timedelta(seconds=120)
    )
    assert telemetria.resultados(OPERACION_DOCUMENTO) == ["ok"]


async def test_documento_de_una_poliza_inexistente(dobles: Any, telemetria: Any) -> None:
    almacen = AlmacenFalso()
    with pytest.raises(PolizaNoEncontrada):
        await _descargar(dobles, almacen).ejecutar(uuid4())
    # Sin almacén también: la póliza se busca antes.
    with pytest.raises(PolizaNoEncontrada):
        await _descargar(dobles, None).ejecutar(uuid4())
    assert almacen.pedidas == []
    assert telemetria.resultados(OPERACION_DOCUMENTO) == ["rechazado", "rechazado"]


@pytest.mark.parametrize("etapa", ["EMITIENDO", "DOCUMENTO", "FIRMADA"])
async def test_documento_de_una_poliza_emitiendo_no_esta_activa(
    dobles: Any,
    sinteticos: Any,
    registrar_poliza: Callable[..., Poliza],
    telemetria: Any,
    etapa: str,
) -> None:
    # Aunque el documento ya esté subido (y firmado), no se entrega hasta ACTIVA.
    aceptacion = await _aceptada(dobles, sinteticos)
    poliza = registrar_poliza(aceptacion, etapa)
    almacen = AlmacenFalso()
    with pytest.raises(PolizaNoActiva):
        await _descargar(dobles, almacen).ejecutar(poliza.poliza_id)
    assert almacen.pedidas == []
    assert telemetria.resultados(OPERACION_DOCUMENTO) == ["rechazado"]


async def test_documento_sin_almacen_configurado(
    dobles: Any, sinteticos: Any, registrar_poliza: Callable[..., Poliza], telemetria: Any
) -> None:
    aceptacion = await _aceptada(dobles, sinteticos)
    poliza = registrar_poliza(aceptacion)
    with pytest.raises(DependenciaNoDisponible) as error:
        await _descargar(dobles, None).ejecutar(poliza.poliza_id)
    assert error.value.dependencia == "documentos"
    assert telemetria.resultados(OPERACION_DOCUMENTO) == ["error"]


@pytest.mark.parametrize(
    "codigo", [FalloAlmacenDocumentos.NO_DISPONIBLE, FalloAlmacenDocumentos.RECHAZADO]
)
async def test_documento_si_falla_la_firma_de_la_url(
    dobles: Any,
    sinteticos: Any,
    registrar_poliza: Callable[..., Poliza],
    telemetria: Any,
    caplog: pytest.LogCaptureFixture,
    codigo: str,
) -> None:
    aceptacion = await _aceptada(dobles, sinteticos)
    poliza = registrar_poliza(aceptacion)
    almacen = AlmacenFalso(FalloAlmacenDocumentos(codigo, "NoCredentials"))
    with (
        caplog.at_level(logging.WARNING, logger="polizas.poliza"),
        pytest.raises(DependenciaNoDisponible) as error,
    ):
        await _descargar(dobles, almacen).ejecutar(poliza.poliza_id)
    assert error.value.dependencia == "documentos"
    assert telemetria.resultados(OPERACION_DOCUMENTO) == ["error"]
    [aviso] = [r for r in caplog.records if r.name == "polizas.poliza"]
    assert aviso.levelno == logging.WARNING
    assert (aviso.poliza_id, aviso.codigo, aviso.error_s3) == (  # type: ignore[attr-defined]
        str(poliza.poliza_id),
        codigo,
        "NoCredentials",
    )


# --- VerificarPoliza --------------------------------------------------------------------------
async def test_verificacion_valida_con_la_huella_firmada(
    dobles: Any, sinteticos: Any, registrar_poliza: Callable[..., Poliza], telemetria: Any
) -> None:
    aceptacion = await _aceptada(dobles, sinteticos)
    poliza = registrar_poliza(aceptacion)
    verificacion = await _verificar(dobles).ejecutar(poliza.numero, HUELLA)
    assert verificacion == Verificacion(valido=True, numero=poliza.numero, firma=poliza.firma)
    assert telemetria.resultados(OPERACION_VERIFICAR) == ["valida"]


async def test_las_verificaciones_no_validas_son_iguales_salvo_el_numero(
    dobles: Any, sinteticos: Any, registrar_poliza: Callable[..., Poliza], telemetria: Any
) -> None:
    """Huella distinta, número inexistente y póliza EMITIENDO (firmada y con la misma huella): la
    misma respuesta, sin decir si el número existe."""
    aceptacion = await _aceptada(dobles, sinteticos)
    activa = registrar_poliza(aceptacion)
    emitiendo = registrar_poliza(_otra(aceptacion), "FIRMADA", secuencia=8)
    assert emitiendo.estado is EstadoPoliza.EMITIENDO
    inexistente = "100-2026-999999"
    verificar = _verificar(dobles)
    resultados = [
        await verificar.ejecutar(activa.numero, OTRA_HUELLA),
        await verificar.ejecutar(inexistente, HUELLA),
        await verificar.ejecutar(emitiendo.numero, HUELLA),
    ]
    numeros = [activa.numero, inexistente, emitiendo.numero]
    assert resultados == [Verificacion(valido=False, numero=n, firma=None) for n in numeros]
    assert telemetria.resultados(OPERACION_VERIFICAR) == ["no_valida"] * 3


async def test_verificar_compara_con_la_huella_del_documento_registrado(
    dobles: Any, sinteticos: Any, registrar_poliza: Callable[..., Poliza]
) -> None:
    # Una huella en mayúsculas no es la firmada (la ruta la rechaza antes con 422).
    poliza = registrar_poliza(await _aceptada(dobles, sinteticos))
    verificacion = await _verificar(dobles).ejecutar(poliza.numero, HUELLA.upper())
    assert verificacion.valido is False


async def test_una_falla_de_la_base_al_verificar_se_mide_como_error(
    dobles: Any, telemetria: Any
) -> None:
    class Rota:
        async def por_numero(self, numero: str) -> Poliza | None:
            raise OSError("base caída")

    with pytest.raises(OSError):
        await VerificarPoliza(Rota()).ejecutar("100-2026-000001", HUELLA)  # type: ignore[arg-type]
    assert telemetria.resultados(OPERACION_VERIFICAR) == ["error"]
