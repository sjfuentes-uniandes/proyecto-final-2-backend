"""N1 del caso de uso calcular_prima (HU-W03) con los puertos en fakes y el reloj en 2026-10-15."""

import asyncio
from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from cotizacion.application.cotizacion.calcular_prima import (
    DEPENDENCIA_CLIENTE,
    DEPENDENCIA_PERFIL,
    DEPENDENCIA_PRODUCTO,
    DEPENDENCIA_REGLAS,
    huella_solicitud,
)
from cotizacion.application.cotizacion.puertos import OrigenDato
from cotizacion.domain.cotizacion.errores import (
    CoberturaNoDisponible,
    DatosFueraDeRango,
    PerfilNoCorresponde,
    ProductoFueraDeVigencia,
    ProductoInactivo,
    ProductoNoEncontrado,
    ReglasNoVigentes,
    RolNoPermitido,
)
from cotizacion.domain.cotizacion.modelos import Actor, EstadoCotizacion, EstadoProducto, RolActor
from cotizacion.domain.perfilamiento.errores import (
    ClienteNoEncontrado,
    ConflictoIdempotencia,
    DependenciaNoDisponible,
    PerfilNoEncontrado,
)

pytestmark = pytest.mark.anyio
SOCIO = Actor("SOC-000001", RolActor.SOCIO)
PERFIL_C01 = UUID("00000000-0000-4000-a000-000000000001")


async def test_ac1_calcula_en_la_moneda_del_producto_y_conserva_la_version(
    escenario_cotizacion: Any, nueva_cotizacion: Any
) -> None:
    cotizacion, pasos, creada = await escenario_cotizacion.caso().ejecutar(nueva_cotizacion())
    assert creada
    assert (cotizacion.moneda, cotizacion.periodicidad) == ("COP", "MENSUAL")
    assert (cotizacion.version_producto, cotizacion.version_reglas) == ("2026.09.1", "2026.09.1")
    assert cotizacion.hash_reglas.startswith("sha256:")
    assert cotizacion.estado is EstadoCotizacion.CALCULADA
    assert cotizacion.desglose.prima_total == Decimal(82593)
    assert cotizacion.entradas.edad == 36
    assert cotizacion.socio_id is None and cotizacion.perfil_id is None
    assert [c.codigo for c in cotizacion.coberturas] == ["MUERTE", "INCAPACIDAD_TOTAL_PERMANENTE"]
    assert escenario_cotizacion.repo.cotizaciones[cotizacion.id] is cotizacion
    assert [p.codigo for p in pasos] == ["CATALOGO_Y_REGLAS", "CLIENTE", "CALCULO_PRIMA"]
    assert pasos[0].origen is OrigenDato.CATALOGO


async def test_con_perfil_usa_su_nivel(
    escenario_cotizacion: Any, nueva_cotizacion: Any, perfil_c01: Any
) -> None:
    cotizacion, pasos, _ = await escenario_cotizacion.caso().ejecutar(
        nueva_cotizacion(perfil_id=PERFIL_C01)
    )
    assert cotizacion.perfil_id == PERFIL_C01
    assert cotizacion.entradas.nivel_riesgo is perfil_c01.resultado.nivel
    assert cotizacion.desglose.factores[0].valor == perfil_c01.resultado.nivel.value
    assert "PERFIL_RIESGO" in [p.codigo for p in pasos]


async def test_perfil_sin_nivel_aplica_sin_perfil(
    escenario_cotizacion: Any, nueva_cotizacion: Any, perfil_c01: Any
) -> None:
    sin_nivel = replace(perfil_c01, resultado=replace(perfil_c01.resultado, nivel=None))
    escenario_cotizacion.perfiles.perfiles[PERFIL_C01] = sin_nivel
    cotizacion, _, _ = await escenario_cotizacion.caso().ejecutar(
        nueva_cotizacion(perfil_id=PERFIL_C01)
    )
    assert cotizacion.desglose.factores[0].valor == "SIN_PERFIL"


async def test_coberturas_opcionales_repetidas_cuentan_una_vez(
    escenario_cotizacion: Any, nueva_cotizacion: Any
) -> None:
    cotizacion, _, _ = await escenario_cotizacion.caso().ejecutar(
        nueva_cotizacion(coberturas_opcionales=("AUXILIO_FUNERARIO", "AUXILIO_FUNERARIO"))
    )
    assert cotizacion.entradas.coberturas_opcionales == ("AUXILIO_FUNERARIO",)
    assert [c.codigo for c in cotizacion.coberturas][-1] == "AUXILIO_FUNERARIO"


# --- AC3 e idempotencia --------------------------------------------------------------------


async def test_ac3_misma_solicitud_misma_prima_y_desglose(
    escenario_cotizacion: Any, nueva_cotizacion: Any
) -> None:
    caso = escenario_cotizacion.caso()
    primera, _, _ = await caso.ejecutar(nueva_cotizacion())
    segunda, _, _ = await caso.ejecutar(
        nueva_cotizacion(idempotency_key=UUID("00000000-0000-4000-c000-000000000002"))
    )
    assert primera.id != segunda.id
    assert primera.desglose == segunda.desglose
    assert primera.huella_calculo == segunda.huella_calculo


async def test_misma_clave_y_mismo_cuerpo_devuelve_la_existente(
    escenario_cotizacion: Any, nueva_cotizacion: Any
) -> None:
    caso = escenario_cotizacion.caso()
    primera, _, _ = await caso.ejecutar(nueva_cotizacion())
    repetida, pasos, creada = await caso.ejecutar(nueva_cotizacion())
    assert (repetida, pasos, creada) == (primera, (), False)
    assert len(escenario_cotizacion.productos.llamadas) == 2  # solo la primera vez


async def test_misma_clave_con_otro_cuerpo_es_conflicto(
    escenario_cotizacion: Any, nueva_cotizacion: Any
) -> None:
    caso = escenario_cotizacion.caso()
    await caso.ejecutar(nueva_cotizacion())
    with pytest.raises(ConflictoIdempotencia):
        await caso.ejecutar(nueva_cotizacion(plazo_credito_meses=240))


async def test_la_clave_es_por_actor(escenario_cotizacion: Any, nueva_cotizacion: Any) -> None:
    caso = escenario_cotizacion.caso()
    await caso.ejecutar(nueva_cotizacion())
    _, _, creada = await caso.ejecutar(nueva_cotizacion(actor=SOCIO))
    assert creada


async def test_carrera_de_la_misma_clave_gana_la_primera(
    escenario_cotizacion: Any, nueva_cotizacion: Any
) -> None:
    caso = escenario_cotizacion.caso()
    ganadora, _, _ = await caso.ejecutar(nueva_cotizacion())
    repo = escenario_cotizacion.repo
    leer = repo.por_idempotencia
    lecturas = iter([None, ganadora])

    async def primero_nada(actor_id: str, clave: UUID) -> Any:
        return next(lecturas)

    repo.por_idempotencia = primero_nada
    repo.errores_al_guardar.append(ConflictoIdempotencia())
    cotizacion, _, creada = await caso.ejecutar(nueva_cotizacion())
    assert (cotizacion, creada) == (ganadora, False)
    repo.por_idempotencia = leer


async def test_conflicto_al_guardar_sin_ganadora_se_propaga(
    escenario_cotizacion: Any, nueva_cotizacion: Any
) -> None:
    escenario_cotizacion.repo.errores_al_guardar.append(ConflictoIdempotencia())
    with pytest.raises(ConflictoIdempotencia):
        await escenario_cotizacion.caso().ejecutar(nueva_cotizacion())


def test_la_huella_de_la_solicitud_ignora_el_orden_de_las_coberturas(nueva_cotizacion: Any) -> None:
    a = nueva_cotizacion(coberturas_opcionales=("ENFERMEDADES_GRAVES", "AUXILIO_FUNERARIO"))
    b = nueva_cotizacion(coberturas_opcionales=("AUXILIO_FUNERARIO", "ENFERMEDADES_GRAVES"))
    assert huella_solicitud(a) == huella_solicitud(b)
    assert huella_solicitud(a) != huella_solicitud(nueva_cotizacion())


# --- AC2: causas funcionales estables ------------------------------------------------------


@pytest.mark.parametrize(
    ("preparar", "causa"),
    [
        (lambda e: None, None),
        (lambda e: e.productos.productos.pop("vida-hipotecario"), ProductoNoEncontrado),
        (
            lambda e: e.productos.productos.update(
                {
                    "vida-hipotecario": replace(
                        e.productos.productos["vida-hipotecario"], estado=EstadoProducto.INACTIVO
                    )
                }
            ),
            ProductoInactivo,
        ),
        (lambda e: e.productos.reglas.pop("vida-hipotecario"), ReglasNoVigentes),
    ],
    ids=["nominal", "inexistente", "inactivo", "sin reglas"],
)
async def test_ac2_tabla_de_decision_en_el_caso_de_uso(
    escenario_cotizacion: Any, nueva_cotizacion: Any, preparar: Any, causa: Any
) -> None:
    preparar(escenario_cotizacion)
    caso = escenario_cotizacion.caso()
    if causa is None:
        await caso.ejecutar(nueva_cotizacion())
    else:
        with pytest.raises(causa):
            await caso.ejecutar(nueva_cotizacion())
        assert escenario_cotizacion.repo.cotizaciones == {}


async def test_ac2_producto_vencido(escenario_cotizacion: Any, nueva_cotizacion: Any) -> None:
    with pytest.raises(ProductoFueraDeVigencia):
        await escenario_cotizacion.caso().ejecutar(
            nueva_cotizacion(producto_id="vida-hipotecario-2025")
        )


async def test_el_producto_decide_antes_que_una_falla_tecnica_de_clientes(
    escenario_cotizacion: Any, nueva_cotizacion: Any
) -> None:
    escenario_cotizacion.productos.productos.pop("vida-hipotecario")
    escenario_cotizacion.identidad.respuesta = DependenciaNoDisponible("clientes")
    with pytest.raises(ProductoNoEncontrado):
        await escenario_cotizacion.caso().ejecutar(nueva_cotizacion())


@pytest.mark.parametrize(
    ("preparar", "dependencia"),
    [
        (
            lambda e: e.productos.errores_producto.update(
                {"vida-hipotecario": DependenciaNoDisponible("catalogo.productos")}
            ),
            DEPENDENCIA_PRODUCTO,
        ),
        (
            lambda e: e.productos.errores_reglas.update(
                {"vida-hipotecario": DependenciaNoDisponible("catalogo.productos")}
            ),
            DEPENDENCIA_REGLAS,
        ),
        (
            lambda e: setattr(e.identidad, "respuesta", DependenciaNoDisponible("clientes")),
            DEPENDENCIA_CLIENTE,
        ),
        (lambda e: setattr(e.productos, "demora_s", 0.3), DEPENDENCIA_PRODUCTO),
    ],
    ids=["producto", "reglas", "clientes", "timeout"],
)
async def test_fallas_tecnicas_fallan_cerrado(
    escenario_cotizacion: Any, nueva_cotizacion: Any, preparar: Any, dependencia: str
) -> None:
    preparar(escenario_cotizacion)
    with pytest.raises(DependenciaNoDisponible) as error:
        await escenario_cotizacion.caso(dependencia_timeout_ms=50).ejecutar(nueva_cotizacion())
    assert error.value.dependencia == dependencia
    assert escenario_cotizacion.repo.cotizaciones == {}


async def test_falla_tecnica_del_perfil(escenario_cotizacion: Any, nueva_cotizacion: Any) -> None:
    async def caida(perfil_id: UUID) -> Any:
        raise DependenciaNoDisponible("base")

    escenario_cotizacion.perfiles.por_id = caida
    with pytest.raises(DependenciaNoDisponible) as error:
        await escenario_cotizacion.caso().ejecutar(nueva_cotizacion(perfil_id=PERFIL_C01))
    assert error.value.dependencia == DEPENDENCIA_PERFIL


async def test_reglas_de_otra_moneda_violan_el_contrato(
    escenario_cotizacion: Any, nueva_cotizacion: Any
) -> None:
    reglas = escenario_cotizacion.productos.reglas
    reglas["vida-hipotecario"] = replace(reglas["vida-hipotecario"], moneda="USD")
    with pytest.raises(DependenciaNoDisponible) as error:
        await escenario_cotizacion.caso().ejecutar(nueva_cotizacion())
    assert error.value.dependencia == DEPENDENCIA_REGLAS


async def test_un_error_inesperado_no_se_disfraza(
    escenario_cotizacion: Any, nueva_cotizacion: Any
) -> None:
    escenario_cotizacion.productos.errores_producto["vida-hipotecario"] = RuntimeError("bug")
    with pytest.raises(RuntimeError):
        await escenario_cotizacion.caso().ejecutar(nueva_cotizacion())


async def test_cliente_inexistente(escenario_cotizacion: Any, nueva_cotizacion: Any) -> None:
    escenario_cotizacion.identidad.respuesta = ClienteNoEncontrado()
    with pytest.raises(ClienteNoEncontrado):
        await escenario_cotizacion.caso().ejecutar(nueva_cotizacion())


async def test_datos_fuera_de_rango(escenario_cotizacion: Any, nueva_cotizacion: Any) -> None:
    with pytest.raises(DatosFueraDeRango) as error:
        await escenario_cotizacion.caso().ejecutar(
            nueva_cotizacion(suma_asegurada=Decimal(1400000000), fecha_nacimiento=date(2015, 4, 18))
        )
    assert error.value.campos == (
        "datosAsegurado.sumaAsegurada",
        "datosAsegurado.fechaNacimiento",
    )


async def test_cobertura_no_disponible(escenario_cotizacion: Any, nueva_cotizacion: Any) -> None:
    with pytest.raises(CoberturaNoDisponible):
        await escenario_cotizacion.caso().ejecutar(
            nueva_cotizacion(coberturas_opcionales=("MUERTE",))
        )


# --- Perfil (0..1) -------------------------------------------------------------------------


async def test_perfil_inexistente(escenario_cotizacion: Any, nueva_cotizacion: Any) -> None:
    with pytest.raises(PerfilNoEncontrado):
        await escenario_cotizacion.caso().ejecutar(
            nueva_cotizacion(perfil_id=UUID("00000000-0000-4000-a000-0000000000ff"))
        )


@pytest.mark.parametrize(
    ("cambio", "campos"),
    [
        ({"cliente_id": UUID("00000000-0000-4000-8000-000000000002")}, ("perfilId",)),
        ({"plazo_credito_meses": 240}, ("datosAsegurado.plazoCreditoMeses",)),
        (
            {"suma_asegurada": Decimal(200000000), "fecha_nacimiento": date(1991, 1, 1)},
            ("datosAsegurado.fechaNacimiento", "datosAsegurado.sumaAsegurada"),
        ),
    ],
)
async def test_el_perfil_debe_corresponder(
    escenario_cotizacion: Any,
    nueva_cotizacion: Any,
    cambio: dict[str, Any],
    campos: tuple[str, ...],
) -> None:
    with pytest.raises(PerfilNoCorresponde) as error:
        await escenario_cotizacion.caso().ejecutar(nueva_cotizacion(perfil_id=PERFIL_C01, **cambio))
    assert error.value.campos == campos


async def test_perfil_de_otro_producto(
    escenario_cotizacion: Any, nueva_cotizacion: Any, perfil_c01: Any
) -> None:
    escenario_cotizacion.perfiles.perfiles[PERFIL_C01] = replace(perfil_c01, producto_id="otro")
    with pytest.raises(PerfilNoCorresponde) as error:
        await escenario_cotizacion.caso().ejecutar(nueva_cotizacion(perfil_id=PERFIL_C01))
    assert error.value.campos == ("perfilId",)


# --- Actor ---------------------------------------------------------------------------------


async def test_el_socio_cotiza_para_si(escenario_cotizacion: Any, nueva_cotizacion: Any) -> None:
    cotizacion, _, _ = await escenario_cotizacion.caso().ejecutar(nueva_cotizacion(actor=SOCIO))
    assert cotizacion.socio_id == SOCIO.id
    mismo, _, _ = await escenario_cotizacion.caso().ejecutar(
        nueva_cotizacion(
            actor=SOCIO,
            socio_id=SOCIO.id,
            idempotency_key=UUID("00000000-0000-4000-c000-000000000003"),
        )
    )
    assert mismo.socio_id == SOCIO.id


@pytest.mark.parametrize(
    "cambio", [{"perfil_id": PERFIL_C01}, {"socio_id": "SOC-000002"}], ids=["perfil", "otro socio"]
)
async def test_el_socio_no_usa_perfiles_ni_otro_socio(
    escenario_cotizacion: Any, nueva_cotizacion: Any, cambio: dict[str, Any]
) -> None:
    with pytest.raises(RolNoPermitido):
        await escenario_cotizacion.caso().ejecutar(nueva_cotizacion(actor=SOCIO, **cambio))


async def test_el_asesor_indica_el_canal_del_socio(
    escenario_cotizacion: Any, nueva_cotizacion: Any
) -> None:
    cotizacion, _, _ = await escenario_cotizacion.caso().ejecutar(
        nueva_cotizacion(socio_id="SOC-000002")
    )
    assert cotizacion.socio_id == "SOC-000002"


# --- Telemetría y PII ----------------------------------------------------------------------


async def test_mide_la_operacion_y_no_registra_pii(
    escenario_cotizacion: Any, nueva_cotizacion: Any, telemetria: Any
) -> None:
    cotizacion, _, _ = await escenario_cotizacion.caso().ejecutar(
        nueva_cotizacion(coberturas_opcionales=("ENFERMEDADES_GRAVES",))
    )
    duraciones = {p.attributes["Operation"]: p for p in telemetria.puntos("OperationDuration")}
    assert "cotizacion" in duraciones
    assert "cotizacion.dependencia.catalogo.producto" in duraciones
    cierre = next(r for r in telemetria.logs() if r["message"] == "cotizacion calculada")
    assert cierre["cotizacion_id"] == str(cotizacion.id)
    assert cierre["operation"] == "cotizacion"
    assert {d["nombre"] for d in cierre["dependencias"]} == {
        DEPENDENCIA_PRODUCTO,
        DEPENDENCIA_REGLAS,
        DEPENDENCIA_CLIENTE,
        DEPENDENCIA_PERFIL,
    }
    texto = telemetria.texto_logs() + telemetria.texto_spans()
    for sensible in ("1990-04-18", "180000000", "82593", texto_prima(cotizacion)):
        assert sensible not in texto


def texto_prima(cotizacion: Any) -> str:
    return str(cotizacion.desglose.prima_total)


async def test_un_rechazo_se_mide_como_rechazado(
    escenario_cotizacion: Any, nueva_cotizacion: Any, telemetria: Any
) -> None:
    with pytest.raises(ProductoFueraDeVigencia):
        await escenario_cotizacion.caso().ejecutar(
            nueva_cotizacion(producto_id="vida-hipotecario-2025")
        )
    cierre = next(
        r for r in telemetria.logs() if r.get("operation") == "cotizacion" and r.get("result")
    )
    assert cierre["result"] == "rechazado"


async def test_las_dependencias_van_en_paralelo(
    escenario_cotizacion: Any, nueva_cotizacion: Any
) -> None:
    escenario_cotizacion.productos.demora_s = 0.05
    escenario_cotizacion.identidad.demora_s = 0.05
    inicio = asyncio.get_running_loop().time()
    await escenario_cotizacion.caso().ejecutar(nueva_cotizacion())
    assert asyncio.get_running_loop().time() - inicio < 0.09
