# Catálogo único de despliegables. platform crea lo que no depende de imágenes
# (ECR, bases, colas, destinos del ALB) y apps lo consume desde el output
# `platform` para crear las tareas. Agregar un servicio aquí y aplicar platform
# antes de publicar su imagen.
#
# tier:      acceso | nucleo | adaptador | proyeccion | worker | soporte (modelo de
#            despliegue). Los workers escalan por cola; el resto, por CPU.
# listener_port: puerto del listener del ALB interno (solo servicios de acceso)
# database:  base PostgreSQL privada y usuario propio en la instancia RDS
# cache:     cliente de la caché ElastiCache (Cache-Aside); sin caché usa RDS
# calls:     dependencias síncronas por ECS Service Connect (REST interno)
# publishes: publica eventos del Outbox en el tópico de negocio (SNS)
# consumes:  colas SQS que procesa (claves de local.queues)
# allies:    credenciales de aliados externos que usa (claves de local.allies)
locals {
  catalog = {
    "bff-web" = {
      tier     = "acceso", listener_port = 8081, database = false, publishes = false, cache = false
      calls    = ["clientes", "cotizacion", "api-socios", "polizas", "consulta"]
      consumes = [], allies = []
      stories  = ["HU-W01", "HU-W02", "HU-W10", "HU-W11", "HU-W12", "HU-W13", "HU-W14", "HU-W15", "HU-W18", "HU-W30", "HU-W31"]
    }
    "bff-movil" = {
      tier     = "acceso", listener_port = 8082, database = false, publishes = false, cache = false
      calls    = ["clientes", "consulta", "avisos"]
      consumes = [], allies = []
      stories  = ["HU-M06", "HU-M07", "HU-M08", "HU-M09", "HU-M10", "HU-M11"]
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
    # Recaudos e indemnizaciones: movimientos idempotentes.
    "dinero" = {
      tier     = "nucleo", listener_port = null, database = true, publishes = true, cache = false
      calls    = ["adaptador-pagos"]
      consumes = ["dinero-aceptaciones"], allies = []
      stories  = ["HU-W16"]
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
    # HU-W18/HU-M10: la proyección se alimenta de los cambios de las pólizas (y de los siniestros).
    consulta-proyeccion = {
      filter             = { eventType = [{ prefix = "Poliza" }, { prefix = "Siniestro" }] }
      visibility_timeout = 30
      max_receive_count  = 5
    }
    # HU-M11: aviso push de la emisión y del pago confirmado.
    avisos = {
      filter             = { eventType = ["PolizaEmitida", "RecaudoConfirmado"] }
      visibility_timeout = 60
      max_receive_count  = 5
    }
  }

  # Aliados externos: el secreto guarda credenciales; la URL se configura en apps.
  allies = {
    open-finance   = { description = "Proveedor de Finanzas Abiertas (ingresos y obligaciones)" }
    datos-abiertos = { description = "Fuente de Datos Abiertos para perfilamiento" }
    kyc            = { description = "Proveedor de identidad, KYC y prueba de vida" }
    pagos          = { description = "Pasarela de medios de pago (recaudo de primas)" }
    firma          = { description = "Proveedor de firma electrónica de pólizas" }
    push           = { description = "Proveedor de notificaciones push a dispositivos" }
  }
}
