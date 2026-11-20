"""Caso de uso `recibir_evento` con puertos falsos (HU-W21, DS3-16): el orden de las comprobaciones,
los rechazos sin efectos y la idempotencia, con los casos de `eventos-parametricos.json`."""

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from adaptador_telemetria.application.telemetria.puertos import DependenciaNoDisponible
from adaptador_telemetria.application.telemetria.recibir_evento import RecibirEvento
from adaptador_telemetria.domain.telemetria.contrato import validar_contrato
from adaptador_telemetria.domain.telemetria.errores import (
    ContratoInvalido,
    FirmaInvalida,
    MarcaFueraDeRango,
)
from adaptador_telemetria.domain.telemetria.firma import calcular_firma
from adaptador_telemetria.domain.telemetria.modelos import evento_id

pytestmark = pytest.mark.anyio
SECRETO = "secreto-de-prueba"
AHORA = datetime(2026, 11, 10, 14, 0, 5, tzinfo=UTC)


async def _enviar(caso: RecibirEvento, envio: Any, correlacion: str = "corr-1") -> Any:
    return await caso(envio.fuente, envio.marca_tiempo, envio.firma, envio.cuerpo, correlacion)


# --- Los casos del archivo de datos --------------------------------------------------------------
def _claves(datos: dict[str, Any], filtro: Callable[[dict[str, Any]], bool]) -> list[str]:
    return [c["clave"] for c in datos["eventos"] if filtro(c)]


def test_el_archivo_de_datos_trae_los_casos_del_plan(datos: dict[str, Any]) -> None:
    claves = {c["clave"] for c in datos["eventos"]}
    assert {
        "valido",
        "justoEnElUmbral",
        "unaUnidadBajoElUmbral",
        "fueraDeVentanaEnVigenteHasta",
        "fueraDeZona",
        "firmaInvalida",
        "marcaDeTiempoVieja",
        "malformadoNoJson",
        "duplicado",
    } <= claves


async def test_cada_caso_aceptado_responde_202_y_archiva_el_original(
    caso: RecibirEvento,
    archivo: Any,
    registro: Any,
    datos: dict[str, Any],
    firmar_caso: Callable[..., Any],
) -> None:
    for entrada in datos["eventos"]:
        esperado = entrada["esperado"]
        if esperado["http"]["estado"] != 202:
            continue
        envio = firmar_caso(entrada, AHORA)
        recepcion = await _enviar(caso, envio)
        assert not recepcion.duplicado, entrada["clave"]
        assert str(recepcion.evento_id) == esperado["eventoId"], entrada["clave"]
        assert recepcion.recibido_en == AHORA
        clave, cuerpo, metadatos = archivo.guardados[-1]
        # El original, byte por byte, con la fecha UTC de la recepción en la clave.
        assert cuerpo == envio.cuerpo
        assert clave == f"telemetria/2026/11/10/{esperado['eventoId']}.json"
        assert metadatos["evento-id"] == esperado["eventoId"]
        assert metadatos["fuente"] == envio.fuente
        assert metadatos["recibido-en"] == "2026-11-10T14:00:05Z"
        assert registro.outbox[-1].evento_id == recepcion.evento_id
    assert len(registro.outbox) == len(archivo.guardados) == 8


async def test_cada_rechazo_responde_su_error_sin_archivar_ni_registrar(
    caso: RecibirEvento,
    archivo: Any,
    registro: Any,
    datos: dict[str, Any],
    firmar_caso: Callable[..., Any],
) -> None:
    errores = {
        "FIRMA_INVALIDA": FirmaInvalida,
        "MARCA_TIEMPO_FUERA_DE_RANGO": MarcaFueraDeRango,
        "CONTRATO_INVALIDO": ContratoInvalido,
    }
    rechazados = [c for c in datos["eventos"] if c["esperado"]["http"]["codigo"]]
    assert len(rechazados) == 10
    for entrada in rechazados:
        esperado = entrada["esperado"]["http"]
        with pytest.raises(errores[esperado["codigo"]]) as error:
            await _enviar(caso, firmar_caso(entrada, AHORA))
        if "campos" in esperado:
            assert error.value.campos == tuple(esperado["campos"]), entrada["clave"]  # type: ignore[attr-defined]
    # Nada se archivó ni se tocó la base: ni siquiera la lectura del duplicado.
    assert archivo.guardados == []
    assert registro.llamadas == []
    assert registro.outbox == []


async def test_el_reenvio_responde_con_el_primer_recibido_en_y_no_archiva_otra_vez(
    caso: RecibirEvento,
    crear_caso: Callable[..., RecibirEvento],
    archivo: Any,
    registro: Any,
    casos: dict[str, dict[str, Any]],
    firmar_caso: Callable[..., Any],
) -> None:
    primero = await _enviar(caso, firmar_caso(casos["valido"], AHORA))
    reenvio_en = AHORA + timedelta(hours=30)
    otro_dia = crear_caso(reloj=reenvio_en)
    segundo = await _enviar(otro_dia, firmar_caso(casos["duplicado"], reenvio_en))
    assert segundo.duplicado
    assert (segundo.evento_id, segundo.recibido_en) == (primero.evento_id, primero.recibido_en)
    assert len(archivo.guardados) == 1
    assert len(registro.outbox) == 1


async def test_dos_envios_a_la_vez_el_que_pierde_la_carrera_es_un_duplicado(
    caso: RecibirEvento,
    archivo: Any,
    registro: Any,
    casos: dict[str, dict[str, Any]],
    firmar_caso: Callable[..., Any],
) -> None:
    """Ambos pasan la lectura, archivan en la misma clave y el `ON CONFLICT` decide."""
    registro.gana_otro = AHORA - timedelta(seconds=1)
    recepcion = await _enviar(caso, firmar_caso(casos["valido"], AHORA))
    assert recepcion.duplicado
    assert recepcion.recibido_en == AHORA - timedelta(seconds=1)
    assert len(archivo.guardados) == 1
    assert registro.outbox == []


# --- Autenticidad --------------------------------------------------------------------------------
async def test_la_firma_se_verifica_antes_que_el_desfase(
    caso: RecibirEvento, casos: dict[str, dict[str, Any]], firmar_caso: Callable[..., Any]
) -> None:
    """Firma inválida y marca vieja a la vez: gana FIRMA_INVALIDA, así quien no puede firmar no
    averigua la ventana de tiempo."""
    envio = firmar_caso(casos["marcaDeTiempoVieja"], AHORA)
    mala = envio.__class__(envio.fuente, envio.marca_tiempo, "sha256=" + "0" * 64, envio.cuerpo)
    with pytest.raises(FirmaInvalida):
        await _enviar(caso, mala)


async def test_el_desfase_se_valida_antes_que_el_contrato(caso: RecibirEvento) -> None:
    marca = (AHORA - timedelta(seconds=301)).isoformat().replace("+00:00", "Z")
    cuerpo = b"esto no es json"
    with pytest.raises(MarcaFueraDeRango):
        await caso("IDEAM-SIM", marca, calcular_firma(SECRETO, marca, cuerpo), cuerpo, "c")


@pytest.mark.parametrize("segundos", [-300, 300, 0])
async def test_el_desfase_en_el_limite_se_acepta(
    caso: RecibirEvento,
    casos: dict[str, dict[str, Any]],
    firmar_caso: Callable[..., Any],
    segundos: int,
) -> None:
    entrada = json.loads(json.dumps(casos["justoEnElUmbral"]))
    entrada["envio"]["marcaTiempo"]["desfaseSegundos"] = segundos
    recepcion = await _enviar(caso, firmar_caso(entrada, AHORA))
    assert not recepcion.duplicado


@pytest.mark.parametrize("segundos", [-301, 301])
async def test_un_segundo_mas_alla_del_limite_se_rechaza(
    caso: RecibirEvento,
    casos: dict[str, dict[str, Any]],
    firmar_caso: Callable[..., Any],
    segundos: int,
) -> None:
    entrada = json.loads(json.dumps(casos["justoEnElUmbral"]))
    entrada["envio"]["marcaTiempo"]["desfaseSegundos"] = segundos
    with pytest.raises(MarcaFueraDeRango):
        await _enviar(caso, firmar_caso(entrada, AHORA))


async def test_sin_secreto_no_se_acepta_ningun_envio(
    crear_caso: Callable[..., RecibirEvento],
    archivo: Any,
    registro: Any,
    casos: dict[str, dict[str, Any]],
    firmar_caso: Callable[..., Any],
) -> None:
    sin_secreto = crear_caso(secreto=None)
    with pytest.raises(FirmaInvalida):
        await _enviar(sin_secreto, firmar_caso(casos["valido"], AHORA))
    assert archivo.guardados == [] and registro.llamadas == []


async def test_otro_secreto_se_rechaza(
    caso: RecibirEvento, casos: dict[str, dict[str, Any]], firmar_caso: Callable[..., Any]
) -> None:
    with pytest.raises(FirmaInvalida):
        await _enviar(caso, firmar_caso(casos["valido"], AHORA, "otro-secreto"))


@pytest.mark.parametrize("encabezado", ["fuente", "marca", "firma"])
async def test_un_encabezado_ausente_es_firma_invalida(
    caso: RecibirEvento,
    casos: dict[str, dict[str, Any]],
    firmar_caso: Callable[..., Any],
    encabezado: str,
) -> None:
    envio = firmar_caso(casos["valido"], AHORA)
    argumentos: dict[str, Any] = {
        "fuente": envio.fuente,
        "marca": envio.marca_tiempo,
        "firma": envio.firma,
    }
    argumentos[encabezado] = None
    with pytest.raises(FirmaInvalida):
        await caso(
            argumentos["fuente"], argumentos["marca"], argumentos["firma"], envio.cuerpo, "c"
        )


async def test_una_marca_ilegible_con_la_firma_correcta_esta_fuera_de_rango(
    caso: RecibirEvento,
) -> None:
    cuerpo = b"{}"
    for marca in ("ayer", "2026-11-10T14:00:05", ""):
        with pytest.raises(MarcaFueraDeRango):
            await caso("IDEAM-SIM", marca, calcular_firma(SECRETO, marca, cuerpo), cuerpo, "c")


async def test_la_fuente_firmada_pero_fuera_de_la_lista_se_rechaza(
    caso: RecibirEvento, casos: dict[str, dict[str, Any]], firmar_caso: Callable[..., Any]
) -> None:
    with pytest.raises(FirmaInvalida):
        await _enviar(caso, firmar_caso(casos["fuenteDesconocida"], AHORA))


# --- Fallos de las dependencias ------------------------------------------------------------------
async def test_si_el_archivo_falla_no_se_registra_nada(
    archivo: Any,
    registro: Any,
    caso: RecibirEvento,
    casos: dict[str, dict[str, Any]],
    firmar_caso: Callable[..., Any],
) -> None:
    archivo.falla = True
    with pytest.raises(DependenciaNoDisponible):
        await _enviar(caso, firmar_caso(casos["valido"], AHORA))
    assert registro.llamadas == ["recibido"]
    assert registro.outbox == []


@pytest.mark.parametrize("donde", ["falla_lectura", "falla_registro"])
async def test_si_la_base_falla_el_error_es_de_dependencia(
    registro: Any,
    caso: RecibirEvento,
    casos: dict[str, dict[str, Any]],
    firmar_caso: Callable[..., Any],
    donde: str,
) -> None:
    setattr(registro, donde, True)
    with pytest.raises(DependenciaNoDisponible):
        await _enviar(caso, firmar_caso(casos["valido"], AHORA))
    assert registro.outbox == []


# --- Medición y logs (SEC-W21-01) ----------------------------------------------------------------
async def test_la_medicion_y_el_log_de_cierre_por_resultado(
    caso: RecibirEvento,
    telemetria: Any,
    casos: dict[str, dict[str, Any]],
    firmar_caso: Callable[..., Any],
) -> None:
    esperados = {
        "valido": "aceptado",
        "duplicado": "duplicado",
        "firmaInvalida": "firma_invalida",
        "marcaDeTiempoVieja": "marca_invalida",
        "malformadoNoJson": "contrato_invalido",
    }
    for clave, resultado in esperados.items():
        try:
            await _enviar(caso, firmar_caso(casos[clave], AHORA))
        except (FirmaInvalida, MarcaFueraDeRango, ContratoInvalido):
            pass
        operaciones = [r for r in telemetria.logs() if r.get("operation") == "telemetria.recibir"]
        assert operaciones[-1]["result"] == resultado, clave
    cierres = [r for r in telemetria.logs() if r["message"] == "telemetria: evento recibido"]
    assert [c["resultado"] for c in cierres] == list(esperados.values())
    valido = evento_id("IDEAM-SIM", "IDEAM-2026-1110-0001")
    assert cierres[0]["evento_id"] == str(valido) and cierres[0]["fuente"] == "IDEAM-SIM"
    # Antes de autenticar, la fuente y el cuerpo son de quien llama: no se registran.
    assert "evento_id" not in cierres[2] and "fuente" not in cierres[2]
    assert "evento_id" not in cierres[3] and "fuente" not in cierres[3]
    # El contrato inválido ya es de una fuente autenticada, pero su cuerpo no es de confianza.
    assert cierres[4]["fuente"] == "IDEAM-SIM" and "evento_id" not in cierres[4]


async def test_los_logs_no_traen_el_secreto_la_firma_ni_el_cuerpo(
    caso: RecibirEvento,
    telemetria: Any,
    datos: dict[str, Any],
    firmar_caso: Callable[..., Any],
) -> None:
    envios = []
    for entrada in datos["eventos"]:
        envio = firmar_caso(entrada, AHORA)
        envios.append(envio)
        try:
            await _enviar(caso, envio)
        except (FirmaInvalida, MarcaFueraDeRango, ContratoInvalido):
            pass
    texto = telemetria.texto_logs()
    assert texto
    assert SECRETO not in texto
    for envio in envios:
        if envio.firma:
            assert envio.firma not in texto
            assert envio.firma.removeprefix("sha256=") not in texto
        assert envio.cuerpo.decode() not in texto
    # Valores distintivos (un "135" suelto coincidiría con un UUID o una duración).
    for valor in ("IDEAM-2026-1110-0001", "EVT-2026-0913", "LLUVIA_ACUMULADA", "mucha", "contacto"):
        assert valor not in texto


async def test_un_rechazo_deja_el_resultado_pero_no_cuenta_como_error_del_servicio(
    caso: RecibirEvento,
    telemetria: Any,
    casos: dict[str, dict[str, Any]],
    firmar_caso: Callable[..., Any],
) -> None:
    with pytest.raises(FirmaInvalida):
        await _enviar(caso, firmar_caso(casos["firmaInvalida"], AHORA))
    errores = telemetria.puntos("OperationErrors")
    assert errores and sum(p.value for p in errores) == 0


def test_el_modelo_canonico_se_arma_desde_el_cuerpo_validado(
    casos: dict[str, dict[str, Any]],
) -> None:
    cuerpo = json.dumps(casos["valido"]["envio"]["cuerpo"]).encode()
    assert validar_contrato(cuerpo, "IDEAM-SIM").zona == "11001"
