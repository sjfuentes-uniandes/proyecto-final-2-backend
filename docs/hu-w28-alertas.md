# HU-W28: generación de alertas (verificación N2 y aceptación en AWS)

Se ejecuta desde la raíz del repo con credenciales de la cuenta del ambiente (`AWS_PROFILE`/`AWS_REGION`) y `ENV=int` (valor por defecto). Umbrales en `infra/envs/<env>/apps.tfvars`; destinatarios en `alert_emails` de `platform.tfvars`.

## Antes de AWS

```bash
make infra-validar                                            # N1: fmt, validate y terraform test
shellcheck -x -P scripts/plataforma scripts/plataforma/*.sh
make test-cov SERVICE=bff-web && make test-contract SERVICE=bff-web
make test-cov SERVICE=api-socios
```

| Prueba N1 | Dónde |
| --- | --- |
| Umbral desde variables, ventana de 60 s, 3 de 5 periodos y `treat_missing_data = notBreaching` | `infra/apps/tests/apps.tftest.hcl` (`todos_los_servicios`, `umbrales_configurables`) |
| La alarma del perfilamiento existe sin `cotizacion` desplegado | `despliegue_parcial` |
| Sin `alarm_actions`/`ok_actions`: un solo correo por cambio de estado (supresión de duplicados) | `todos_los_servicios` |
| Transición ALARMA→RECUPERADA: regla de OK solo desde ALARM | `todos_los_servicios` |
| Mensaje transformado sin cuenta, ARN, tokens ni dimensiones | `todos_los_servicios` |
| Tasa de error = `OperationErrors / SampleCount(OperationDuration)` por servicio | `todos_los_servicios` |
| Solo las reglas de alertas publican en el tópico; solo `alert_emails` reciben | `infra/platform/tests/platform.tftest.hcl` |
| Resumen del mes, alertas abiertas, catálogo y permisos de `bff-web` | `services/bff-web/tests/unit/test_alertas.py` |
| `PartnerQuotaUsage` por socio activo | `services/api-socios/tests/unit/test_cuota_publicada.py` |

## 0. Desplegar

```bash
# 1. Política del tópico: solo las reglas de EventBridge del ambiente publican.
make infra-plataforma
# 2. Alarmas sin acciones, reglas de EventBridge, IAM y variables de bff-web y api-socios.
make infra-microservicios SERVICES=api-socios,bff-web
# 3. Pantalla Operación › Alertas (en proyecto-final-2-frontend).
make desplegar ENV=int
```

**Verificar:**
- Cada correo de `alert_emails` confirmó la suscripción (`aws sns list-subscriptions-by-topic --topic-arn $(terraform -chdir=infra/platform output -raw alerts_topic_arn)` no muestra `PendingConfirmation`).
- `terraform -chdir=infra/apps output alarms` lista las alarmas con su severidad. Las dos del perfilamiento existen aunque `cotizacion` no esté desplegado.

## 1. N2: activar y recuperar la alarma con una métrica sintética (AC1, AC2, AC3, AC4)

```bash
NIVEL=advertencia make infra-probar-alerta ENV=int   # 1 correo de ALARMA y 1 de RECUPERADA
make infra-probar-alerta ENV=int                     # crítica: 2 y 2 (advertencia y crítica)
```

**Verificar:**
- El script termina con `Resultado: OK` y sale con 0. Imprime el tiempo hasta la alerta (3–5 min) y hasta la recuperación (3–5 min).
- Llega un correo de **ALARMA** por cada alarma que se abre, aunque el script publique varias mediciones (AC2). El correo incluye estado, ambiente, alarma, severidad, operación, umbral, valor observado, hora y enlace al tablero (AC1).
- Llega un correo de **RECUPERADA** por cada alarma (AC3).
- Repetir el script da el mismo resultado (AC4). Formato del correo: [infra/DESPLIEGUE.md](../infra/DESPLIEGUE.md#52-probar-las-alertas-hu-w28).

```bash
aws cloudwatch describe-alarm-history --alarm-name solventa-int-perfilamiento-p95-critica \
  --history-item-type StateUpdate --max-records 4 --query 'AlarmHistoryItems[].HistorySummary'
```

## 2. Pantalla Operación › Alertas (aceptación manual)

Con un usuario del grupo `operacion` (`EMAIL=<correo> make infra-usuario-admin`), abrir `<portal>/operacion/alertas` mientras el script del paso 1 mantiene la alarma abierta.

**Verificar:**
- "Alertas abiertas" muestra la alarma con su severidad, el valor actual, el umbral y hace cuánto se abrió. "Ver traza" abre Operación › Trazas filtrando por la duración mínima del umbral.
- "Umbrales configurados" coincide con `apps.tfvars` (meta, advertencia y crítica).
- "Resumen del mes" cuenta la alerta al recuperarse.
- Captura de los dos correos y del tablero enlazado (`make infra-salidas` imprime `dashboard_url`).

## Decisiones

| Tema | Decisión |
| --- | --- |
| Correo legible | Dos reglas de EventBridge (`solventa-<env>-alertas-alarma` y `-recuperada`) con *input transformer* hacia el tópico, sin Lambda. **Reemplazan** las acciones directas de las alarmas: si se mantuvieran ambas, llegarían dos correos por cambio (JSON en inglés y texto en español). La política del tópico solo admite esas reglas (`aws:SourceArn`). El asunto lo fija SNS (`AWS Notification Message`) y no se puede cambiar sin Lambda. |
| Duplicados (AC2) | CloudWatch emite el evento solo cuando cambia el estado. Una condición sostenida produce un solo correo, sin importar cuántas mediciones lleguen. `INSUFFICIENT_DATA` no notifica, y OK solo notifica si viene de ALARM. |
| Severidad | Por meta, una alarma de advertencia (`warning_ratio`, 90 %; se puede fijar por meta con `warning_ms` o `*_warning_percent`) y una crítica (la meta). La DLQ es informativa. La severidad va en la descripción y en `ALERTAS_CATALOGO`. |
| Tasa de error | Por servicio desplegado: `IF(total > 0, 100 * FILL(errores, 0) / total, 0)` con `OperationErrors` (Sum) y `OperationDuration` (SampleCount), dimensiones `Environment` y `Service`. Se mide como proporción, no como cantidad. |
| Disponibilidad | Por API: `100 * (1 - 5XXError / Count)` en ventanas de `availability_period_seconds` (1 h). La meta mensual se vigila por hora: una alarma mensual no se puede evaluar en CloudWatch. |
| Cuota de socio | `api-socios` publica `PartnerQuotaUsage` cada 5 min. Las alarmas usan Metrics Insights (`MAX` entre socios), porque los socios se crean en tiempo de ejecución y Terraform no puede crear una alarma por socio. |
| Resumen del mes | Historial de alarmas de los últimos 30 días (su retención). Un **falso positivo** es una alerta que se recuperó en menos de `false_positive_minutes` (5). Una **meta incumplida** es una alerta crítica que duró, o lleva abierta, más de `alert_attention_minutes` (30). El **tiempo medio de atención** es la duración media hasta RECUPERADA. |
| Configurar umbrales | El botón del mockup no se implementa: los umbrales se cambian en `apps.tfvars` y se aplican con `make infra-aplicaciones`, no desde el portal. |
| Permisos de `bff-web` | Solo lectura: `DescribeAlarms`, `DescribeAlarmHistory` y `GetMetricData` con `Resource = "*"`, porque el historial sin nombre de alarma y Metrics Insights no admiten ARN. Filtra por `ALERTAS_PREFIJO`. No puede crear, cambiar ni silenciar alarmas. |
| Costo | ~67 alarmas y ~93 métricas con los 10 servicios: ~8 USD/mes (antes ~2,4). Con menos servicios desplegados bajan las de tasa de error y saturación. |

## Seguridad

- Destinatarios: solo las suscripciones de `alert_emails` (prueba en `platform.tftest.hcl`). Cada correo debe confirmar la suscripción.
- Mensajes: el *input transformer* solo toma `alarmName`, `configuration.description`, `state.reason`, `state.reasonData` y `time`. No incluye la cuenta, ARN, dimensiones ni datos de negocio. El valor observado son datos de la métrica.
- Configuración analizada: `make infra-validar` (`fmt`, `validate`, `test`) y `shellcheck` en el workflow **Infra - Validar**.
