from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from cotizacion.domain.perfilamiento import errores
from cotizacion.domain.perfilamiento.modelo_riesgo import (
    Condicion,
    DefAjuste,
    DefFactor,
    Expresion,
    ModeloRiesgo,
    ReglaFactor,
    UmbralNivel,
)
from cotizacion.domain.perfilamiento.modelos import (
    PROPOSITO_PERFILAMIENTO,
    TIPO_DE_FUENTE,
    AccionSugerida,
    ContextoConsulta,
    DatoRequerido,
    DatosDeclarados,
    DecisionConsentimiento,
    Direccion,
    EstadoCampo,
    EstadoFuente,
    EstadoPerfil,
    Fuente,
    MotivoDato,
    NivelRiesgo,
    PlanFuentes,
    ResultadoEvaluacion,
    ResultadoFuente,
    SolicitudFuente,
    TipoFuente,
    ValorCampo,
    VerificacionConsentimiento,
)

CLIENTE = UUID("00000000-0000-4000-8000-000000000001")


def _modelo() -> ModeloRiesgo:
    return ModeloRiesgo(
        version="2026.10.1",
        hash="h",
        producto_id="vida-hipotecario",
        proposito=PROPOSITO_PERFILAMIENTO,
        fuentes={
            Fuente.FA_HISTORIAL_PAGOS_12M: ("moras_12m",),
            Fuente.DA_REGISTROS_PUBLICOS: ("coincidencias_listas_restrictivas",),
        },
        declarados=("edad",),
        obligatorios=frozenset({"edad", "moras_12m"}),
        derivados={"x": Expresion("div", ("moras_12m", Decimal(12)))},
        factores=(
            DefFactor(
                "PUNTUALIDAD_PAGOS",
                "FA_HISTORIAL_PAGOS_12M",
                Decimal("1.00"),
                (
                    ReglaFactor(
                        (Condicion("moras_12m", "==", Decimal(0)),),
                        Decimal("0.9"),
                        Direccion.REDUCE_RIESGO,
                    ),
                ),
            ),
        ),
        ajustes=(
            DefAjuste(
                "COINCIDENCIA_LISTAS_RESTRICTIVAS",
                (Condicion("coincidencias_listas_restrictivas", ">=", Decimal(1)),),
                "FORZAR_NIVEL",
                NivelRiesgo.ALTO,
            ),
        ),
        niveles=(
            UmbralNivel(Decimal(70), NivelRiesgo.BAJO),
            UmbralNivel(Decimal(0), NivelRiesgo.ALTO),
        ),
        penalizacion_no_autorizada=Decimal("0.05"),
        penalizacion_no_obtenida=Decimal("0.15"),
    )


def test_tipo_de_cada_fuente() -> None:
    assert set(TIPO_DE_FUENTE) == set(Fuente)
    assert TIPO_DE_FUENTE[Fuente.DA_REGISTROS_PUBLICOS] is TipoFuente.DATOS_ABIERTOS
    with pytest.raises(TypeError):
        TIPO_DE_FUENTE[Fuente.DA_REGISTROS_PUBLICOS] = TipoFuente.DECLARADO  # type: ignore[index]


def test_enumeraciones_son_textos() -> None:
    assert EstadoFuente.NO_CONSULTADA == "NO_CONSULTADA"
    assert MotivoDato("NO_AUTORIZADO") is MotivoDato.NO_AUTORIZADO
    assert len(EstadoFuente) == 6
    assert len(EstadoPerfil) == 3


def test_fuente_de_campo() -> None:
    modelo = _modelo()
    assert modelo.fuente_de_campo("moras_12m") is Fuente.FA_HISTORIAL_PAGOS_12M
    assert modelo.fuente_de_campo("edad") is None


def test_construccion_e_inmutabilidad() -> None:
    ahora = datetime(2026, 10, 15, 15, tzinfo=UTC)
    instancias = [
        ValorCampo(EstadoCampo.DISPONIBLE, Decimal(0)),
        SolicitudFuente(Fuente.FA_HISTORIAL_PAGOS_12M, ("moras_12m",)),
        ContextoConsulta(CLIENTE, None, PROPOSITO_PERFILAMIENTO, 290),
        ResultadoFuente(
            Fuente.FA_HISTORIAL_PAGOS_12M,
            EstadoFuente.CONSULTADA,
            ("moras_12m",),
            {"moras_12m": ValorCampo(EstadoCampo.DISPONIBLE, Decimal(0))},
            "3.1",
            Decimal("0.91"),
            ahora,
            50,
            "SIM-OPEN-FINANCE",
        ),
        DatosDeclarados(date(1990, 4, 18), 180, Decimal(180000000)),
        VerificacionConsentimiento(
            DecisionConsentimiento.RECHAZADO, None, None, frozenset(), "REVOCADO"
        ),
        PlanFuentes((), (Fuente.FA_INGRESOS_AGREGADOS,), ()),
        DatoRequerido("moras_12m", Fuente.FA_HISTORIAL_PAGOS_12M, MotivoDato.NO_AUTORIZADO),
        ResultadoEvaluacion(
            EstadoPerfil.INFORMACION_INSUFICIENTE,
            None,
            None,
            Decimal(0),
            (),
            (),
            (),
            AccionSugerida.COMPLETAR_CONSENTIMIENTO,
        ),
        _modelo(),
    ]
    for instancia in instancias:
        primer_campo = instancia.__slots__[0]  # type: ignore[attr-defined]
        with pytest.raises(FrozenInstanceError):
            setattr(instancia, primer_campo, None)


def test_errores_con_atributos() -> None:
    assert errores.DependenciaNoDisponible("clientes.identidad").dependencia == "clientes.identidad"
    assert errores.DatosDeclaradosInvalidos(("plazoCreditoMeses",)).campos == ("plazoCreditoMeses",)
    for clase in (
        errores.ClienteNoEncontrado,
        errores.IdentidadNoVerificada,
        errores.IdentidadPendiente,
        errores.ModeloNoDisponible,
        errores.ConflictoIdempotencia,
        errores.PerfilNoEncontrado,
    ):
        assert issubclass(clase, Exception)
