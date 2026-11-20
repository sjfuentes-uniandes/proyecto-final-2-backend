"""Creación del siniestro paramétrico (HU-W22, W22-B07): una vez por evento y póliza, con su historial,
su evento en el Outbox y su clave de idempotencia en la misma transacción."""

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from siniestros.application.siniestro.crear_parametrico import (
    PREFIJO_CLAVE,
    CrearParametrico,
    huella_de,
)
from siniestros.application.siniestro.errores import IdempotenciaConflicto, SolicitudInvalida
from siniestros.application.siniestro.vistas import Plazos
from siniestros.domain.siniestro.modelos import EstadoSiniestro, OrigenSiniestro
from siniestros.domain.siniestro.parametrico import DatosParametrico, nuevo_siniestro
from siniestros.domain.siniestro.tramite import ACCION_CREADO_AUTOMATICAMENTE

pytestmark = pytest.mark.anyio


@pytest.fixture
def crear(repo, codigos, reloj, plazos: Plazos) -> CrearParametrico:  # type: ignore[no-untyped-def]
    return CrearParametrico(repo, codigos, reloj, plazos)


async def test_crea_un_siniestro_aprobado_con_su_historial_su_evento_y_su_clave(
    crear: CrearParametrico,
    repo,
    datos_parametrico: Callable[..., DatosParametrico],
    ahora,  # type: ignore[no-untyped-def]
) -> None:
    datos = datos_parametrico()

    resultado = await crear.ejecutar(datos.clave, datos)

    assert resultado.creado is True
    s = resultado.resumen.siniestro
    assert repo.siniestros[s.id] == s
    assert (s.codigo, s.origen, s.estado) == (
        "88-2026-1000",
        OrigenSiniestro.PARAMETRICO,
        EstadoSiniestro.APROBADO,
    )
    # AC1: sin analista, con el evento, la regla y la versión.
    assert s.analista_id is None and s.evento == datos.evento
    assert (s.evento.regla, s.evento.version_regla) == ("VALOR_IGUAL_O_SUPERIOR_AL_UMBRAL", "1")
    assert (resultado.resumen.tiempo_restante_min, resultado.resumen.siniestro.decidido_en) == (
        2880,
        ahora,
    )
    # El historial automático: el actor es el sistema, sin actor ni nombre, y sin la medición.
    (entrada,) = repo.historial
    assert (entrada.accion, entrada.estado_anterior, entrada.estado_nuevo) == (
        ACCION_CREADO_AUTOMATICAMENTE,
        None,
        EstadoSiniestro.APROBADO,
    )
    assert (entrada.actor_id, entrada.actor_rol, entrada.actor_nombre) == (
        None,
        "SISTEMA_PARAMETRICO",
        None,
    )
    assert entrada.detalles == {
        "eventoId": str(datos.evento.evento_id),
        "polizaId": str(datos.poliza_id),
        "regla": "VALOR_IGUAL_O_SUPERIOR_AL_UMBRAL",
        "versionRegla": "1",
    }
    # Un solo SiniestroAprobado, sin quien decide (R-65) y con la referencia única del pago.
    ((tipo, siniestro_id, referencia, decidido_por, aprobado_en),) = repo.eventos
    assert (tipo, siniestro_id, referencia, decidido_por, aprobado_en) == (
        "SiniestroAprobado",
        s.id,
        "IND-88-2026-1000-01",
        None,
        ahora,
    )
    registro = repo.idempotencia[f"{PREFIJO_CLAVE}{datos.clave}"]
    assert registro.huella == huella_de(datos)
    assert registro.respuesta == {"siniestroId": str(s.id)}


async def test_el_valor_y_la_clave_vienen_de_la_cobertura(
    crear: CrearParametrico, datos_parametrico: Callable[..., DatosParametrico]
) -> None:
    datos = datos_parametrico(valor=Decimal("620000.00"))

    resultado = await crear.ejecutar(datos.clave.upper(), datos)  # la clave no distingue mayúsculas

    s = resultado.resumen.siniestro
    assert (s.valor_indemnizar, s.deducible) == (Decimal("620000.00"), Decimal("0.00"))


async def test_la_repeticion_devuelve_el_mismo_siniestro_sin_escribir_ni_gastar_un_codigo(
    crear: CrearParametrico,
    repo,
    codigos,
    datos_parametrico: Callable[..., DatosParametrico],  # type: ignore[no-untyped-def]
) -> None:
    datos = datos_parametrico()
    primero = await crear.ejecutar(datos.clave, datos)

    segundo = await crear.ejecutar(datos.clave, datos)

    assert segundo.creado is False
    assert segundo.resumen.siniestro.id == primero.resumen.siniestro.id
    assert len(repo.siniestros) == 1 and len(repo.historial) == 1 and len(repo.eventos) == 1
    assert codigos.anios == [2026]  # solo la primera pidió consecutivo


async def test_la_misma_clave_con_otro_cuerpo_es_un_conflicto_y_no_cambia_nada(
    crear: CrearParametrico,
    repo,
    datos_parametrico: Callable[..., DatosParametrico],  # type: ignore[no-untyped-def]
) -> None:
    datos = datos_parametrico()
    await crear.ejecutar(datos.clave, datos)

    with pytest.raises(IdempotenciaConflicto):
        await crear.ejecutar(datos.clave, datos_parametrico(valor=Decimal("620000.00")))

    assert len(repo.siniestros) == 1 and len(repo.eventos) == 1


async def test_una_clave_que_no_es_la_pareja_evento_poliza_es_invalida(
    crear: CrearParametrico,
    repo,
    codigos,
    datos_parametrico: Callable[..., DatosParametrico],  # type: ignore[no-untyped-def]
) -> None:
    datos = datos_parametrico()

    with pytest.raises(SolicitudInvalida) as error:
        await crear.ejecutar(f"{uuid4()}:{datos.poliza_id}", datos)

    assert error.value.detalles == {"campos": ["Idempotency-Key"]}
    assert not repo.siniestros and not repo.idempotencia and not codigos.anios


async def test_si_la_clave_se_purgo_el_par_ya_existente_se_devuelve_y_se_vuelve_a_guardar(
    crear: CrearParametrico,
    repo,
    codigos,
    ahora,
    datos_parametrico: Callable[..., DatosParametrico],  # type: ignore[no-untyped-def]
) -> None:
    datos = datos_parametrico()
    existente = nuevo_siniestro(
        datos, siniestro_id=uuid4(), codigo="88-2026-0999", ahora=ahora, correlation_id="c"
    )
    repo.agregar(existente)

    resultado = await crear.ejecutar(datos.clave, datos)

    assert (resultado.creado, resultado.resumen.siniestro.id) == (False, existente.id)
    assert not codigos.anios and not repo.eventos and not repo.historial
    assert repo.idempotencia[f"{PREFIJO_CLAVE}{datos.clave}"].respuesta == {
        "siniestroId": str(existente.id)
    }


async def test_si_otra_replica_gana_mientras_se_pide_el_codigo_la_barrera_de_negocio_lo_resuelve(
    repo,
    codigos,
    reloj,
    plazos: Plazos,
    ahora,
    datos_parametrico: Callable[..., DatosParametrico],  # type: ignore[no-untyped-def]
) -> None:
    datos = datos_parametrico()
    ganador = nuevo_siniestro(
        datos, siniestro_id=uuid4(), codigo="88-2026-0998", ahora=ahora, correlation_id="c"
    )
    original = codigos.siguiente

    async def siguiente_con_carrera(anio: int) -> str:
        codigo = await original(anio)
        repo.siniestros[ganador.id] = ganador  # la otra réplica hizo commit entre las dos lecturas
        return codigo

    codigos.siguiente = siguiente_con_carrera  # type: ignore[method-assign]

    resultado = await CrearParametrico(repo, codigos, reloj, plazos).ejecutar(datos.clave, datos)

    assert (resultado.creado, resultado.resumen.siniestro.id) == (False, ganador.id)
    # Ni historial ni SiniestroAprobado de más: solo existe el del ganador.
    assert len(repo.siniestros) == 1 and not repo.historial and not repo.eventos


async def test_el_codigo_se_pide_sin_ninguna_transaccion_abierta(
    repo,
    codigos,
    reloj,
    plazos: Plazos,
    datos_parametrico: Callable[..., DatosParametrico],  # type: ignore[no-untyped-def]
) -> None:
    abiertas: list[bool] = []
    original = codigos.siguiente

    async def siguiente_vigilado(anio: int) -> str:
        abiertas.append(repo.tx_abierta)
        return await original(anio)

    codigos.siguiente = siguiente_vigilado  # type: ignore[method-assign]
    datos = datos_parametrico()

    await CrearParametrico(repo, codigos, reloj, plazos).ejecutar(datos.clave, datos)

    assert abiertas == [False]  # nunca dos conexiones del pool anidadas


async def test_el_anio_del_codigo_es_el_de_colombia(
    repo,
    codigos,
    plazos: Plazos,
    datos_parametrico: Callable[..., DatosParametrico],  # type: ignore[no-untyped-def]
) -> None:
    class Reloj:
        def ahora(self) -> datetime:
            return datetime(2027, 1, 1, 3, 0, tzinfo=UTC)  # 31 dic 2026 22:00 en Colombia

    datos = datos_parametrico()
    resultado = await CrearParametrico(repo, codigos, Reloj(), plazos).ejecutar(datos.clave, datos)

    assert codigos.anios == [2026]
    assert resultado.resumen.siniestro.codigo == "88-2026-1000"


async def test_la_medicion_distingue_creado_existente_y_rechazado(
    crear: CrearParametrico, telemetria: Any, datos_parametrico: Callable[..., DatosParametrico]
) -> None:
    datos = datos_parametrico()
    await crear.ejecutar(datos.clave, datos)
    await crear.ejecutar(datos.clave, datos)
    with pytest.raises(IdempotenciaConflicto):
        await crear.ejecutar(datos.clave, replace(datos, valor=Decimal("1.00")))

    resultados = [
        s.attributes["result"]
        for s in telemetria.lista_spans()
        if s.name == "siniestro.crear_parametrico"
    ]
    assert resultados == ["creado", "existente", "rechazado"]


def test_la_huella_depende_del_cuerpo_y_no_de_la_hora(
    datos_parametrico: Callable[..., DatosParametrico],
) -> None:
    uno, otro = datos_parametrico(), datos_parametrico()
    assert huella_de(uno) == huella_de(otro)
    assert huella_de(uno) != huella_de(datos_parametrico(valor=Decimal("1.00")))
    assert huella_de(uno) != huella_de(datos_parametrico(numero="250-2026-000102"))


async def test_si_el_insert_pierde_la_carrera_se_relee_el_ganador_y_no_se_escribe_nada_mas(
    repo,
    codigos,
    reloj,
    plazos: Plazos,
    ahora,
    datos_parametrico: Callable[..., DatosParametrico],  # type: ignore[no-untyped-def]
) -> None:
    """`ON CONFLICT DO NOTHING`: las dos lecturas previas no ven al ganador (aún no hacía commit) y
    el `INSERT` lo descubre. Se responde 200 con el existente, sin historial ni evento."""
    datos = datos_parametrico()
    ganador = nuevo_siniestro(
        datos, siniestro_id=uuid4(), codigo="88-2026-0997", ahora=ahora, correlation_id="c"
    )
    abrir = repo.transaccion
    lecturas = {"vistas": 0}

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def transaccion():  # type: ignore[no-untyped-def]
        async with abrir() as tx:
            real = tx.siniestro_parametrico

            async def invisible(evento_id, poliza_id):  # type: ignore[no-untyped-def]
                lecturas["vistas"] += 1
                # Las dos primeras lecturas (la corta y la de la transacción de escritura) no lo ven.
                return None if lecturas["vistas"] <= 2 else await real(evento_id, poliza_id)

            async def insertar(siniestro):  # type: ignore[no-untyped-def]
                repo.siniestros[ganador.id] = ganador  # el ganador hace commit justo ahora
                return False

            tx.siniestro_parametrico = invisible
            tx.insertar_parametrico = insertar
            yield tx

    repo.transaccion = transaccion  # type: ignore[method-assign]

    resultado = await CrearParametrico(repo, codigos, reloj, plazos).ejecutar(datos.clave, datos)

    assert (resultado.creado, resultado.resumen.siniestro.id) == (False, ganador.id)
    assert not repo.historial and not repo.eventos
    assert repo.idempotencia[f"{PREFIJO_CLAVE}{datos.clave}"].respuesta == {
        "siniestroId": str(ganador.id)
    }
