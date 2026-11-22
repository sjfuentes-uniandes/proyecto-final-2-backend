# HU-W09 · lanzar (2026-11-22)

Reconstruido del log del job: el resumen del job (`GITHUB_STEP_SUMMARY`) no viene en el archivo de logs que descarga GitHub. Cuenta, correos y valores enmascarados, redactados.

| Dato | Valor |
| --- | --- |
| Workflow | `E2E (N3)` (`.github/workflows/e2e.yml`), `ambiente = exp` |
| Run | `121922925722` |
| Commit probado | `274771d52fd120f25bd68ed26aba57430209feee` |
| Filtro | `hu_w09_lanzar` |
| Inicio del paso (UTC) | `2026-11-22T18:03:19.7010944Z` |
| Resultado | 1 passed, 205 deselected in 40.50s |
| Tarea k6 | arn:aws:ecs:us-east-1:<cuenta>:task/solventa-exp/e6a6ae81e7a44869a824ea03509a9209 |
| Definición | arn:aws:ecs:us-east-1:<cuenta>:task-definition/solventa-exp-carga:2 |
| Estado | RUNNING |
| t0 (UTC) | 2026-11-22T18:03:48+00:00 (HU-W09-INICIO) |
| Recoger desde (UTC) | 2026-11-22T18:43:48+00:00 |
| Tramos | alb, apigw |
| Socio | SOC-000001 |
| Plan efectivo de la API key | experimento |
| Tramo gw | tramo gw en experimento (decisión 2): APIGW_TASA por omisión de carga.js, 6.000/min (el 60 % del límite del WAF por IP) |
| URL del ALB | http://internal-solventa-exp-access-1524001600.us-east-1.elb.amazonaws.com:8083 — DNS de describe_load_balancers del ALB solventa-exp-access (infra/platform/edge.tf:43); puerto 8083, el listener_port de api-socios en la salida platform.catalog (infra/platform/catalog.tf:36) |

Log del paso «Pruebas E2E»: `2026-11-22-lanzar.log`.
