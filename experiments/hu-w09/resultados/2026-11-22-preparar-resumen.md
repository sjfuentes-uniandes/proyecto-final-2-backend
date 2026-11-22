# HU-W09 · preparar (2026-11-22)

Reconstruido del log del job: el resumen del job (`GITHUB_STEP_SUMMARY`) no viene en el archivo de logs que descarga GitHub. Cuenta, correos y valores enmascarados, redactados.

| Dato | Valor |
| --- | --- |
| Workflow | `E2E (N3)` (`.github/workflows/e2e.yml`), `ambiente = exp` |
| Run | `121921951728` |
| Commit probado | `274771d52fd120f25bd68ed26aba57430209feee` |
| Filtro | `hu_w09_preparar` |
| Inicio del paso (UTC) | `2026-11-22T17:59:34.2396059Z` |
| Resultado | 1 passed, 205 deselected in 51.13s |
| Socio | SOC-000001 |
| Plan efectivo de la API key | experimento |
| Movimiento de plan | movida de estandar a experimento (delete_usage_plan_key y create_usage_plan_key) |
| Suscripción de alertas (decisión 11) | 1 PendingConfirmation (email) |
| URL del ALB | http://internal-solventa-exp-access-1524001600.us-east-1.elb.amazonaws.com:8083 — DNS de describe_load_balancers del ALB solventa-exp-access (infra/platform/edge.tf:43); puerto 8083, el listener_port de api-socios en la salida platform.catalog (infra/platform/catalog.tf:36) |
| Tarea del alta | a5993aa72531490bba75340c95df1357 |
| Cuota Fargate On-Demand vCPU resource count (L-3032A538) | 64 vCPU (aplicada) |
| Cuota Fargate Spot vCPU resource count (L-36FBB829) | 64 vCPU (aplicada) |
| vCPU estimadas (cadena y k6) | 4.5 ahora · 9.5 con la cadena al máximo (k6: 2) |
| Holgura On-Demand | alcanza: 9.5 de 64 vCPU |

Log del paso «Pruebas E2E»: `2026-11-22-preparar.log`.
