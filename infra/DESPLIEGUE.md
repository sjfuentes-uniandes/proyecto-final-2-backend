# Despliegue de la plataforma Solventa

Esta guía cubre cómo crear, actualizar y destruir un ambiente completo de la plataforma: `infra/platform` + `infra/apps`. Qué contiene cada raíz y por qué está en [PLATAFORMA.md](PLATAFORMA.md).

Todo se hace con **un solo comando** desde la máquina local (`make infra-*`) o desde **GitHub Actions**. Ambos caminos ejecutan los mismos scripts (`scripts/plataforma/*.sh`), así que el resultado es idéntico.

## 1. Piezas y dependencias

```
infra/bootstrap  (una vez por cuenta; nunca se destruye)
   │  bucket S3 de estado  +  rol OIDC para GitHub Actions
   ▼
infra/platform   (paso 1)  ── crea los repositorios ECR, RDS, red, borde...
   │
   ├──► tarea db-bootstrap (paso 2)   crea base y usuario por servicio en RDS
   │
   ├──► imágenes (paso 3)             build + push a los repositorios ECR de platform
   │
   ▼
infra/apps       (paso 4)  ── lee el estado de platform y despliega SOLO los servicios con imagen en ECR
```

| Paso | Necesita | Produce |
| --- | --- | --- |
| 0. Bootstrap | Credenciales de administrador | Bucket `solventa-tfstate-<cuenta>` y rol `solventa-github-deploy` |
| 1. Plataforma | Bootstrap | Todo lo que no depende de imágenes, incluidos los repositorios ECR vacíos |
| 2. Bases | Plataforma (RDS disponible) | Una base y un usuario por servicio con base de datos |
| 3. Imágenes | Plataforma (repositorios ECR) y código en `services/<servicio>/Dockerfile` | Imágenes con etiqueta inmutable en ECR |
| 4. Aplicaciones | Plataforma + al menos una imagen en ECR (y paso 2 para servicios con base) | Servicios ECS, IAM, autoescalado, alarmas y tablero |

**Dependencia de imágenes:** `apps` despliega exactamente los servicios que tienen imagen en ECR.
- `scripts/plataforma/digests.sh` toma el digest **más reciente de cada repositorio** y lo escribe en `infra/envs/<ambiente>/images.tfvars.json`, que no se versiona.
- Si ningún servicio tiene imagen, el paso 4 no hace nada y avisa.
- Publicar un solo microservicio no retira a los demás.
- `terraform -chdir=infra/apps output pending_services` lista los servicios del catálogo que todavía no tienen imagen.

**Orden entre microservicios:** `apps` crea primero a los servicios invocados y después a quienes los invocan (capas de Service Connect). Si en un despliegue aparece un servicio nuevo que otros ya invocaban, el script fuerza un nuevo despliegue de esos llamadores para que lo descubran.

## 2. Requisitos

| Herramienta | Uso |
| --- | --- |
| Terraform ≥ 1.10 | Backend S3 con bloqueo nativo (`use_lockfile`) |
| AWS CLI v2 | Credenciales, ECR, ECS |
| Docker con Buildx | Build `linux/amd64` de las imágenes |
| jq, git, make, bash ≥ 4 | Scripts |

Credenciales de AWS en la terminal (`aws configure`, `AWS_PROFILE` o SSO), verificables con `aws sts get-caller-identity`.

## 3. Configuración por ambiente

Cada ambiente es una carpeta versionada `infra/envs/<ambiente>/` sin secretos:

| Archivo | Contenido |
| --- | --- |
| `platform.tfvars` | Red, perfil de costo, RDS, Cognito, cuotas de socios, WAF, correos de alertas. `environment` debe ser igual al nombre de la carpeta. |
| `apps.tfvars` | Tamaño de tareas, réplicas, Spot, pausa, endpoints de aliados y umbrales de alarmas. |
| `images.tfvars.json` | Se genera en cada despliegue (ignorado por Git). |

El estado remoto queda en `s3://solventa-tfstate-<cuenta>/<ambiente>/{platform,apps}.tfstate`, con bloqueo, así que dos despliegues simultáneos del mismo ambiente no se pisan.

Para crear otro ambiente, por ejemplo `qa`:

```bash
cp -r infra/envs/int infra/envs/qa
# editar infra/envs/qa/platform.tfvars: environment = "qa" y otro vpc_cidr si se desea
make infra-desplegar ENV=qa
```

**Secretos de aliados:** Terraform crea los secretos `solventa-<ambiente>/aliados/{kyc,open-finance,datos-abiertos}` con un valor de marcador. Mientras no se carguen las credenciales reales, los adaptadores usan `simulador-aliados`. Para cargarlas, después del paso 1:

```bash
aws secretsmanager put-secret-value --secret-id solventa-int/aliados/kyc \
  --secret-string '{"client_id":"...","client_secret":"..."}'
```

## 4. Primera vez en una cuenta (bootstrap)

```bash
make infra-bootstrap AWS_REGION=us-east-1
```

- **Crea** el bucket de estado (versionado, cifrado, sin acceso público y protegido contra borrado) y el rol `solventa-github-deploy`, que solo pueden asumir workflows de `sjfuentes-uniandes/proyecto-final-2-backend`, identificado por los ID del dueño y del repositorio (`sjfuentes-uniandes@196879525/proyecto-final-2-backend@1409601103`).
- **Crea** también el rol `solventa-github-web-deploy` para el repo del portal (`sjfuentes-uniandes@196879525/proyecto-final-2-frontend@1409599583`, variable `github_web_repository`). Solo puede leer `/solventa/*/web/config` en SSM, escribir en los buckets `solventa-*-web-<cuenta>` e invalidar CloudFront: no toca Terraform ni su estado.
- **Muestra** `AWS_ROLE_ARN` y `AWS_WEB_ROLE_ARN` para configurar GitHub (sección 7). Si el bootstrap ya estaba aplicado, basta con volver a ejecutar `make infra-bootstrap` para crear el rol del portal.
- **Proveedor OIDC existente:** si la cuenta ya tiene el proveedor OIDC de GitHub, usar `CREATE_GITHUB_OIDC_PROVIDER=false make infra-bootstrap`.
- **Estado local:** el estado del bootstrap queda en `infra/bootstrap/terraform.tfstate` (ignorado por Git). Guardarlo; si se pierde, los recursos se pueden importar de nuevo.

## 5. Despliegue con make

Todos los targets aceptan `ENV=<ambiente>` (por defecto `int`) y `AUTO_APPROVE=1` para no pedir confirmación. `make infra-ayuda` los lista.

### Todo de una vez

```bash
make infra-desplegar ENV=int
```

Ejecuta en orden los cuatro pasos de la sección 1:

| Paso | Qué hace | Duración aproximada |
| --- | --- | --- |
| **1. `infra-plataforma`** | `terraform apply` de `infra/platform` | 15–25 min (RDS, CloudFront y NAT son lo más lento) |
| **2. `infra-bases`** | Ejecuta la tarea `db-bootstrap`, espera su fin y falla si su código de salida no es 0 | 1–2 min |
| **3. `infra-imagenes`** | Build y push de cada servicio del catálogo que tenga `services/<servicio>/Dockerfile` | Depende de los servicios |
| **4. `infra-aplicaciones`** | Genera los digests, `terraform apply` de `infra/apps` y espera a que los servicios queden estables. Muestra las URLs y los servicios pendientes | 5–10 min |

### Por partes

| Comando | Cuándo usarlo |
| --- | --- |
| `make infra-plataforma` | Cambios en red, datos, colas, Cognito, API Gateway, WAF o el catálogo (`infra/platform/catalog.tf`) |
| `make infra-bases` | Después de agregar al catálogo un servicio con `database = true` (es idempotente) |
| `make infra-imagenes [SERVICES=a,b]` | Publicar imágenes sin desplegarlas |
| `make infra-aplicaciones` | Desplegar lo que ya está en ECR, o aplicar cambios de `apps.tfvars` |
| `make infra-microservicios SERVICES=clientes,adaptador-identidad` | Imagen + despliegue de esos servicios, sin tocar la plataforma |
| `make infra-plan` | Ver los cambios de ambas raíces sin aplicar |
| `make infra-pausar` / `make infra-reanudar` | Llevar todos los servicios a 0 tareas o volver a las réplicas configuradas |
| `make infra-salidas` | URLs de los APIs, portal, Cognito y tablero |
| `make infra-probar` | Prueba rápida del ambiente desplegado (sección 5.1) |

### 5.1 Prueba rápida del ambiente

Sirve para comprobar el ambiente aunque los servicios todavía sean esqueletos: solo usa `GET /health`. El workflow **Infra - Desplegar** la ejecuta al final, salvo que se desmarque `probar`.

Primer despliegue de prueba, solo con lo necesario para recorrer el borde completo (dos tareas Spot, unos 7 USD/mes además de la plataforma; destruir o pausar al terminar):

```bash
make infra-bootstrap                                                # una vez por cuenta
SERVICES=api-socios,simulador-aliados make infra-desplegar ENV=int
make infra-probar ENV=int                                           # SMOKE_ALERTA=1 prueba también el correo
```

| Chequeo | Esperado | Qué valida |
| --- | --- | --- |
| Servicios ECS | Tareas corriendo = deseadas | Imágenes en ECR, healthcheck de la imagen, IAM, secretos, red privada y NAT |
| Destinos del ALB | Al menos 1 saludable por servicio de acceso | ALB interno, grupos de seguridad, `GET /health` |
| Canales sin token o con token inválido | 401 con `X-Correlation-Id` | WAF, API Gateway, autorizador de Cognito, respuestas del borde |
| Socio de prueba: token `client_credentials` | Cognito entrega el token | Pool de socios, dominio y servidor de recursos |
| Socio con token, sin API key | 401/403 | Plan de uso por API key (HU-W02) |
| Socio con token y API key | 200 desde `api-socios` | Recorrido completo: WAF → API Gateway → VPC Link v2 → ALB → ECS |
| `SMOKE_ALERTA=1` | Llega el correo de prueba | Tópico de alertas y suscripción confirmada (HU-W28) |

- **Socios de prueba:** la prueba usa `socio-a`, definido en `infra/envs/int/platform.tfvars`.
- **Dominio de Cognito:** puede tardar unos minutos en responder después del primer `infra-plataforma`. Si falla solo el token, repetir.
- **Más servicios:** para cubrir los demás, agregarlos a `SERVICES`. Sin `SERVICES`, se construyen los 10 del catálogo.

### Imágenes de los microservicios

- **Convención:** `services/<servicio>/Dockerfile`, construido con contexto en la raíz del repo para que la imagen incluya `libs/`. El nombre de la carpeta debe coincidir con el del catálogo (`bff-web`, `clientes`, `adaptador-identidad`…). Con `SRC_DIR=otra/carpeta` se usa otra raíz.
- **Contrato de la imagen:** escucha en el puerto 8080, expone `GET /health` e incluye `/app/healthcheck`. Las variables de entorno que recibe están en PLATAFORMA.md.
- **Etiqueta:** es el commit (`git rev-parse --short=12 HEAD`). Si `services/` o `libs/` tienen cambios sin commit, se usa `<commit>-dirty-<fecha>`. Los repositorios son inmutables: si la etiqueta ya existe, no se reconstruye.

### Flujo típico de una historia

```bash
# 1. Implementar el servicio en services/clientes/ (Dockerfile incluido) y hacer commit.
# 2. Publicar y desplegar solo ese servicio:
make infra-microservicios SERVICES=clientes
# 3. Revisar el tablero y los logs:
make infra-salidas
```

## 6. Destruir el ambiente

```bash
make infra-destruir ENV=int                 # pide escribir el nombre del ambiente
AUTO_APPROVE=1 make infra-destruir ENV=int  # sin pregunta (CI)
```

| Orden | Qué pasa |
| --- | --- |
| 1 | Destruye `apps` (servicios, IAM, alarmas, tablero). |
| 2 | Vacía el bucket de auditoría saltando la retención GOVERNANCE de Object Lock. Con `audit_lock_mode = "COMPLIANCE"`, AWS no permite borrar antes del vencimiento y la destrucción de ese bucket falla. |
| 3 | Borra todas las imágenes de los repositorios ECR del ambiente, así la destrucción no depende de que el estado tenga `force_delete = true`. |
| 4 | Destruye `platform`: RDS sin snapshot final, repositorios ECR, Cognito, colas, NAT, ALB, API Gateway, WAF y VPC. |

- **Idempotencia:** si una raíz ya no tiene recursos, se omite.
- **Lo que se conserva:** el bucket de estado y el rol de GitHub (bootstrap).
- **Recrear:** volver a ejecutar `make infra-desplegar`; las imágenes se reconstruyen porque ECR se borró con el ambiente. Después hay que crear de nuevo el primer administrador y publicar el portal (sección 6.1).
- **Datos:** la base de datos, los usuarios de Cognito y la auditoría **se pierden**. Exportar lo necesario antes de destruir.

### 6.1 Volver a levantar el ambiente después de destruirlo

El destroy borra el pool de Cognito del back-office, por lo que no queda ningún usuario que pueda entrar al portal ni crear otros, y el bucket y la distribución de CloudFront del portal, que quedan vacíos y con otra URL. El bootstrap y las variables de GitHub no cambian.

```bash
# 1. Backend: plataforma, bases, imágenes y servicios (sección 5)
make infra-desplegar ENV=int
make infra-probar ENV=int

# 2. Primer administrador del back-office: el pool es nuevo y está vacío
EMAIL=ana@solventa.co NOMBRE="Ana Pérez" make infra-usuario-admin ENV=int

# 3. Portal: el bucket nuevo está vacío (en proyecto-final-2-frontend)
make desplegar ENV=int

# 4. URL del portal (cambia con cada CloudFront nuevo)
make infra-salidas ENV=int
```

5. **Primer ingreso:** con la contraseña temporal que llega al correo, entrar a `<portal>/ingresar`, definir la contraseña y registrar la aplicación de autenticación con el QR (detalle en la sección 7.1).
   - **Antes de escanear:** borrar de la aplicación de autenticación la entrada "Solventa" del ambiente anterior. El pool es nuevo, así que el código anterior ya no sirve y confunde al elegir la entrada.
6. **Demás usuarios:** crearlos desde el portal (**Administración › Usuarios**). También funciona `make infra-usuario-admin` con `GRUPOS=`.

Si el correo no llega, volver a ejecutar el paso 2: mientras el usuario no haya hecho su primer ingreso, reenvía otra contraseña temporal.

## 7. GitHub Actions

### Configuración (una vez)

1. Ejecutar `make infra-bootstrap` localmente (sección 4).
2. En GitHub, en **Settings → Secrets and variables → Actions → Variables**, crear:
   - `AWS_ROLE_ARN`: el valor que imprimió el bootstrap.
   - `AWS_REGION`: por ejemplo `us-east-1`.
   Deben ser variables **del repositorio**, no del environment: los jobs de plan no usan el environment.
3. Opcional: en **Settings → Environments**, crear el ambiente (`int`, `qa`…) con *required reviewers* para exigir aprobación antes de desplegar o destruir. En **Infra - Desplegar** solo los jobs que aplican usan `environment: <ambiente>`, así que la aprobación se pide cuando el plan ya está en el resumen de la ejecución.
4. Los workflows `workflow_dispatch` solo aparecen en la pestaña **Actions** cuando están en la rama por defecto (`main`).

5. En el repo **proyecto-final-2-frontend**, crear las variables `AWS_WEB_ROLE_ARN` (salida `github_web_role_arn`) y `AWS_REGION`, y el environment `int` (opcionalmente con *required reviewers*). El workflow **CD web** publica el portal en cada push a `main` (ambiente `int`) o a mano en otro ambiente.

No se guardan llaves de AWS en GitHub: los workflows obtienen credenciales temporales por OIDC.

### Workflows

| Workflow | Disparador | Qué hace |
| --- | --- | --- |
| **Infra - Desplegar** (`infra-desplegar.yml`) | Manual | Alcance `completo`, `plataforma`, `bases`, `aplicaciones` o `microservicios` (con la lista `servicios`). Por cada raíz (`platform`, `apps`) calcula el plan, lo publica en el resumen, pide la aprobación del environment y aplica ese plan guardado; sin cambios no pide aprobación. En `completo` hay dos aprobaciones (plataforma y aplicaciones) y `construir_imagenes` decide si se publican imágenes antes del plan de `apps`. Deja las URLs en el resumen de la ejecución. |
| **Infra - Imágenes a ECR** (`infra-imagenes.yml`) | Manual | Build y push de los servicios indicados (o todos los que tienen Dockerfile), sin desplegar. |
| **Infra - Destruir** (`infra-destruir.yml`) | Manual | Exige escribir `destruir <ambiente>`. Ejecuta `make infra-destruir`. |
| **Infra - Validar** (`infra-validar.yml`) | Pull requests que tocan `infra/` o los scripts | `fmt`, `validate` y `terraform test` de las tres raíces, más `shellcheck`. No usa AWS. |

Los workflows de un mismo ambiente comparten un grupo de concurrencia, así que no se ejecutan dos a la vez. Además, el estado remoto tiene bloqueo.

### Portal web (repo proyecto-final-2-frontend)

`infra/platform` crea el bucket privado, CloudFront y el parámetro SSM `/solventa/<ambiente>/web/config` (bucket, distribución, URL de la API de canales, IDs públicos de Cognito y tablero; sin secretos). El repo del portal lo usa para publicar:

```bash
# En proyecto-final-2-frontend, con credenciales del ambiente
make desplegar ENV=int        # compila, genera config.json desde SSM, sube a S3 e invalida CloudFront
make config-local ENV=int     # para correr el portal en local contra la API y el Cognito de int
```

El workflow **CD web** del repo del portal ejecuta lo mismo con el rol `solventa-github-web-deploy`.

## 7.1 Usuarios del back-office

El pool de Cognito del back-office no permite registro abierto y exige MFA (TOTP). El primer administrador se crea desde aquí; los siguientes, desde el portal (**Administración › Usuarios**, grupo `administradores`):

```bash
EMAIL=ana@solventa.co NOMBRE="Ana Pérez" make infra-usuario-admin ENV=int     # grupos administradores,operacion
GRUPOS=operacion EMAIL=luis@solventa.co make infra-usuario-admin ENV=int      # otros grupos
```

Cognito envía al correo una contraseña temporal con el enlace `<portal>/ingresar`. El correo sale de `no-reply@verificationemail.com` (revisar spam; el envío por defecto de Cognito permite unos 50 al día). Si no llega, volver a ejecutar el mismo comando: mientras el usuario no haya hecho su primer ingreso, se genera y reenvía otra contraseña temporal. En el primer ingreso la persona define su contraseña (12+ caracteres con mayúsculas, minúsculas, números y símbolos) y registra su aplicación de autenticación con el código QR. Grupos: `administradores` (crea usuarios), `operacion` (tablero y trazas) y `administradores-socios` (socios, credenciales y cuotas).

## 8. Problemas frecuentes

| Síntoma | Causa y solución |
| --- | --- |
| `No existe el bucket de estado` | Falta `make infra-bootstrap` en esta cuenta o región. |
| `Falta la variable AWS_ROLE_ARN` (Actions) | Configurar las variables del repositorio (sección 7). |
| `Not authorized to perform sts:AssumeRoleWithWebIdentity` (Actions) | AWS rechazó el token de GitHub. El paso *Diagnóstico OIDC* imprime el rol pedido y los claims del token. Revisar: (1) que `AWS_ROLE_ARN` sea exactamente el `github_role_arn` del bootstrap y de la misma cuenta; (2) que `sub` empiece por `repo:` + el `github_repository` del bootstrap (`owner@<id>/repo@<id>`); si el repositorio se renombra, transfiere o recrea, actualizar esa variable; (3) que el rol y el proveedor OIDC existan (`aws iam get-role --role-name solventa-github-deploy`). Corregir y volver a ejecutar `make infra-bootstrap`. |
| `Error acquiring the state lock` | Otro despliegue del mismo ambiente está en curso. Si quedó colgado: `terraform -chdir=infra/<raíz> force-unlock <id>` tras `tf_init`. |
| `db-bootstrap falló` | Revisar el log group `/ecs/solventa-<ambiente>/db-bootstrap`. Si RDS aún no está disponible, repetir `make infra-bases`. |
| Un servicio no queda estable | `aws ecs describe-services` y el log group `/ecs/solventa-<ambiente>/<servicio>`. Si el health check falla, el *circuit breaker* revierte el despliegue. |
| Una tarea no alcanza a otro servicio | El invocado se creó después que el llamador y la autodetección no aplicó. Ejecutar `aws ecs update-service --force-new-deployment` sobre el llamador. |
| `aws_api_gateway_account` en conflicto | La configuración de logs de API Gateway es única por región y cuenta. Si otra pila la administra, quitar ese recurso de una de las dos. |
| No se puede destruir la auditoría | `audit_lock_mode = "COMPLIANCE"`: esperar el vencimiento de la retención. |

## 9. Validación sin AWS

```bash
make infra-validar                      # fmt, validate y pruebas con proveedores simulados
shellcheck -x -P scripts/plataforma scripts/plataforma/*.sh
```
