"""Pruebas del reconciliador del experimento multizona (HU-W29, AC2) con datos sintéticos.

uv run pytest experiments/hu-w29
"""

import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

_spec = importlib.util.spec_from_file_location(
    "reconciliar", Path(__file__).with_name("reconciliar.py")
)
assert _spec is not None and _spec.loader is not None
reconciliar = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reconciliar)

AHORA = datetime(2026, 11, 20, 14, 40, tzinfo=UTC)
VENTANA = ("2026-11-20T14:00:00Z", "2026-11-20T14:30:00Z")


def intento(
    paso: str, clave: str, status: int = 201, recorrido: str = "R1", **extra: Any
) -> dict[str, Any]:
    return {
        "hu29": 1,
        "recorrido": recorrido,
        "paso": paso,
        "clave": clave,
        "status": status,
        "confirmada": 200 <= status < 300,
        **extra,
    }


def fila(tipo: str, clave: str, **extra: Any) -> dict[str, Any]:
    base = {"cotizacion": "cotizacion", "aceptacion": "polizas", "recaudo": "dinero"}[tipo]
    return {"base": base, "tipo": tipo, "clave": clave, **extra}


def sin_pendientes() -> list[dict[str, Any]]:
    return [
        {"base": b, "tipo": "outbox_pendiente", "total": 0}
        for b in ("cotizacion", "polizas", "dinero")
    ]


def conciliar(
    intentos: list[Any],
    observadas: list[Any],
    dlq: list[Any] | None = None,
    explicaciones: dict[str, str] | None = None,
) -> dict[str, Any]:
    return reconciliar.conciliar(
        intentos, observadas, dlq or [], explicaciones or {}, "EXP1", *VENTANA, ahora=AHORA
    )


def verificacion(informe: dict[str, Any], id_: str) -> dict[str, Any]:
    return next(v for v in informe["verificaciones"] if v["id"] == id_)


def recorrido_r3_completo(clave: str, aceptacion_id: str) -> tuple[list[Any], list[Any]]:
    intentos = [
        intento("cotizacion", f"cot-{clave}", recorrido="R3"),
        intento("aceptacion", clave, recorrido="R3", id=aceptacion_id),
    ]
    filas = [
        fila("cotizacion", f"cot-{clave}"),
        fila("aceptacion", clave, id=aceptacion_id),
        fila("recaudo", aceptacion_id, estado="CONFIRMADO"),
    ]
    return intentos, filas


def test_sin_diferencias_cuando_cada_confirmada_existe_una_vez() -> None:
    r3_intentos, r3_filas = recorrido_r3_completo("k3", "ac-1")
    informe = conciliar(
        [intento("cotizacion", "k1"), *r3_intentos],
        [fila("cotizacion", "k1"), *r3_filas, *sin_pendientes()],
    )

    assert informe["estado"] == "SIN_DIFERENCIAS"
    assert {v["resultado"] for v in informe["verificaciones"]} == {"COINCIDE"}
    assert (
        informe["resumen"]["confirmadas"],
        informe["resumen"]["perdidas"],
        informe["resumen"]["duplicadas"],
    ) == (3, 0, 0)
    assert (informe["version"], informe["experimento"], informe["ventana"]) == (
        1,
        "EXP1",
        {"desde": VENTANA[0], "hasta": VENTANA[1]},
    )
    assert informe["generado"] == "2026-11-20T14:40:00Z"


def test_una_operacion_confirmada_que_falta_en_la_base_es_una_perdida() -> None:
    informe = conciliar(
        [intento("cotizacion", "k1"), intento("cotizacion", "k2")],
        [fila("cotizacion", "k1"), *sin_pendientes()],
    )

    cotizaciones = verificacion(informe, "cotizaciones")
    assert (cotizaciones["esperado"], cotizaciones["observado"], cotizaciones["resultado"]) == (
        2,
        1,
        "DIFERENCIA",
    )
    assert cotizaciones["detalle"] == [{"clave": "k2", "problema": "FALTANTE"}]
    assert (informe["estado"], informe["resumen"]["perdidas"]) == ("CON_DIFERENCIAS", 1)


def test_dos_filas_con_la_misma_clave_son_una_duplicada() -> None:
    informe = conciliar(
        [intento("aceptacion", "k1", recorrido="R3", id="ac-1")],
        [
            fila("aceptacion", "k1", id="ac-1"),
            fila("aceptacion", "k1", id="ac-2"),
            fila("recaudo", "ac-1"),
            *sin_pendientes(),
        ],
    )

    aceptaciones = verificacion(informe, "aceptaciones")
    assert aceptaciones["detalle"] == [{"clave": "k1", "problema": "DUPLICADA", "filas": 2}]
    assert (informe["estado"], informe["resumen"]["duplicadas"]) == ("CON_DIFERENCIAS", 1)


def test_reintentar_con_la_misma_clave_cuenta_una_sola_operacion() -> None:
    # 503 durante la falla y 201 en el reintento con la misma Idempotency-Key.
    informe = conciliar(
        [
            intento("cotizacion", "k1", status=503, intento=1),
            intento("cotizacion", "k1", intento=2),
        ],
        [fila("cotizacion", "k1"), *sin_pendientes()],
    )

    assert informe["estado"] == "SIN_DIFERENCIAS"
    assert verificacion(informe, "cotizaciones")["esperado"] == 1
    assert informe["recorridos"]["R1"] == {
        "intentos": 2,
        "confirmadas": 1,
        "errores": 1,
        "erroresPorEstado": {"503": 1},
    }
    assert (
        informe["resumen"]["solicitudes"],
        informe["resumen"]["solicitudesConError"],
        informe["resumen"]["porcentajeError"],
    ) == (2, 1, 50.0)


def test_una_solicitud_que_nunca_se_confirmo_no_se_exige_en_la_base() -> None:
    informe = conciliar([intento("cotizacion", "k1", status=0)], sin_pendientes())

    assert informe["estado"] == "SIN_DIFERENCIAS"
    assert verificacion(informe, "cotizaciones")["esperado"] == 0


def test_dos_cobros_para_la_misma_aceptacion_son_un_cobro_duplicado() -> None:
    intentos, filas = recorrido_r3_completo("k3", "ac-1")
    informe = conciliar(intentos, [*filas, fila("recaudo", "ac-1"), *sin_pendientes()])

    assert verificacion(informe, "cobros_duplicados")["detalle"] == [
        {"claveRecaudo": "ac-1", "cobros": 2}
    ]
    assert (informe["estado"], informe["resumen"]["cobrosDuplicados"]) == ("CON_DIFERENCIAS", 1)


def test_aceptacion_sin_cobro_es_pendiente_si_su_evento_sigue_en_el_outbox() -> None:
    intentos, filas = recorrido_r3_completo("k3", "ac-1")
    sin_recaudo = [f for f in filas if f["tipo"] != "recaudo"]
    en_transito = [{"base": "polizas", "tipo": "outbox_pendiente", "total": 1}]

    assert conciliar(intentos, sin_recaudo + en_transito)["estado"] == "PENDIENTE"
    entregado = conciliar(intentos, sin_recaudo + sin_pendientes())
    assert verificacion(entregado, "cobro_por_aceptacion")["detalle"] == [
        {"aceptacionId": "ac-1", "problema": "SIN_COBRO"}
    ]
    assert entregado["estado"] == "CON_DIFERENCIAS"


def test_un_mensaje_en_dlq_sin_explicacion_es_una_diferencia() -> None:
    dlq = [
        {"cola": "solventa-exp-consulta-proyeccion-dlq", "messageId": "m1"},
        {"cola": "solventa-exp-avisos-dlq", "messageId": "m2"},
    ]

    informe = conciliar([], sin_pendientes(), dlq)
    assert verificacion(informe, "dlq_sin_explicacion")["observado"] == 2
    assert informe["estado"] == "CON_DIFERENCIAS"

    explicado = conciliar(
        [],
        sin_pendientes(),
        dlq,
        {"m1": "proyección reconstruible", "m2": "aviso de texto reintentado"},
    )
    assert explicado["estado"] == "SIN_DIFERENCIAS"
    assert explicado["dlq"]["explicados"][0] == {
        "cola": "solventa-exp-consulta-proyeccion-dlq",
        "messageId": "m1",
        "motivo": "proyección reconstruible",
    }


def test_sin_carga_de_r5_lo_dice_el_informe() -> None:
    assert conciliar([], sin_pendientes())["recorridos"]["R5"] == {"estado": "SIN_CARGA"}


# --- R5: evento firmado → evaluación → siniestro paramétrico → pago (HU-W21 a HU-W23) ----------
def r5(id_externo: str, evento_id: str, status: int = 202) -> dict[str, Any]:
    return intento("evento", id_externo, status=status, recorrido="R5", id=evento_id)


def filas_r5(id_externo: str, evento_id: str, siniestros: int = 1, pagos: int = 1) -> list[Any]:
    """El recorrido completo de un evento que activa una póliza (COB-MED-1)."""
    filas: list[Any] = [
        {
            "base": "adaptador_telemetria",
            "tipo": "evento_recibido",
            "id": evento_id,
            "clave": id_externo,
        },
        {
            "base": "pagos_parametricos",
            "tipo": "evento_evaluado",
            "id": evento_id,
            "clave": id_externo,
            "decision": "ACTIVA",
        },
        {
            "base": "pagos_parametricos",
            "tipo": "activacion",
            "id": evento_id,
            "polizaId": "p-med",
            "estado": "SINIESTRO_CREADO",
        },
    ]
    for n in range(siniestros):
        filas.append(
            {
                "base": "siniestros",
                "tipo": "siniestro",
                "id": f"s-{evento_id}-{n}",
                "eventoId": evento_id,
                "polizaId": "p-med",
            }
        )
    filas += [
        {
            "base": "dinero",
            "tipo": "indemnizacion",
            "id": f"i-{n}",
            "siniestroId": f"s-{evento_id}-0",
            "estado": "CONFIRMADA",
        }
        for n in range(pagos)
    ]
    return filas


def sin_pendientes_r5() -> list[dict[str, Any]]:
    return [
        *sin_pendientes(),
        *(
            {"base": b, "tipo": "outbox_pendiente", "total": 0}
            for b in ("adaptador_telemetria", "pagos_parametricos", "siniestros")
        ),
    ]


def test_r5_sin_diferencias_un_evento_un_siniestro_un_pago() -> None:
    # El reintento con el mismo idExterno (503 y luego 200) es la misma operación.
    intentos = [
        r5("HU29-a", "e-a", status=503),
        r5("HU29-a", "e-a", status=200),
        r5("HU29-b", "e-b"),
    ]
    informe = conciliar(
        intentos, [*filas_r5("HU29-a", "e-a"), *filas_r5("HU29-b", "e-b"), *sin_pendientes_r5()]
    )

    assert informe["estado"] == "SIN_DIFERENCIAS"
    for id_ in ("eventos_recibidos", "eventos_evaluados", "siniestro_por_activacion"):
        assert (
            verificacion(informe, id_)["esperado"],
            verificacion(informe, id_)["observado"],
        ) == (2, 2), id_
    assert informe["resumen"]["confirmadas"] == 2
    assert informe["recorridos"]["R5"] == {
        "intentos": 3,
        "confirmadas": 2,
        "errores": 1,
        "erroresPorEstado": {"503": 1},
    }


def test_r5_evento_confirmado_que_no_se_recibio_es_una_perdida() -> None:
    informe = conciliar([r5("HU29-a", "e-a")], sin_pendientes_r5())

    assert verificacion(informe, "eventos_recibidos")["detalle"] == [
        {"clave": "HU29-a", "problema": "FALTANTE"}
    ]
    assert (informe["estado"], informe["resumen"]["perdidas"]) == ("CON_DIFERENCIAS", 1)


def test_r5_evaluacion_pendiente_si_el_evento_sigue_en_el_outbox_o_en_la_dlq() -> None:
    recibido = filas_r5("HU29-a", "e-a")[:1]
    en_outbox = [
        *sin_pendientes(),
        {"base": "adaptador_telemetria", "tipo": "outbox_pendiente", "total": 1},
    ]
    assert (
        verificacion(conciliar([r5("HU29-a", "e-a")], recibido + en_outbox), "eventos_evaluados")[
            "resultado"
        ]
        == "PENDIENTE"
    )

    en_dlq = [{"cola": "solventa-exp-parametricos-eventos-dlq", "messageId": "m1"}]
    con_dlq = conciliar(
        [r5("HU29-a", "e-a")], recibido + sin_pendientes_r5(), en_dlq, {"m1": "reproceso desde H11"}
    )
    assert verificacion(con_dlq, "eventos_evaluados")["resultado"] == "PENDIENTE"

    perdido = conciliar([r5("HU29-a", "e-a")], recibido + sin_pendientes_r5())
    assert (verificacion(perdido, "eventos_evaluados")["resultado"], perdido["estado"]) == (
        "DIFERENCIA",
        "CON_DIFERENCIAS",
    )


def test_r5_activacion_sin_siniestro_pendiente_o_diferencia() -> None:
    filas = filas_r5("HU29-a", "e-a", siniestros=0, pagos=0)
    filas[2] = {**filas[2], "estado": "PENDIENTE"}
    pendiente = conciliar([r5("HU29-a", "e-a")], [*filas, *sin_pendientes_r5()])
    assert verificacion(pendiente, "siniestro_por_activacion")["resultado"] == "PENDIENTE"

    filas[2] = {**filas[2], "estado": "SINIESTRO_CREADO"}
    perdido = conciliar([r5("HU29-a", "e-a")], [*filas, *sin_pendientes_r5()])
    siniestro = verificacion(perdido, "siniestro_por_activacion")
    assert (siniestro["resultado"], siniestro["detalle"]) == (
        "DIFERENCIA",
        [{"eventoId": "e-a", "polizaId": "p-med", "problema": "SIN_SINIESTRO"}],
    )


def test_r5_siniestro_y_pago_duplicados_cuentan_como_duplicadas_y_cobros_duplicados() -> None:
    informe = conciliar(
        [r5("HU29-a", "e-a")],
        [*filas_r5("HU29-a", "e-a", siniestros=2, pagos=2), *sin_pendientes_r5()],
    )

    assert verificacion(informe, "siniestros_duplicados")["detalle"] == [
        {"eventoId": "e-a", "polizaId": "p-med", "problema": "DUPLICADA", "siniestros": 2}
    ]
    assert verificacion(informe, "pagos_duplicados")["detalle"] == [
        {"siniestroId": "s-e-a-0", "pagos": 2}
    ]
    assert (
        informe["resumen"]["duplicadas"],
        informe["resumen"]["cobrosDuplicados"],
        informe["estado"],
    ) == (1, 1, "CON_DIFERENCIAS")


def test_r5_ignora_siniestros_y_activaciones_de_otros_eventos() -> None:
    ajenas = [
        f for f in filas_r5("OTRO", "e-x", siniestros=2, pagos=2) if f["tipo"] != "evento_recibido"
    ]
    informe = conciliar(
        [r5("HU29-a", "e-a")], [*filas_r5("HU29-a", "e-a"), *ajenas, *sin_pendientes_r5()]
    )
    assert informe["estado"] == "SIN_DIFERENCIAS"


def test_lee_la_consola_de_k6_en_logfmt_y_en_json() -> None:
    linea = json.dumps(
        {
            "hu29": 1,
            "recorrido": "R1",
            "paso": "cotizacion",
            "clave": "k1",
            "confirmada": True,
            "nota": "zona sana",
        }
    )
    logfmt = (
        'time="2026-11-20T14:01:00Z" level=info msg="'
        + linea.replace('"', '\\"')
        + '" source=console'
    )

    intentos = reconciliar.leer_intentos(
        [logfmt, linea, "otra línea de k6", 'msg="{\\"hu29\\": 2}"', "{roto hu29"]
    )
    assert [i["clave"] for i in intentos] == ["k1", "k1"]
    assert intentos[0]["nota"] == "zona sana"


def test_main_escribe_el_informe_y_sale_con_1_si_hay_diferencias(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "carga.log").write_text(
        json.dumps(intento("cotizacion", "k1")) + "\n", encoding="utf-8"
    )
    (tmp_path / "observadas.ndjson").write_text(
        "\n".join(json.dumps(f) for f in sin_pendientes()) + "\n", encoding="utf-8"
    )

    salida = reconciliar.main(
        [
            "--experimento",
            "EXP1",
            "--desde",
            VENTANA[0],
            "--hasta",
            VENTANA[1],
            "--resultados",
            str(tmp_path),
        ]
    )

    informe = json.loads((tmp_path / "informe.json").read_text(encoding="utf-8"))
    assert (salida, informe["estado"], informe["resumen"]["perdidas"]) == (1, "CON_DIFERENCIAS", 1)
    assert "CON_DIFERENCIAS: 1 confirmadas, 1 perdidas" in capsys.readouterr().out

    (tmp_path / "observadas.ndjson").write_text(
        json.dumps(fila("cotizacion", "k1")) + "\n", encoding="utf-8"
    )
    (tmp_path / "explicaciones.json").write_text("{}", encoding="utf-8")
    assert (
        reconciliar.main(
            [
                "--experimento",
                "EXP1",
                "--desde",
                VENTANA[0],
                "--hasta",
                VENTANA[1],
                "--resultados",
                str(tmp_path),
            ]
        )
        == 0
    )


def _minuto(hh_mm: str, ok: int, error: int) -> list[dict[str, Any]]:
    marca = f"2026-11-20T{hh_mm}:10Z"
    return [intento("cotizacion", f"{hh_mm}-{n}", instante=marca) for n in range(ok)] + [
        intento("cotizacion", f"{hh_mm}-e{n}", status=503, instante=marca) for n in range(error)
    ]


def test_la_redireccion_termina_cuando_la_carga_vuelve_a_bajo_el_umbral_de_error() -> None:
    intentos = [
        *_minuto("14:04", 60, 0),
        *_minuto("14:05", 40, 20),
        *_minuto("14:06", 55, 5),
        *_minuto("14:07", 60, 0),
        *_minuto("14:08", 60, 0),
    ]

    resultado = reconciliar.continuidad(intentos, "2026-11-20T14:05:00Z")

    assert (
        resultado["redireccionSegundos"],
        resultado["cumpleObjetivo"],
        resultado["objetivoSegundos"],
    ) == (120, True, 600)
    assert resultado["serie"][1] == {
        "minuto": "2026-11-20T14:05Z",
        "solicitudes": 60,
        "errores": 20,
        "porcentajeError": 33.33,
    }


def test_sin_estabilizarse_no_cumple_el_objetivo() -> None:
    intentos = [*_minuto("14:05", 50, 10), *_minuto("14:16", 50, 10)]

    resultado = reconciliar.continuidad(intentos, "2026-11-20T14:05:00Z")
    assert (resultado["redireccionSegundos"], resultado["cumpleObjetivo"]) == (None, False)
    tarde = reconciliar.continuidad(
        [*_minuto("14:05", 50, 10), *_minuto("14:16", 60, 0)], "2026-11-20T14:05:00Z"
    )
    assert (tarde["redireccionSegundos"], tarde["cumpleObjetivo"]) == (660, False)


def test_el_informe_incluye_la_continuidad_solo_con_la_inyeccion() -> None:
    assert conciliar([], sin_pendientes())["continuidad"] is None
    informe = reconciliar.conciliar(
        _minuto("14:05", 10, 0),
        sin_pendientes(),
        [],
        {},
        "EXP1",
        *VENTANA,
        ahora=AHORA,
        inyeccion="2026-11-20T14:05:00Z",
    )
    assert informe["continuidad"]["redireccionSegundos"] == 0
