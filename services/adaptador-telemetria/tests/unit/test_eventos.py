"""El evento `EventoParametricoObservado` que escribe el servicio (HU-W21, W21-B02)."""

from datetime import UTC, datetime
from uuid import UUID

from adaptador_telemetria.adapters.outbound.eventos import ESQUEMAS, sobre_de
from adaptador_telemetria.domain.telemetria.modelos import EventoCanonico


def _evento() -> EventoCanonico:
    return EventoCanonico(
        fuente="IDEAM-SIM",
        id_externo="IDEAM-2026-1110-0001",
        evento_origen="EVT-2026-0913",
        tipo_evento="LLUVIA_ACUMULADA",
        valor_observado="135",
        unidad="mm",
        zona="11001",
        observado_en=datetime(2026, 11, 10, 14, 0, tzinfo=UTC),
    )


def test_el_sobre_usa_el_evento_id_como_event_id_y_aggregate_id() -> None:
    recibido = datetime(2026, 11, 10, 14, 0, 5, tzinfo=UTC)
    clave = "telemetria/2026/11/10/bafc2b26-7c88-5ae3-b5ac-26fdf5427a09.json"
    sobre = sobre_de(_evento(), recibido, clave, "corr-1")
    esperado = UUID("bafc2b26-7c88-5ae3-b5ac-26fdf5427a09")
    assert (sobre.event_id, sobre.aggregate_id, sobre.payload.evento_id) == (esperado,) * 3
    assert (sobre.type, sobre.version, sobre.occurred_at) == (
        "EventoParametricoObservado",
        1,
        recibido,
    )
    assert sobre.payload.archivo_ref == clave
    assert sobre.correlation_id == "corr-1"


def test_el_payload_no_lleva_el_cuerpo_la_firma_ni_la_ubicacion() -> None:
    sobre = sobre_de(
        _evento(),
        datetime(2026, 11, 10, tzinfo=UTC),
        "telemetria/2026/11/10/"
        + "a" * 8
        + "-"
        + "a" * 4
        + "-"
        + "a" * 4
        + "-"
        + "a" * 4
        + "-"
        + "a" * 12
        + ".json",
        "c",
    )
    campos = set(sobre.payload.model_dump(by_alias=True))
    assert campos == {
        "eventoId",
        "fuente",
        "idExterno",
        "eventoOrigen",
        "tipoEvento",
        "valorObservado",
        "unidad",
        "zona",
        "observadoEn",
        "recibidoEn",
        "archivoRef",
    }


def test_el_esquema_se_publica_con_el_nombre_del_tipo() -> None:
    assert list(ESQUEMAS) == ["EventoParametricoObservado.v1.schema.json"]
