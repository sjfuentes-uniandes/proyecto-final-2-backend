# HU-W29 · Manifiesto del experimento multizona

Issue [#71](https://github.com/sjfuentes-uniandes/proyecto-final/issues/71) · ASR [ARQ-001](https://github.com/sjfuentes-uniandes/proyecto-final/issues/28) · Ambiente `exp` (D4) · Guía: [docs/hu-w29-multizona.md](../../docs/hu-w29-multizona.md)

> Este manifiesto se completa y se versiona **antes** de ejecutar. Lo que se mida después se compara contra lo escrito aquí; si algo cambia, se registra como desviación en `informe.md`, no se reescribe este archivo.

## 1. Objetivo de recuperación (fijado antes de ejecutar)

ARQ-001 pide disponibilidad y confiabilidad de punta a punta con un **SLO mensual ≥ 99,95 % por componente** y monitoreo independiente. No fija un tiempo de recuperación; con el responsable de la historia se acordó:

| # | Criterio | Meta | Cómo se mide |
| --- | --- | --- | --- |
| O1 | Tiempo de redirección | **≤ 10 min** desde la inyección | `continuidad.redireccionSegundos` de `informe.json`: inicio del primer minuto desde el cual la carga se mantiene con tasa de error ≤ 1 % (`error_rate_percent` de HU-W28) |
| O2 | Operaciones confirmadas perdidas (R1, R3 y R5) | **0** | `resumen.perdidas` y `eventos_evaluados`, `siniestro_por_activacion` sin `DIFERENCIA` |
| O3 | Operaciones confirmadas o siniestros paramétricos duplicados | **0** | `resumen.duplicadas` |
| O4 | Cobros (R3) o indemnizaciones (R5) duplicados | **0** | `resumen.cobrosDuplicados` |
| O5 | Mensajes en DLQ sin explicación | **0** | `resumen.dlqSinExplicacion` |
| O6 | Cronología | Detección, failover y recuperación con hora | `cronologia.ndjson`, `alarmas.json` y `fis-experimento.json` |

**Presupuesto de error.** 99,95 % mensual equivale a ~21,6 min de indisponibilidad al mes. Una redirección de 10 min consume hasta ~46 % del presupuesto. La alarma de disponibilidad de HU-W28 usa 99,9 % (`availability_percent`), más laxa que la ficha; el informe reporta ambos valores.

**Hipótesis.** Con tareas en las zonas A y B (mínimo 2 por servicio crítico), NAT Gateway por zona y RDS Multi-AZ, la pérdida total de la red de la zona A durante 10 min:
1. saca del balanceo a las tareas de A (ALB interno y Service Connect, "Monitor de salud y retiro de réplicas fallidas" de `modelo_concurrencia.puml`);
2. promueve el standby de RDS en B ("El standby atiende fallas de instancia o zona", `diagrama_estrategia_replicacion_recuperacion.puml`);
3. mantiene R1 y R3 operando por la zona B ("operación activa-activa entre dos zonas de una región", `modelo_despliegue.puml`);
4. no pierde ni duplica operaciones confirmadas: `Idempotency-Key` en el borde, `eventoId` determinista de la telemetría, unicidad por clave en las bases, Outbox e Inbox.

## 2. Falla inyectada

| Acción FIS | Objetivo | Parámetros |
| --- | --- | --- |
| `aws:network:disrupt-connectivity` | Subredes **privadas y de datos de la zona A** | `scope = all`, `duration = PT10M` |
| `aws:rds:reboot-db-instances` | Base del ambiente | `forceFailover = true` |

- **Condición de parada:** alarma crítica `solventa-exp-api-canales-5xx` (HU-W28). Si la tasa de 5XX del API de canales supera `api_5xx_rate` (5 %) en 3 de 5 minutos, FIS restaura las ACL y termina. Una parada así se reporta como **no cumplido**: indica que la zona sana no absorbió la carga.
- **Caché:** Valkey tiene un solo nodo. Si quedó en la zona A, se pierde la caché (Cache-Aside): sube la latencia y no se pierden datos. Se registra la zona del nodo en la evidencia.
- **Fuera de alcance:** falla regional (arquitectura objetivo, `modelo_despliegue.puml`).

## 3. Carga de fondo

`carga.js` (k6) por el borde real, con `Idempotency-Key` y reintento con la misma clave:

| Recorrido | Tasa | Qué se confirma |
| --- | --- | --- |
| R1 cotizar | 60/min | Cotización 201 (`cotizaciones.idempotency_key`) |
| R3 aceptar | 6/min (perfil → cotización → decisión → aceptación) | Aceptación 201 (`aceptaciones.idempotency_key`) y su cobro (`recaudos.clave_recaudo = aceptacionId`) |
| R5 evento de telemetría firmado | 6/min (`LLUVIA_ACUMULADA` de 135 mm en `05001`) | Evento 202/200 (`eventos_recibidos.id_externo`), evaluado una vez (`eventos_evaluados`), un siniestro por activación (`siniestros`, UNIQUE evento-póliza) y un pago por siniestro (`indemnizaciones`) |

Duración: 30 min (≥ 5 min antes de la inyección, 10 min de falla y ≥ 15 min de recuperación). Ventana de la reconciliación: inicio de la carga − 1 min a fin + 1 min.

## 4. Configuración ejecutada (completar antes de ejecutar)

| Dato | Valor |
| --- | --- |
| Commit del backend | `f44268c07e23e60bb21de06f39fe73de9e0b59c0` |
| Commit del frontend | `8fce36032afa500d769b06727340ef8207e342e7` |
| Rama | `main` |
| Digests de imágenes | `{
  "image_digests": {
    "pagos-parametricos": "sha256:4a4914e5d221ba0ac0a2cb24ac48b4415c9f8395fe223a2139ed24aeedfdd6e7",
    "adaptador-datos": "sha256:b1e24d09e5e6986d02ee06cc9befefddd859fd875bde71fe02fbd5c3237f2bc3",
    "polizas": "sha256:6adae90754bc6f356811e9ab28452a43f4008f7548417875c6888664f5118a06",
    "catalogo": "sha256:9acde7002dfa963338ce58fcca9854a1a7b6f46ca9e8aa9336c2969171a58cd9",
    "dinero": "sha256:6cf0ea77bdfddbc6bbb070acdcdbed908bbcb38d509cca45bf08bf2e236c35b9",
    "api-socios": "sha256:012308e0be8b6421df88c4065ecbe2019522152b53de06d687f5dd99cea36340",
    "simulador-aliados": "sha256:691c3f8689a34e3e68e9ea99dc91bcaa2934ed29bb6bae78a0e66ab64b4d2e9d",
    "cotizacion": "sha256:94b6d92524c0985acb7eac270093cfe6694c98f4039d762a9e6b1bcbffbcd212",
    "avisos": "sha256:fa30b5a54c4e2a78f169cb715c59b809ddac0934812159ef2a8c6302de0d8bea",
    "bff-movil": "sha256:6a1fc1d7e703d8671f14090cefb3e284220846ef3d9748ac212fbfc4dd7bc6cf",
    "adaptador-pagos": "sha256:f685bf6341decf950a15205fa637bd51c3ff0018c78cf274892fb22aa9c92f72",
    "clientes": "sha256:ce35cdb9dc9dbd705d0c6e23a19e5eb1e3f1370d7ae469956bec3c480353835c",
    "consulta": "sha256:dd481571b089c73dcef22b70c66b5cbd7cd0fc30e7c8491c6bba049932f1173d",
    "bff-web": "sha256:de90266c97c1209e4b19b60e7e09fecb7125a25b0439eddb296dfc7405c901ca",
    "adaptador-telemetria": "sha256:4e29d41d9ded2b3dea151d205dffa8d452f7bc4f8f2f21ca49883f006a2eb7df",
    "adaptador-identidad": "sha256:ff7190f8a6afc85b79f2f0df9c6cc18b44bf416e571cd1120b178fb66db3716d",
    "siniestros": "sha256:26faddd1d61e8f2036c53f1a00351fd4b2ed0b3274417546f6568444238ff00f",
    "auditoria": "sha256:2a56b10b4f242231df940d6c3b4d3bdfe7536d027f934858544e1bd577cad4dc",
    "adaptador-firma": "sha256:88b5a6b2c89e811ffd4eae731b55cf8eaa9e33db1e93e03bbfb8ca0fc259c93c"
  }
}`  |
| tfvars | [`infra/envs/exp/platform.tfvars`](../../infra/envs/exp/platform.tfvars), [`infra/envs/exp/apps.tfvars`](../../infra/envs/exp/apps.tfvars) al commit anterior |
| Región y zonas | `us-east-1`; zona A = `<experiment.zone>` |
| Base | `db.t4g.small` Multi-AZ, primaria en `<zona>` y standby en `<zona>` |
| Nodo de caché | `<zona del nodo Valkey>` |
| k6 | `2.1.0` |
| Secreto de telemetría | Cargado en `solventa-exp/aliados/telemetria` (no se versiona; solo la fecha de carga) |
| Coberturas R5 | `COB-*` de `contracts/datos-sinteticos/eventos-parametricos.json` publicadas (fecha) |
| Inicio de la carga / inyección / fin | `<UTC>` |

```bash
git rev-parse HEAD; (cd ../proyecto-final-2-frontend && git rev-parse HEAD)
cat infra/envs/exp/images.tfvars.json
terraform -chdir=infra/platform output -json experiment | jq '{zone, db_multi_az}'
aws elasticache describe-cache-clusters --query 'CacheClusters[?starts_with(CacheClusterId, `solventa-exp`)].PreferredAvailabilityZone'
k6 version
```

## 5. Costo estimado

Precios de lista de `us-east-1`; confirmarlos al desplegar.

| Recurso | Cantidad | USD/h aprox. |
| --- | --- | --- |
| Fargate (sin Spot) | 19 servicios × 2 tareas (13 de 0,25 vCPU/1 GB; 5 de 0,5 vCPU/1 GB; 1 de 1 vCPU/2 GB) | 0,73 |
| RDS `db.t4g.small` Multi-AZ | 1 | 0,06 |
| NAT Gateway | 2 (+ 0,045 USD/GB procesado) | 0,09 |
| ALB interno, Valkey, WAF, alarmas y secretos | — | 0,08 |
| **Ambiente** | | **≈ 1,0 USD/h** |
| AWS FIS | 0,10 USD por acción-minuto: 10 min de red + ~1 min de RDS | ≈ 1,1 USD por ejecución |

Una corrida completa (desplegar, humo, 30 min de carga, recolectar y destruir) toma ~3 h: **≈ 4–5 USD**. Cada despliegue requiere confirmación explícita.

## 6. Procedimiento

Cada paso con AWS requiere confirmación; detalle en [docs/hu-w29-multizona.md](../../docs/hu-w29-multizona.md#3-experimento-en-exp).

1. `make infra-desplegar ENV=exp` → secreto `hmac_secret` de prueba en `solventa-exp/aliados/telemetria` → `make infra-probar ENV=exp` → `./distribucion.sh antes` (N2: cada servicio crítico con tareas saludables en dos zonas) → coberturas sintéticas (E2E de HU-W21 con `E2E_PUBLICAR_COBERTURAS=1`) → R1, R3 y R5 en verde (`make test-e2e` con `-k "hu_w03 or hu_w13_w15 or hu_w21 or hu_w22"`).
2. `./preparar-fis.sh` y crear la plantilla.
3. Iniciar `carga.js` (30 min). A los 5 min, iniciar el experimento FIS; `./distribucion.sh durante` a los 5 min de la inyección.
4. Al terminar la carga: `./distribucion.sh despues`, `./recolectar.sh`, `./extraer.sh`, revisar la DLQ (`explicaciones.json`) y `reconciliar.py --inyeccion <startTime de la acción>`.
5. `./publicar.sh` y capturas de I04 e I05.
6. `make infra-destruir ENV=exp` y confirmar que no quedan recursos `solventa-exp-*`.

Si algo falla a mitad: detenerse, recolectar lo que haya y consultar antes de destruir.
