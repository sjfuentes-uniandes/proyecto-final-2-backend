"""HU-W24 (N1): registro encadenado (`RegistroAuditoria` con `previousHash`) y su verificación."""

import json

import pytest
from auditoria.domain.registro import (
    ACTOR_SISTEMA,
    GENESIS,
    CadenaRota,
    EventoAuditable,
    actor_de,
    sellar,
    verificar_cadena,
)

INSTANTE = "2026-11-05T14:06:12+00:00"


def _evento(n: int, ocurrido: str = "2026-11-05T14:06:12Z") -> EventoAuditable:
    return EventoAuditable(
        event_id=f"00000000-0000-4000-a000-{n:012d}",
        event_type="EventoParametricoObservado",
        aggregate_id=f"poliza-{n}",
        occurred_at=ocurrido,
        correlation_id=f"corr-{n}",
        actor=ACTOR_SISTEMA,
        cuerpo=json.dumps({"eventId": n, "payload": {"precipitacionMm": "142.7"}}),
    )


def _cadena(lotes: int, por_lote: int = 3) -> list[dict]:
    documentos, anterior, n = [], GENESIS, 0
    for secuencia in range(1, lotes + 1):
        eventos = [_evento(n := n + 1) for _ in range(por_lote)]
        lote = sellar(secuencia, anterior, eventos, INSTANTE)
        documentos.append(json.loads(lote.contenido()))
        anterior = lote.hash
    return documentos


def test_cada_registro_lleva_el_contrato_y_enlaza_con_el_anterior() -> None:
    lote = sellar(1, GENESIS, [_evento(1), _evento(2)], INSTANTE)

    primero, segundo = lote.registros
    assert set(primero) == {
        "actor",
        "accion",
        "resourceId",
        "timestamp",
        "previousHash",
        "hash",
        "eventId",
        "correlationId",
        "registradoEn",
        "evento",
    }
    assert (primero["accion"], primero["resourceId"]) == ("EventoParametricoObservado", "poliza-1")
    assert primero["previousHash"] == GENESIS
    assert segundo["previousHash"] == primero["hash"]
    assert lote.hash == segundo["hash"]
    # El evento se guarda tal como llegó.
    assert primero["evento"] == _evento(1).cuerpo


def test_el_orden_del_lote_es_determinista() -> None:
    tarde, temprano = _evento(1, "2026-11-05T14:09:03Z"), _evento(2, "2026-11-05T14:06:12Z")

    a = sellar(1, GENESIS, [tarde, temprano], INSTANTE)
    b = sellar(1, GENESIS, [temprano, tarde], INSTANTE)

    assert a.contenido() == b.contenido()
    assert [r["resourceId"] for r in a.registros] == ["poliza-2", "poliza-1"]


def test_un_lote_vacio_no_se_sella() -> None:
    with pytest.raises(ValueError):
        sellar(1, GENESIS, [], INSTANTE)


def test_una_cadena_integra_verifica_y_devuelve_su_cabeza() -> None:
    documentos = _cadena(4)

    assert verificar_cadena(documentos) == documentos[-1]["hash"]


@pytest.mark.parametrize(
    ("alterar", "motivo"),
    [
        (lambda d: d[2]["registros"][1].update(actor="otro"), "registro alterado"),
        (lambda d: d[2]["registros"][0].update(evento="{}"), "registro alterado"),
        (lambda d: d.pop(1), "secuencia fuera de orden"),
        (lambda d: d[2].update(secuencia=9), "secuencia fuera de orden"),
        (lambda d: d[3].update(hash="f" * 64), "hash del lote no coincide"),
        (lambda d: d[1]["registros"].pop(), "hash del lote no coincide"),
    ],
    ids=["actor", "evento", "lote-quitado", "secuencia", "hash-lote", "registro-quitado"],
)
def test_alterar_quitar_o_reordenar_rompe_la_cadena(alterar, motivo: str) -> None:  # type: ignore[no-untyped-def]
    documentos = _cadena(4)
    alterar(documentos)

    with pytest.raises(CadenaRota, match=motivo):
        verificar_cadena(documentos)


@pytest.mark.parametrize(
    ("payload", "actor"),
    [
        ({"actorId": "asesor-sintetico-01"}, "asesor-sintetico-01"),
        ({"actorId": ""}, ACTOR_SISTEMA),
        ({"actorId": "x" * 129}, ACTOR_SISTEMA),
        ({"actorId": "con\nsalto"}, ACTOR_SISTEMA),
        ({"actorId": 7}, ACTOR_SISTEMA),
        ({}, ACTOR_SISTEMA),
        (None, ACTOR_SISTEMA),
    ],
)
def test_el_actor_es_el_actor_id_del_payload_o_sistema(payload: object, actor: str) -> None:
    assert actor_de(payload) == actor
