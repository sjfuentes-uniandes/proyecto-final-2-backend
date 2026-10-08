# Catálogo único de despliegables. platform crea lo que no depende de imágenes
# (ECR, bases, colas, destinos del ALB) y apps lo consume desde el output
# `platform` para crear las tareas. Agregar un servicio aquí y aplicar platform
# antes de publicar su imagen.
#
# tier:      acceso | nucleo | adaptador | worker | soporte (modelo de despliegue)
# listener_port: puerto del listener del ALB interno (solo servicios de acceso)
# database:  base PostgreSQL privada y usuario propio en la instancia RDS
# calls:     dependencias síncronas por ECS Service Connect (REST interno)
# publishes: publica eventos del Outbox en el tópico de negocio (SNS)
# consumes:  colas SQS que procesa (claves de local.queues)
# allies:    credenciales de aliados externos que usa (claves de local.allies)
locals {
  catalog = {
    "bff-web" = {
      tier     = "acceso", listener_port = 8081, database = false, publishes = false
      calls    = ["clientes", "cotizacion", "api-socios"]
      consumes = [], allies = []
      stories  = ["HU-W01", "HU-W02", "HU-W10", "HU-W11", "HU-W12", "HU-W30", "HU-W31"]
    }
    "bff-movil" = {
      tier     = "acceso", listener_port = 8082, database = false, publishes = false
      calls    = ["clientes"]
      consumes = [], allies = []
      stories  = ["HU-M06", "HU-M07", "HU-M08", "HU-M09"]
    }
    "api-socios" = {
      tier     = "acceso", listener_port = 8083, database = true, publishes = true
      calls    = ["cotizacion"]
      consumes = [], allies = []
      stories  = ["HU-W01", "HU-W02"]
    }
    "clientes" = {
      tier     = "nucleo", listener_port = null, database = true, publishes = true
      calls    = ["adaptador-identidad"]
      consumes = [], allies = []
      stories  = ["HU-W30", "HU-W31", "HU-M06", "HU-M08", "HU-M09"]
    }
    "catalogo" = {
      tier     = "nucleo", listener_port = null, database = true, publishes = false
      calls    = []
      consumes = [], allies = []
      stories  = ["HU-W11"]
    }
    "cotizacion" = {
      tier     = "nucleo", listener_port = null, database = true, publishes = true
      calls    = ["catalogo", "clientes", "adaptador-datos"]
      consumes = ["consentimientos-cotizacion"], allies = []
      stories  = ["HU-W10", "HU-W11", "HU-W12", "HU-W31"]
    }
    "adaptador-datos" = {
      tier     = "adaptador", listener_port = null, database = false, publishes = false
      calls    = ["simulador-aliados"]
      consumes = [], allies = ["open-finance", "datos-abiertos"]
      stories  = ["HU-W10", "HU-W12"]
    }
    "adaptador-identidad" = {
      tier     = "adaptador", listener_port = null, database = false, publishes = false
      calls    = ["simulador-aliados"]
      consumes = [], allies = ["kyc"]
      stories  = ["HU-W30", "HU-M06"]
    }
    "auditoria" = {
      tier     = "worker", listener_port = null, database = false, publishes = false
      calls    = []
      consumes = ["auditoria"], allies = []
      stories  = ["HU-W31", "HU-M08", "HU-M09", "HU-W27"]
    }
    "simulador-aliados" = {
      tier     = "soporte", listener_port = null, database = false, publishes = false
      calls    = []
      consumes = [], allies = []
      stories  = ["HU-W10", "HU-W12", "HU-W30", "HU-M06"]
    }
  }

  # Un servicio anuncia su nombre en Service Connect solo si otro lo invoca.
  discoverable = toset(flatten([for service in values(local.catalog) : service.calls]))

  database_services = toset([for name, service in local.catalog : name if service.database])
  access_services   = { for name, service in local.catalog : name => service if service.listener_port != null }

  # Colas por consumidor suscritas al tópico de eventos de negocio. El filtro se
  # aplica sobre el atributo de mensaje `eventType` que publica cada Outbox.
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
  }

  # Aliados externos: el secreto guarda credenciales; la URL se configura en apps.
  allies = {
    open-finance   = { description = "Proveedor de Finanzas Abiertas (ingresos y obligaciones)" }
    datos-abiertos = { description = "Fuente de Datos Abiertos para perfilamiento" }
    kyc            = { description = "Proveedor de identidad, KYC y prueba de vida" }
  }
}
