"""Migración 001 de siniestros contra PostgreSQL real (HU-W19, W19-B09)."""

from importlib import resources
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb
from siniestros.adapters.outbound.base_de_datos import migrar

ANALISTA = "a0a0a0a0-0000-4000-8000-000000000001"
SHA = "b" * 64


def insertar_siniestro(conexion: psycopg.Connection[Any], **cambios: Any) -> str:
    datos: dict[str, Any] = {
        "id": str(uuid4()),
        "codigo": f"88-2026-{uuid4().int % 9000 + 1000}",
        "origen": "MOVIL",
        "estado": "REPORTADO",
        "poliza_id": str(uuid4()),
        "cliente_id": str(uuid4()),
        "copia_poliza": Jsonb({"numero": "220-2026-011294"}),
        "causa": "DANO_ACCIDENTAL",
        "fecha_evento": "2026-11-04T19:00:00Z",
        "ubicacion_declarada": Jsonb({"lat": 4.65, "lon": -74.05}),
        "reportado_en": "2026-11-04T20:00:00Z",
        "correlation_id": "corr-w19",
    }
    datos.update(cambios)
    columnas = ", ".join(datos)
    marcas = ", ".join(["%s"] * len(datos))
    conexion.execute(f"INSERT INTO siniestros ({columnas}) VALUES ({marcas})", list(datos.values()))
    return str(datos["id"])


def insertar_evidencia(conexion: psycopg.Connection[Any], siniestro_id: str, **cambios: Any) -> str:
    identificador = str(uuid4())
    datos: dict[str, Any] = {
        "id": identificador,
        "siniestro_id": siniestro_id,
        "nombre_archivo": "foto.jpg",
        "tipo": "FOTOGRAFIA",
        "clave_objeto": f"siniestros/{siniestro_id}/{identificador}",
        "sha256": SHA,
    }
    datos.update(cambios)
    columnas = ", ".join(datos)
    marcas = ", ".join(["%s"] * len(datos))
    conexion.execute(f"INSERT INTO evidencias ({columnas}) VALUES ({marcas})", list(datos.values()))
    return identificador


def test_crea_las_ocho_tablas_con_sus_indices(conexion: psycopg.Connection[Any]) -> None:
    tablas = {
        fila[0]
        for fila in conexion.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        ).fetchall()
    }
    assert tablas == {
        "siniestros",
        "evidencias",
        "historial_siniestro",
        "decisiones_siniestro",
        "outbox",
        "inbox",
        "idempotencia",
        "secuencia_codigo",
    }
    indices = {
        fila[0]
        for fila in conexion.execute("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
    }
    assert {
        "siniestros_por_estado_analista",
        "siniestros_por_analista",
        "siniestros_por_poliza",
        "siniestros_cola",
        "evidencias_por_siniestro",
        "historial_por_siniestro",
        "outbox_pendientes",
        "decisiones_por_siniestro",
        "decisiones_una_final",
        "siniestros_plazo_soporte",
    } <= indices


def test_migrar_es_idempotente(base: str, conexion: psycopg.Connection[Any]) -> None:
    insertar_siniestro(conexion)
    migrar(base)
    migrar(base)
    assert conexion.execute("SELECT count(*) FROM siniestros").fetchone() == (1,)


def test_el_codigo_es_unico_y_tiene_su_forma(conexion: psycopg.Connection[Any]) -> None:
    insertar_siniestro(conexion, codigo="88-2026-0442")
    with pytest.raises(psycopg.errors.UniqueViolation):
        insertar_siniestro(conexion, codigo="88-2026-0442")
    with pytest.raises(psycopg.errors.CheckViolation):
        insertar_siniestro(conexion, codigo="99-2026-0442")


@pytest.mark.parametrize(
    "cambios",
    [
        {"origen": "WEB"},
        {"estado": "ABIERTO"},
        # EN_EVALUACION sin analista, y REPORTADO con analista.
        {"estado": "EN_EVALUACION"},
        {"analista_id": ANALISTA, "asignado_en": "2026-11-05T01:00:00Z"},
        # El analista y su fecha de asignación van juntos.
        {"estado": "APROBADO", "decidido_en": "2026-11-05T02:00:00Z", "analista_id": ANALISTA},
        # Con W20: plazo y liquidación según el estado.
        {"plazo_soporte_vence_en": "2026-11-19T02:00:00Z"},
        {"estado": "APROBADO", "decidido_en": "2026-11-05T02:00:00Z"},
        {"valor_indemnizar": 100},
        {"deducible": 10},
        {"estado": "RECHAZADO", "decidido_en": "2026-11-05T02:00:00Z", "valor_indemnizar": 100},
        {"deducible": -1},
        {"valor_indemnizar": 0},
        # La pausa solo en FALTA_SOPORTE; la decisión solo en un caso decidido.
        {"pausado_desde": "2026-11-05T01:00:00Z"},
        {"estado": "APROBADO"},
        {"decidido_en": "2026-11-05T01:00:00Z"},
        {"pausa_acumulada_s": -1},
        {"valor_reclamado": -5},
        {"moneda": "cop"},
        {"pago_estado": "ENVIADO"},
        {"version": 0},
        {"causa": "  "},
    ],
)
def test_las_invariantes_del_agregado_las_exige_la_base(
    conexion: psycopg.Connection[Any], cambios: dict[str, Any]
) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        insertar_siniestro(conexion, **cambios)


def test_un_caso_asignado_y_uno_decidido_por_paramétrico_son_validos(
    conexion: psycopg.Connection[Any],
) -> None:
    insertar_siniestro(
        conexion,
        estado="EN_EVALUACION",
        analista_id=ANALISTA,
        asignado_en="2026-11-05T01:00:00Z",
    )
    insertar_siniestro(
        conexion,
        estado="FALTA_SOPORTE",
        analista_id=ANALISTA,
        asignado_en="2026-11-05T01:00:00Z",
        pausado_desde="2026-11-05T02:00:00Z",
        plazo_soporte_vence_en="2026-11-19T02:00:00Z",
    )
    insertar_siniestro(
        conexion,
        origen="PARAMETRICO",
        estado="APROBADO",
        decidido_en="2026-11-05T03:00:00Z",
        valor_indemnizar=1500000,
    )
    assert conexion.execute("SELECT count(*) FROM siniestros").fetchone() == (3,)


def test_las_columnas_de_otras_historias_nacen_vacias(conexion: psycopg.Connection[Any]) -> None:
    identificador = insertar_siniestro(conexion)
    fila = conexion.execute(
        "SELECT analista_id, valor_reclamado, moneda, evento_id, regla, pago_estado, pago_intentos, "
        "pago_duplicados_descartados, decidido_en, plazo_soporte_vence_en, pausa_acumulada_s, version, "
        "deducible, valor_indemnizar FROM siniestros WHERE id = %s",
        (identificador,),
    ).fetchone()
    assert fila == (None, None, None, None, None, None, 0, 0, None, None, 0, 1, None, None)


def test_una_evidencia_pertenece_a_un_siniestro_y_su_clave_es_unica(
    conexion: psycopg.Connection[Any],
) -> None:
    siniestro = insertar_siniestro(conexion)
    evidencia = insertar_evidencia(conexion, siniestro)
    with pytest.raises(psycopg.errors.UniqueViolation):
        insertar_evidencia(
            conexion,
            siniestro,
            clave_objeto=f"siniestros/{siniestro}/{evidencia}",
        )
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        insertar_evidencia(conexion, str(uuid4()))


@pytest.mark.parametrize(
    "cambios",
    [
        {"tipo": "AUDIO"},
        {"sha256": "A" * 64},
        {"sha256": "b" * 63},
        {"nombre_archivo": " "},
        {"tamano_bytes": -1},
        # Un SOPORTE lleva su tipo de soporte, y una foto no.
        {"tipo": "SOPORTE"},
        {"soporte_tipo": "PERITAJE"},
        {"tipo": "SOPORTE", "soporte_tipo": "RECIBO"},
    ],
)
def test_las_invariantes_de_la_evidencia_las_exige_la_base(
    conexion: psycopg.Connection[Any], cambios: dict[str, Any]
) -> None:
    siniestro = insertar_siniestro(conexion)
    with pytest.raises(psycopg.errors.CheckViolation):
        insertar_evidencia(conexion, siniestro, **cambios)


def test_un_soporte_clasificado_es_valido(conexion: psycopg.Connection[Any]) -> None:
    siniestro = insertar_siniestro(conexion)
    insertar_evidencia(
        conexion, siniestro, tipo="SOPORTE", soporte_tipo="PERITAJE", nombre_archivo="peritaje.pdf"
    )
    assert conexion.execute("SELECT count(*) FROM evidencias").fetchone() == (1,)


def test_el_historial_es_de_solo_insercion(conexion: psycopg.Connection[Any]) -> None:
    siniestro = insertar_siniestro(conexion)
    entrada = str(uuid4())
    conexion.execute(
        "INSERT INTO historial_siniestro (id, siniestro_id, accion, estado_nuevo, actor_rol, "
        "ocurrido_en) VALUES (%s, %s, 'REPORTADO', 'REPORTADO', 'CLIENTE', now())",
        (entrada, siniestro),
    )
    with pytest.raises(psycopg.errors.RaiseException, match="solo inserción"):
        conexion.execute(
            "UPDATE historial_siniestro SET actor_nombre = 'Otro' WHERE id = %s", (entrada,)
        )
    with pytest.raises(psycopg.errors.RaiseException, match="solo inserción"):
        conexion.execute("DELETE FROM historial_siniestro WHERE id = %s", (entrada,))
    assert conexion.execute("SELECT count(*) FROM historial_siniestro").fetchone() == (1,)


@pytest.mark.parametrize(
    "accion", ["reportado", "AB", "UNA_ACCION_CON_UN_NOMBRE_DEMASIADO_LARGO_PARA_EL_CODIGO"]
)
def test_la_accion_del_historial_es_un_codigo(
    conexion: psycopg.Connection[Any], accion: str
) -> None:
    siniestro = insertar_siniestro(conexion)
    with pytest.raises(psycopg.errors.CheckViolation):
        conexion.execute(
            "INSERT INTO historial_siniestro (id, siniestro_id, accion, ocurrido_en) "
            "VALUES (%s, %s, %s, now())",
            (str(uuid4()), siniestro, accion),
        )


def test_outbox_e_inbox_tienen_el_esquema_de_dinero(conexion: psycopg.Connection[Any]) -> None:
    columnas = {
        tabla: [
            fila[0]
            for fila in conexion.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = %s ORDER BY ordinal_position",
                (tabla,),
            )
        ]
        for tabla in ("outbox", "inbox")
    }
    assert columnas["outbox"] == [
        "event_id",
        "aggregate_id",
        "event_type",
        "event_version",
        "occurred_at",
        "correlation_id",
        "payload",
        "published_at",
        "attempts",
        "last_error",
    ]
    assert columnas["inbox"] == ["event_id", "event_type", "recibido_en"]


def test_la_idempotencia_guarda_la_respuesta_por_clave(conexion: psycopg.Connection[Any]) -> None:
    insertar = (
        "INSERT INTO idempotencia (clave, huella_solicitud, respuesta, creada_en) "
        "VALUES (%s, 'h', %s, now())"
    )
    conexion.execute(insertar, ("siguiente:analista:clave-1", Jsonb({"id": "x"})))
    with pytest.raises(psycopg.errors.UniqueViolation):
        conexion.execute(insertar, ("siguiente:analista:clave-1", Jsonb({"id": "y"})))


def test_el_consecutivo_del_codigo_arranca_en_1000_y_sube_de_uno_en_uno(
    conexion: psycopg.Connection[Any],
) -> None:
    # La sentencia que usará el repositorio (B3): una transacción corta por código.
    avanzar = (
        "INSERT INTO secuencia_codigo (anio, ultimo) VALUES (%s, 1000) "
        "ON CONFLICT (anio) DO UPDATE SET ultimo = secuencia_codigo.ultimo + 1 RETURNING ultimo"
    )
    obtenidos = [conexion.execute(avanzar, (2026,)).fetchone() for _ in range(3)]
    assert obtenidos == [(1000,), (1001,), (1002,)]
    assert conexion.execute(avanzar, (2027,)).fetchone() == (1000,)
    with pytest.raises(psycopg.errors.CheckViolation):
        conexion.execute(avanzar, (1999,))


def _reversa(nombre: str) -> str:
    return (
        resources.files("siniestros.adapters.outbound")
        .joinpath("migraciones", "down", nombre)
        .read_text(encoding="utf-8")
    )


def test_la_reversa_borra_todo_y_se_puede_volver_a_migrar(base: str) -> None:
    # La reversa de 002 va antes que la de 001 (decisiones_siniestro referencia a siniestros).
    reversa = _reversa("002_decisiones.sql") + _reversa("001_siniestros.sql")
    with psycopg.connect(base, autocommit=True) as conexion:
        conexion.execute(reversa)
        assert conexion.execute(
            "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'"
        ).fetchone() == (0,)
        assert conexion.execute(
            "SELECT count(*) FROM pg_proc WHERE proname = 'historial_siniestro_solo_insercion'"
        ).fetchone() == (0,)
        # Repetirla no falla.
        conexion.execute(reversa)
    migrar(base)
    with psycopg.connect(base, autocommit=True) as conexion:
        assert conexion.execute(
            "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'"
        ).fetchone() == (8,)


# --- Migración 002: decisiones (HU-W20, W20-B05) ---------------------------------------------------


def insertar_decision(
    conexion: psycopg.Connection[Any], siniestro_id: str, tipo: str = "APROBAR", **cambios: Any
) -> str:
    datos: dict[str, Any] = {
        "id": str(uuid4()),
        "siniestro_id": siniestro_id,
        "tipo": tipo,
        "idempotency_key": str(uuid4()),
        "huella_solicitud": "h" * 64,
        "detalle": Jsonb({"justificacion": "ok"}),
        "actor_id": ANALISTA,
        "registrada_en": "2026-11-09T15:00:00Z",
    }
    datos.update(cambios)
    columnas = ", ".join(datos)
    marcas = ", ".join(["%s"] * len(datos))
    conexion.execute(
        f"INSERT INTO decisiones_siniestro ({columnas}) VALUES ({marcas})", list(datos.values())
    )
    return str(datos["id"])


def test_migrar_dos_veces_con_decisiones_no_falla(
    base: str, conexion: psycopg.Connection[Any]
) -> None:
    siniestro = insertar_siniestro(conexion)
    insertar_decision(conexion, siniestro)
    migrar(base)
    assert conexion.execute("SELECT count(*) FROM decisiones_siniestro").fetchone() == (1,)


def test_una_sola_decision_final_por_siniestro(conexion: psycopg.Connection[Any]) -> None:
    siniestro = insertar_siniestro(conexion)
    insertar_decision(conexion, siniestro, "APROBAR")
    for otro in ("APROBAR", "RECHAZAR"):
        with pytest.raises(psycopg.errors.UniqueViolation):
            insertar_decision(conexion, siniestro, otro)
    # Otro siniestro sí puede tener la suya.
    insertar_decision(conexion, insertar_siniestro(conexion), "RECHAZAR")


def test_pueden_existir_varios_pedidos_de_soporte_y_despues_una_decision_final(
    conexion: psycopg.Connection[Any],
) -> None:
    siniestro = insertar_siniestro(conexion)
    for _ in range(3):
        insertar_decision(conexion, siniestro, "SOLICITAR_SOPORTE")
    insertar_decision(conexion, siniestro, "RECHAZAR")
    assert conexion.execute(
        "SELECT count(*) FROM decisiones_siniestro WHERE siniestro_id = %s", (siniestro,)
    ).fetchone() == (4,)


def test_la_clave_de_idempotencia_de_la_decision_es_unica(
    conexion: psycopg.Connection[Any],
) -> None:
    clave = str(uuid4())
    insertar_decision(conexion, insertar_siniestro(conexion), idempotency_key=clave)
    with pytest.raises(psycopg.errors.UniqueViolation):
        insertar_decision(
            conexion, insertar_siniestro(conexion), "SOLICITAR_SOPORTE", idempotency_key=clave
        )


@pytest.mark.parametrize(
    "cambios",
    [
        {"tipo": "ANULAR"},
        {"huella_solicitud": " "},
        {"actor_id": " "},
        {"detalle": None},
    ],
)
def test_las_invariantes_de_la_decision_las_exige_la_base(
    conexion: psycopg.Connection[Any], cambios: dict[str, Any]
) -> None:
    siniestro = insertar_siniestro(conexion)
    with pytest.raises((psycopg.errors.CheckViolation, psycopg.errors.NotNullViolation)):
        insertar_decision(conexion, siniestro, **cambios)


def test_una_decision_pertenece_a_un_siniestro(conexion: psycopg.Connection[Any]) -> None:
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        insertar_decision(conexion, str(uuid4()))


def test_las_decisiones_son_de_solo_insercion(conexion: psycopg.Connection[Any]) -> None:
    decision = insertar_decision(conexion, insertar_siniestro(conexion))
    with pytest.raises(psycopg.errors.RaiseException, match="solo inserción"):
        conexion.execute(
            "UPDATE decisiones_siniestro SET tipo = 'RECHAZAR' WHERE id = %s", (decision,)
        )
    with pytest.raises(psycopg.errors.RaiseException, match="solo inserción"):
        conexion.execute("DELETE FROM decisiones_siniestro WHERE id = %s", (decision,))
    assert conexion.execute("SELECT count(*) FROM decisiones_siniestro").fetchone() == (1,)


def test_el_detalle_de_la_decision_esta_marcado_como_sensible(
    conexion: psycopg.Connection[Any],
) -> None:
    comentario = conexion.execute(
        "SELECT col_description('decisiones_siniestro'::regclass, "
        "(SELECT ordinal_position FROM information_schema.columns "
        "WHERE table_name = 'decisiones_siniestro' AND column_name = 'detalle'))"
    ).fetchone()
    assert comentario is not None and comentario[0].startswith("sensible")


def test_cada_transicion_cumple_las_invariantes_de_la_base(
    conexion: psycopg.Connection[Any],
) -> None:
    """Las CHECK de 001 y 002 aceptan el estado que deja cada transición de la tabla (DS3-02)."""
    sellos = {"asignado_en": "2026-11-05T01:00:00Z", "analista_id": ANALISTA}
    siniestro = insertar_siniestro(conexion)
    # ASIGNAR
    conexion.execute(
        "UPDATE siniestros SET estado='EN_EVALUACION', analista_id=%s, asignado_en=%s WHERE id=%s",
        (sellos["analista_id"], sellos["asignado_en"], siniestro),
    )
    # SOLICITAR_SOPORTE
    conexion.execute(
        "UPDATE siniestros SET estado='FALTA_SOPORTE', pausado_desde='2026-11-05T02:00:00Z', "
        "plazo_soporte_vence_en='2026-11-19T02:00:00Z' WHERE id=%s",
        (siniestro,),
    )
    # VENCER_PLAZO_SOPORTE
    conexion.execute(
        "UPDATE siniestros SET estado='EN_EVALUACION', pausado_desde=NULL, "
        "plazo_soporte_vence_en=NULL, pausa_acumulada_s=1209600 WHERE id=%s",
        (siniestro,),
    )
    # APROBAR
    conexion.execute(
        "UPDATE siniestros SET estado='APROBADO', decidido_en='2026-11-20T02:00:00Z', "
        "deducible=350000, valor_indemnizar=2500000 WHERE id=%s",
        (siniestro,),
    )
    otro = insertar_siniestro(conexion, estado="EN_EVALUACION", **sellos)
    # RECHAZAR
    conexion.execute(
        "UPDATE siniestros SET estado='RECHAZADO', decidido_en='2026-11-20T02:00:00Z' WHERE id=%s",
        (otro,),
    )
    # Y lo que ninguna transición deja: aprobado sin liquidación o pausado sin plazo.
    for cambio in (
        "estado='APROBADO', decidido_en=now()",
        "estado='FALTA_SOPORTE', pausado_desde=now()",
    ):
        libre = insertar_siniestro(conexion, estado="EN_EVALUACION", **sellos)
        with pytest.raises(psycopg.errors.CheckViolation):
            conexion.execute(f"UPDATE siniestros SET {cambio} WHERE id=%s", (libre,))


def test_la_reversa_de_002_deja_la_base_de_001(limpia: str) -> None:
    # `limpia`: reaplicar 002 con un caso APROBADO cuya liquidación se perdió en la reversa lo impide
    # la CHECK a propósito (la reversa es para pruebas y retrocesos planificados).
    base = limpia
    with psycopg.connect(base, autocommit=True) as conexion:
        conexion.execute(_reversa("002_decisiones.sql"))
        tablas = {
            f[0]
            for f in conexion.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            ).fetchall()
        }
        assert "decisiones_siniestro" not in tablas and "siniestros" in tablas
        columnas = {
            f[0]
            for f in conexion.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name='siniestros'"
            ).fetchall()
        }
        assert not {"deducible", "valor_indemnizar"} & columnas
        # Con 001 solamente, la reversa se repite sin fallar y 002 se vuelve a aplicar.
        conexion.execute(_reversa("002_decisiones.sql"))
    migrar(base)
    with psycopg.connect(base, autocommit=True) as conexion:
        assert conexion.execute(
            "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'"
        ).fetchone() == (8,)
