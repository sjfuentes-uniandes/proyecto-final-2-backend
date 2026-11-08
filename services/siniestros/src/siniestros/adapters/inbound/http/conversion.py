"""De las vistas de la aplicación a los DTO del contrato (HU-W19). Ninguna incluye la clave del
objeto ni una URL: `EvidenciaVista` solo lleva metadatos e integridad."""

from siniestros.adapters.inbound.http.dto import (
    AmparoCopiaDto,
    BandejaSiniestrosRespuesta,
    BeneficiarioDto,
    CoherenciaDto,
    CopiaPolizaDto,
    DetalleSiniestro,
    EnlaceEvidencia,
    EntradaHistorial,
    EvidenciaVista,
    PasoTramiteDto,
    ResumenBandejaSiniestros,
    ResumenSiniestro,
    SoportePendienteDto,
    UbicacionSiniestroDto,
    VerificacionCoberturaDto,
    VigenciaCopiaDto,
)
from siniestros.application.siniestro.vistas import (
    DetalleCaso,
    EnlaceEmitido,
    Indicadores,
    PaginaBandeja,
    ResumenCaso,
)
from siniestros.domain.siniestro.modelos import CopiaPoliza, Ubicacion


def ubicacion_dto(ubicacion: Ubicacion) -> UbicacionSiniestroDto:
    return UbicacionSiniestroDto(
        descripcion=ubicacion.descripcion, lat=ubicacion.lat, lon=ubicacion.lon
    )


def copia_dto(copia: CopiaPoliza) -> CopiaPolizaDto:
    # La copia nace completa al reportar (M01); una incompleta es un dato dañado, no una respuesta.
    assert copia.vigencia is not None
    assert copia.beneficiario is not None
    assert copia.carencia_dias is not None
    assert copia.plazo_aviso_dias is not None
    assert copia.primas_al_dia is not None
    return CopiaPolizaDto(
        numero=copia.numero,
        ramo=copia.ramo,
        vigencia=VigenciaCopiaDto(
            desde=copia.vigencia.desde.isoformat(), hasta=copia.vigencia.hasta.isoformat()
        ),
        amparos=[
            AmparoCopiaDto(codigo=a.codigo, nombre=a.nombre, suma_asegurada=a.suma_asegurada)
            for a in copia.amparos
        ],
        moneda=copia.moneda,
        carencia_dias=copia.carencia_dias,
        plazo_aviso_dias=copia.plazo_aviso_dias,
        primas_al_dia=copia.primas_al_dia,
        beneficiario=BeneficiarioDto(
            beneficiario_ref=copia.beneficiario.beneficiario_ref,
            cuenta_mascarada=copia.beneficiario.cuenta_mascarada,
        ),
    )


def resumen_dto(resumen: ResumenCaso) -> ResumenSiniestro:
    s = resumen.siniestro
    return ResumenSiniestro(
        id=s.id,
        codigo=s.codigo,
        poliza_id=s.poliza_id,
        poliza_numero=s.copia_poliza.numero,
        cliente_id=s.cliente_id,
        ramo=s.copia_poliza.ramo,
        origen=s.origen,
        estado=s.estado,
        prioridad=resumen.prioridad,
        reportado_en=s.reportado_en,
        analista_id=s.analista_id,
        tiempo_restante_min=resumen.tiempo_restante_min,
        reloj_pausado=s.reloj_pausado,
    )


def bandeja_dto(pagina: PaginaBandeja) -> BandejaSiniestrosRespuesta:
    return BandejaSiniestrosRespuesta(
        items=[resumen_dto(r) for r in pagina.items],
        siguiente_cursor=pagina.siguiente_cursor,
        total=pagina.total,
    )


def indicadores_dto(i: Indicadores) -> ResumenBandejaSiniestros:
    return ResumenBandejaSiniestros(
        pendientes=i.pendientes,
        vencen_hoy=i.vencen_hoy,
        fuera_de_compromiso=i.fuera_de_compromiso,
        resueltos_semana=i.resueltos_semana,
        resueltos_semana_previa=i.resueltos_semana_previa,
        tiempo_medio_decision_horas=i.tiempo_medio_decision_horas,
        compromiso_horas=i.compromiso_horas,
        asignados_al_actor=i.asignados_al_actor,
        sin_asignar=i.sin_asignar,
    )


def detalle_dto(detalle: DetalleCaso) -> DetalleSiniestro:
    s = detalle.resumen.siniestro
    base = resumen_dto(detalle.resumen).model_dump()
    c = detalle.coherencia
    return DetalleSiniestro(
        **base,
        causa=s.causa,
        fecha_evento=s.fecha_evento,
        ubicacion_declarada=ubicacion_dto(s.ubicacion_declarada),
        poliza=copia_dto(s.copia_poliza),
        verificacion_cobertura=[
            VerificacionCoberturaDto(
                codigo=v.codigo, resultado=v.resultado, detalle=dict(v.detalle)
            )
            for v in detalle.verificacion_cobertura
        ],
        coherencia=CoherenciaDto(
            fecha_declarada=c.fecha_declarada,
            primera_captura=c.primera_captura,
            diferencia_min=c.diferencia_min,
            ubicacion_coincide=c.ubicacion_coincide,
            archivos_alterados=c.archivos_alterados,
            resultado=c.resultado,
        ),
        evidencias=[
            EvidenciaVista(
                id=v.evidencia.id,
                nombre_archivo=v.evidencia.nombre_archivo,
                tipo=v.evidencia.tipo,
                capturada_en=v.evidencia.capturada_en,
                ubicacion=ubicacion_dto(v.evidencia.ubicacion) if v.evidencia.ubicacion else None,
                integridad=v.integridad,
            )
            for v in detalle.evidencias
        ],
        soportes_pendientes=[
            SoportePendienteDto(tipo=p.tipo, estado=p.estado) for p in detalle.soportes_pendientes
        ],
        tramite=[
            PasoTramiteDto(
                paso=p.paso,
                estado=p.estado,
                ocurrido_en=p.ocurrido_en,
                actor_nombre=p.actor_nombre,
            )
            for p in detalle.tramite
        ],
        historial=[
            EntradaHistorial(
                id=h.id,
                accion=h.accion,
                estado_anterior=h.estado_anterior,
                estado_nuevo=h.estado_nuevo,
                actor_id=h.actor_id,
                actor_rol=h.actor_rol,
                actor_nombre=h.actor_nombre,
                justificacion=h.justificacion,
                ocurrido_en=h.ocurrido_en,
            )
            for h in detalle.historial
        ],
        liquidacion=None,
        evento=None,
        pago=None,
    )


def enlace_dto(enlace: EnlaceEmitido) -> EnlaceEvidencia:
    return EnlaceEvidencia(
        url=enlace.url, expira_en=enlace.expira_en, tipo_contenido=enlace.tipo_contenido
    )
