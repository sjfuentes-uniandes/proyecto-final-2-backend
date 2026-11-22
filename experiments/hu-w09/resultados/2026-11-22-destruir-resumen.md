# HU-W09 · destruir (2026-11-22)

Reconstruido del archivo de logs del run. El resumen del job (`GITHUB_STEP_SUMMARY`) no viene en ese archivo. La cuenta, los correos y los valores enmascarados están redactados.

| Dato | Valor |
| --- | --- |
| Workflow | `Infra - Destruir` (`.github/workflows/infra-destruir.yml`), `ambiente = exp`, confirmación `destruir exp` |
| Run | sin número: el archivo se descargó renombrado |
| Commit | `274771d52fd120f25bd68ed26aba57430209feee` (#93) |
| Inicio y fin (UTC) | 18:50:38 a 19:07:55 |
| 1/5 Aplicaciones | `Destroy complete! Resources: 123 destroyed.` (19:01:39) |
| 2/5 y 3/5 | Buckets de auditoría, pólizas y evidencias vaciados; los 19 repositorios ECR de `solventa-exp`, vacíos |
| 4/5 API keys de socios | Una eliminada: la del socio de la carga (redactada) |
| 5/5 Plataforma | `Destroy complete! Resources: 342 destroyed.` (19:07:54) |
| Final | «Ambiente exp destruido. Se conservan el bucket de estado y el rol de GitHub (infra/bootstrap).» |

Extractos: `2026-11-22-destruir.log`.
