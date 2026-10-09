# HU-W27: métricas y trazas correlacionadas (verificación N3 en AWS)

Se ejecuta desde la raíz del repo con credenciales de la cuenta del ambiente (`AWS_PROFILE`/`AWS_REGION`) y `ENV=int` (valor por defecto). Contrato de la instrumentación: [infra/PLATAFORMA.md](../infra/PLATAFORMA.md#contratos-para-los-servicios).

## Antes de AWS: colector local

```bash
make test-module                          # N2: OTLP real contra un colector en proceso
docker compose up -d otel-collector api-socios
curl -si localhost:8082/health -H "X-Correlation-Id: prueba-123" | grep -i correlation
docker compose logs otel-collector | grep -E "OperationDuration|spans"
```

## 0. Desplegar

```bash
# Si es la primera vez después de esta historia: encabezado X-Authenticated-Groups en API Gateway.
make infra-plataforma

# Imagen nueva de api-socios (instrumentada) y apps: colector con anotaciones, tablero e IAM.
make infra-microservicios SERVICES=api-socios
make infra-probar
```

**Verificar:** `make infra-probar` termina sin fallas. El chequeo 3 ya confirma que el borde devuelve `X-Correlation-Id`.

## 1. Llamar al API de socios con un correlationId

```bash
SOCIOS=$(terraform -chdir=infra/platform output -json api_urls | jq -r '.socios_mtls // .socios')
PARTNER=$(terraform -chdir=infra/platform output -json partners \
  | jq -c 'to_entries | map(select(.value.enabled and .value.client_id != null)) | first')
TOKEN_URL=$(terraform -chdir=infra/platform output -json cognito | jq -r '.partners.token_url')
TOKEN=$(curl -s -u "$(jq -r .value.client_id <<<"$PARTNER"):$(jq -r .value.client_secret <<<"$PARTNER")" \
  -d grant_type=client_credentials "$TOKEN_URL" | jq -r .access_token)

# AC2: el ID del socio vuelve en la respuesta.
curl -si "$SOCIOS/health" -H "Authorization: Bearer $TOKEN" \
  -H "x-api-key: $(jq -r .value.api_key <<<"$PARTNER")" -H "X-Correlation-Id: prueba-123" | grep -i correlation

# AC1: sin ID se usa el requestId de API Gateway (o un uuid4).
curl -si "$SOCIOS/health" -H "Authorization: Bearer $TOKEN" \
  -H "x-api-key: $(jq -r .value.api_key <<<"$PARTNER")" | grep -i correlation

# ID inválido: se descarta y se responde con otro válido.
curl -si "$SOCIOS/health" -H "Authorization: Bearer $TOKEN" \
  -H "x-api-key: $(jq -r .value.api_key <<<"$PARTNER")" -H "X-Correlation-Id: no válido<script>" | grep -i correlation
```

**Verificar:**
- La primera respuesta trae `X-Correlation-Id: prueba-123`.
- La segunda trae un UUID (el `requestId` de API Gateway).
- La tercera trae un ID distinto de `no válido<script>`.

## 2. Logs del recorrido

```bash
aws logs tail /ecs/solventa-int/api-socios --since 15m --filter-pattern '"prueba-123"'
```

**Verificar:** hay una línea JSON con:
- `"service":"api-socios"`, `"environment":"int"`, `"operation":"GET /health"`, `"result":"ok"`.
- `duration_ms` numérico, `"correlation_id":"prueba-123"` y `trace_id` con formato `1-xxxxxxxx-...`.
- Ningún `Authorization`, token ni API key en la línea.

```bash
# El healthcheck de ECS (cada 10 s desde 127.0.0.1) no debe aparecer.
aws logs tail /ecs/solventa-int/api-socios --since 5m --filter-pattern '"GET /health"' | wc -l
```

## 3. Traza en X-Ray

```bash
DESDE=$(date -u -v-30M +%s 2>/dev/null || date -u -d '30 min ago' +%s)
aws xray get-trace-summaries --start-time "$DESDE" --end-time "$(date -u +%s)" \
  --filter-expression 'annotation.correlation_id = "prueba-123"' \
  --query 'TraceSummaries[].{Id:Id,Duracion:Duration,Entrada:EntryPoint.Name}'
# Con el Id anterior:
aws xray batch-get-traces --trace-ids <Id> --query 'Traces[].Segments[].Document' --output text | jq '{name, annotations}'
```

**Verificar:** la traza existe, su segmento es `api-socios` y las anotaciones incluyen `correlation_id = prueba-123`. El `Id` coincide con el `trace_id` del log. También se puede buscar en la consola: CloudWatch › X-Ray traces › Query `annotation.correlation_id = "prueba-123"`.

## 4. Métricas y tablero

```bash
aws cloudwatch list-metrics --namespace Solventa --metric-name OperationDuration \
  --dimensions Name=Environment,Value=int Name=Service,Value=api-socios
make infra-salidas   # imprime dashboard_url
```

**Verificar:**
- Las métricas tardan hasta 2 minutos en aparecer (lote EMF de 60 s). Existe `OperationDuration` con `Operation=GET /health`, y también `OperationErrors`.
- En el tablero `solventa-int`, los widgets "Volumen por operación", "Tasa de error (%)" y "Latencia p95 por operación (ms)" muestran la serie `api-socios / GET /health`.
- La variable **Servicio** (arriba del tablero) filtra las búsquedas. Al elegir `api-socios` quedan solo sus series; al elegir otro servicio no queda ninguna.
- La tabla "Resumen por operación" muestra volumen, `tasa_error_pct` y `p95_ms` por `service` y `operation`.
- "Tareas en ejecución por servicio" muestra una línea por servicio desplegado. Junto a "ECS CPU/memoria", "Destinos saludables" y "Colas: profundidad y antigüedad" completa las métricas mínimas de la historia.

## 5. Pantalla Operación › Trazas (portal)

Requiere `bff-web`, el portal publicado y un usuario del back-office en el grupo `operacion` (guía en [infra/DESPLIEGUE.md](../infra/DESPLIEGUE.md#71-usuarios-del-back-office)):

```bash
make infra-microservicios SERVICES=bff-web
EMAIL=<correo> NOMBRE="<nombre>" make infra-usuario-admin      # administradores + operacion
# En proyecto-final-2-frontend:
make desplegar ENV=int
```

**Verificar:**
- En `<portal>/ingresar`, el primer ingreso pide cambiar la contraseña temporal y registrar el autenticador. Después se llega a **Operación › Trazas**.
- Al buscar `prueba-123`, la pantalla muestra el desglose, la interpretación y el contexto de la traza.
- Un usuario creado en **Administración › Usuarios** solo con `administradores-socios` no ve Operación, y la API responde 403.
- Si un usuario del grupo recibe 403, revisa en CloudWatch Logs del BFF el encabezado `X-Authenticated-Groups`. La expresión `context.authorizer.claims.cognito:groups` de `infra/platform/edge.tf` es lo que se valida aquí.

## Seguridad (AC4)

```bash
uvx bandit -r libs/solventa-common/src services/bff-web/src -ll
uvx semgrep scan --config p/python --config p/secrets --severity ERROR libs/solventa-common/src services/bff-web/src
```

Las pruebas `tests/module/test_recorrido_instrumentado.py::test_seguridad_*` y `tests/unit/test_logging.py` verifican que logs y trazas no contienen secretos, tokens, biometría ni montos.
