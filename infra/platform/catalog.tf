# Catálogo único de despliegables. platform crea lo que no depende de imágenes
# (ECR, bases, colas, destinos del ALB) y apps lo consume desde el output
# `platform` para crear las tareas. Agregar un servicio aquí y aplicar platform
# antes de publicar su imagen.
#
# tier:      acceso | nucleo | adaptador | proyeccion | worker | soporte (modelo de
#            despliegue). Los workers escalan por cola; el resto, por CPU.
# listener_port: puerto del listener del ALB interno (servicios de acceso y la entrada
#            de telemetría, HU-S03)
# database:  base PostgreSQL privada y usuario propio en la instancia RDS
# cache:     cliente de la caché ElastiCache (Cache-Aside); sin caché usa RDS
# calls:     dependencias síncronas por ECS Service Connect (REST interno)
# publishes: publica eventos del Outbox en el tópico de negocio (SNS)
# consumes:  colas SQS que procesa (claves de local.queues)
# allies:    credenciales de aliados externos que usa (claves de local.allies)
locals {
  catalog = {
    "bff-web" = {
      tier = "acceso", listener_port = 8081, database = false, publishes = false, cache = false
      # HU-S03: siniestros por ServiciosWeb --> AtencionSiniestros (modelo_componentes.puml).
      calls    = ["clientes", "cotizacion", "api-socios", "polizas", "consulta", "siniestros"]
      consumes = [], allies = []
      stories  = ["HU-W01", "HU-W02", "HU-W10", "HU-W11", "HU-W12", "HU-W13", "HU-W14", "HU-W15", "HU-W18", "HU-W19", "HU-W20", "HU-W30", "HU-W31"]
    }
    "bff-movil" = {
      tier = "acceso", listener_port = 8082, database = false, publishes = false, cache = false
      # HU-S03: siniestros por ServiciosMovil --> AtencionSiniestros (modelo_componentes.puml).
      calls    = ["clientes", "consulta", "avisos", "siniestros"]
      consumes = [], allies = []
      stories  = ["HU-M01", "HU-M02", "HU-M03", "HU-M04", "HU-M05", "HU-M06", "HU-M07", "HU-M08", "HU-M09", "HU-M10", "HU-M11"]
    }
    "api-socios" = {
      tier     = "acceso", listener_port = 8083, database = true, publishes = true, cache = false
      calls    = ["cotizacion"]
      consumes = [], allies = []
      stories  = ["HU-W01", "HU-W02", "HU-W03", "HU-W04"]
    }
    "clientes" = {
      tier     = "nucleo", listener_port = null, database = true, publishes = true, cache = false
      calls    = ["adaptador-identidad"]
      consumes = [], allies = []
      stories  = ["HU-W30", "HU-W31", "HU-M06", "HU-M08", "HU-M09"]
    }
    "catalogo" = {
      tier     = "nucleo", listener_port = null, database = true, publishes = false, cache = true
      calls    = []
      consumes = [], allies = []
      stories  = ["HU-W03", "HU-W11"]
    }
    "cotizacion" = {
      tier     = "nucleo", listener_port = null, database = true, publishes = true, cache = true
      calls    = ["catalogo", "clientes", "adaptador-datos"]
      consumes = ["consentimientos-cotizacion"], allies = []
      stories  = ["HU-W03", "HU-W04", "HU-W05", "HU-W06", "HU-W10", "HU-W11", "HU-W12", "HU-W31"]
    }
    # Suscripción, aceptación y emisión; inicia la Saga de recaudo, firma y emisión.
    "polizas" = {
      tier     = "nucleo", listener_port = null, database = true, publishes = true, cache = false
      calls    = ["adaptador-firma", "cotizacion", "catalogo"]
      consumes = ["polizas-recaudos"], allies = []
      stories  = ["HU-W13", "HU-W14", "HU-W15", "HU-W17"]
    }
    # Recaudos e indemnizaciones: movimientos idempotentes. HU-S03: el siniestro aprobado
    # inicia la indemnización (modelo_componentes.puml (6); modelo_concurrencia.puml (6)).
    "dinero" = {
      tier     = "nucleo", listener_port = null, database = true, publishes = true, cache = false
      calls    = ["adaptador-pagos"]
      consumes = ["dinero-aceptaciones", "dinero-indemnizaciones"], allies = []
      stories  = ["HU-W16", "HU-W23"]
    }
    "adaptador-datos" = {
      tier     = "adaptador", listener_port = null, database = false, publishes = false, cache = false
      calls    = ["simulador-aliados"]
      consumes = [], allies = ["open-finance", "datos-abiertos"]
      stories  = ["HU-W05", "HU-W06", "HU-W10", "HU-W12"]
    }
    "adaptador-identidad" = {
      tier     = "adaptador", listener_port = null, database = false, publishes = false, cache = false
      calls    = ["simulador-aliados"]
      consumes = [], allies = ["kyc"]
      stories  = ["HU-W30", "HU-M06"]
    }
    "adaptador-pagos" = {
      tier     = "adaptador", listener_port = null, database = false, publishes = false, cache = false
      calls    = ["simulador-aliados"]
      consumes = [], allies = ["pagos"]
      stories  = ["HU-W16"]
    }
    # Firma electrónica y notificaciones (modelo_despliegue.puml: ConexionFirma).
    "adaptador-firma" = {
      tier     = "adaptador", listener_port = null, database = false, publishes = false, cache = false
      calls    = ["simulador-aliados"]
      consumes = [], allies = ["firma", "push"]
      stories  = ["HU-W17", "HU-M11"]
    }
    # Proyección CQRS de pólizas (y siniestros más adelante). Responde lecturas
    # síncronas con meta de latencia, por eso escala por CPU y no por su cola.
    "consulta" = {
      tier     = "proyeccion", listener_port = null, database = true, publishes = false, cache = true
      calls    = ["catalogo"]
      consumes = ["consulta-proyeccion"], allies = []
      stories  = ["HU-W18", "HU-M10"]
    }
    # Inbox y tokens de dispositivo; envía los avisos push por adaptador-firma.
    "avisos" = {
      tier     = "worker", listener_port = null, database = true, publishes = false, cache = false
      calls    = ["adaptador-firma"]
      consumes = ["avisos"], allies = []
      stories  = ["HU-M11"]
    }
    "auditoria" = {
      tier     = "worker", listener_port = null, database = false, publishes = false, cache = false
      calls    = []
      consumes = ["auditoria"], allies = []
      stories  = ["HU-W31", "HU-M08", "HU-M09", "HU-W27"]
    }
    # --- Sprint 3 (HU-S03) ---------------------------------------------------------
    # Registra, evalúa y decide los casos; único dueño de las evidencias
    # (AtencionSiniestros --> Evidencias). Recibe el resultado de la indemnización por
    # su cola (modelo_concurrencia.puml (8)) y no llama a nadie: la indemnización la
    # inicia SiniestroAprobado (modelo_componentes.puml (6)), no la flecha REST a Dinero.
    "siniestros" = {
      tier     = "nucleo", listener_port = null, database = true, publishes = true, cache = false
      calls    = []
      consumes = ["siniestros-indemnizaciones"], allies = []
      stories  = ["HU-W19", "HU-W20", "HU-W22", "HU-W23", "HU-M01", "HU-M02", "HU-M03", "HU-M04", "HU-M05"]
    }
    # Consumidor idempotente de eventos paramétricos (Inbox, coberturas activas y
    # evaluaciones). Un evento válido crea el siniestro una sola vez:
    # PagosParametricos --> AtencionSiniestros y modelo_concurrencia.puml (9).
    "pagos-parametricos" = {
      tier     = "worker", listener_port = null, database = true, publishes = true, cache = false
      calls    = ["siniestros"]
      consumes = ["parametricos-eventos", "parametricos-polizas"], allies = []
      stories  = ["HU-W21", "HU-W22", "HU-W24", "HU-W26"]
    }
    # Entrada de eventos de telemetría (POST /telemetria/v1/eventos, edge.tf). Valida la
    # firma HMAC (D5: ConexionTelemetria --> OtrosProveedores : mTLS / evento autenticado)
    # y escribe el evento válido en su Outbox (ConexionTelemetria --> PublicadoresOutbox,
    # ControlDependencias --> TransaccionOutbox : eventos externos válidos). Tiene
    # listener en el ALB porque API Gateway lo invoca, pero no es de acceso: no recibe JWT.
    "adaptador-telemetria" = {
      tier     = "adaptador", listener_port = 8084, database = true, publishes = true, cache = false
      calls    = []
      consumes = [], allies = ["telemetria"]
      stories  = ["HU-W21", "HU-W26"]
    }
    "simulador-aliados" = {
      tier     = "soporte", listener_port = null, database = false, publishes = false, cache = false
      calls    = []
      consumes = [], allies = []
      stories  = ["HU-W10", "HU-W12", "HU-W16", "HU-W17", "HU-W30", "HU-M06", "HU-M11"]
    }
  }

  # Un servicio anuncia su nombre en Service Connect solo si otro lo invoca.
  discoverable = toset(flatten([for service in values(local.catalog) : service.calls]))

  database_services = toset([for name, service in local.catalog : name if service.database])
  cache_services    = toset([for name, service in local.catalog : name if service.cache])
  access_services   = { for name, service in local.catalog : name => service if service.listener_port != null }

  # Colas por consumidor suscritas al tópico de eventos de negocio. El filtro se
  # aplica sobre el atributo de mensaje `eventType` que publica cada Outbox.
  # Ninguna cola se comparte: cada consumidor tiene su ritmo, su DLQ y sus alarmas.
  # message_retention_seconds es opcional: sin él, la cola retiene 4 días (messaging.tf).
  queues = {
    auditoria = {
      filter             = null
      visibility_timeout = 60
      max_receive_count  = 5
    }
    consentimientos-cotizacion = {
      filter             = { eventType = [{ prefix = "Consentimiento" }] }
      visibility_timeout = 30
      max_receive_count  = 5
    }
    # Saga de contratación (HU-W16): la oferta aceptada inicia el recaudo de la prima.
    dinero-aceptaciones = {
      filter             = { eventType = ["OfertaAceptada"] }
      visibility_timeout = 60
      max_receive_count  = 5
    }
    # HU-W17: el recaudo confirma la emisión (firma y documento) o la compensa si se rechaza.
    polizas-recaudos = {
      filter             = { eventType = [{ prefix = "Recaudo" }] }
      visibility_timeout = 120
      max_receive_count  = 5
    }
    # HU-W18/HU-M10: la proyección se alimenta de los cambios de las pólizas y de los siniestros.
    # HU-S03 (HU-M05): también del resultado de la indemnización (EventosNegocio --> ProyeccionesConsulta).
    consulta-proyeccion = {
      filter             = { eventType = [{ prefix = "Poliza" }, { prefix = "Siniestro" }, { prefix = "Indemnizacion" }] }
      visibility_timeout = 30
      max_receive_count  = 5
    }
    # HU-M11: aviso push de la emisión y del pago confirmado.
    avisos = {
      filter             = { eventType = ["PolizaEmitida", "RecaudoConfirmado"] }
      visibility_timeout = 60
      max_receive_count  = 5
    }
    # --- Sprint 3 (HU-S03). D2: cola por grupo de consumidores, sin FIFO; el
    # resultado no depende del orden (Inbox por eventId y clave única de negocio).
    # HU-W21: evento externo validado por adaptador-telemetria (EventosNegocio --> PagosParametricos,
    # ParticionesEventos --> ConsumidoresParametricos). HU-W24: retiene 4 días para reprocesar.
    parametricos-eventos = {
      filter                    = { eventType = ["EventoParametricoObservado"] }
      visibility_timeout        = 30
      max_receive_count         = 5
      message_retention_seconds = 345600
    }
    # HU-W21: copia local de las coberturas paramétricas activas, sin llamadas síncronas a polizas.
    parametricos-polizas = {
      filter             = { eventType = [{ prefix = "Poliza" }] }
      visibility_timeout = 30
      max_receive_count  = 5
    }
    # HU-W23: el siniestro aprobado inicia la indemnización (modelo_componentes.puml (6);
    # modelo_concurrencia.puml (5) y (6)).
    dinero-indemnizaciones = {
      filter             = { eventType = ["SiniestroAprobado"] }
      visibility_timeout = 60
      max_receive_count  = 5
    }
    # HU-W23: la saga actualiza el resultado del siniestro (modelo_concurrencia.puml (8)).
    siniestros-indemnizaciones = {
      filter             = { eventType = [{ prefix = "Indemnizacion" }] }
      visibility_timeout = 30
      max_receive_count  = 5
    }
  }

  # Aliados externos: el secreto guarda credenciales; la URL se configura en apps.
  # secret_keys (opcional) son las claves del marcador del secreto; por omisión,
  # client_id y client_secret.
  allies = {
    open-finance   = { description = "Proveedor de Finanzas Abiertas (ingresos y obligaciones)" }
    datos-abiertos = { description = "Fuente de Datos Abiertos para perfilamiento" }
    kyc            = { description = "Proveedor de identidad, KYC y prueba de vida" }
    pagos          = { description = "Pasarela de medios de pago (recaudo de primas)" }
    firma          = { description = "Proveedor de firma electrónica de pólizas" }
    push           = { description = "Proveedor de notificaciones push a dispositivos" }
    # HU-S03 (D5): secreto HMAC-SHA256 con el que el aliado firma cada evento.
    telemetria = { description = "Telemetría, clima y vuelos (eventos paramétricos firmados)", secret_keys = ["hmac_secret"] }
  }
}
