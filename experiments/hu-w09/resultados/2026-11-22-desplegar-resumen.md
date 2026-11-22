# HU-W09 · desplegar (2026-11-22)

Reconstruido del archivo de logs del run. El resumen del job (`GITHUB_STEP_SUMMARY`) no viene en ese archivo. La cuenta, los correos y los ID de API Gateway están redactados.

| Dato | Valor |
| --- | --- |
| Workflow | `Infra - Desplegar` (`.github/workflows/infra-desplegar.yml`), `ambiente = exp`, completo |
| Run | sin número: el archivo se descargó renombrado |
| Commit desplegado | `0f7045e70e0fb2c6a96430b7e0b06099338c3564` (#91), en los seis trabajos |
| Inicio y fin (UTC) | 16:55:11 (`plan-plataforma`) a 17:25:58 (`aplicaciones`) |
| Plataforma | Plan de 342 recursos; `Apply complete! Resources: 342 added`, de 16:56:02 a 17:13:01 |
| Bases | De 17:13:24 a 17:14:18 |
| Imágenes | «Publicando api-socios cotizacion catalogo clientes con etiqueta 0f7045e70e0f (exp)», de 17:15:00 a 17:15:47 |
| Aplicaciones | Plan de 123 recursos; `Apply complete! Resources: 123 added` a las 17:17:39; 4 servicios estables a las 17:20:22 |
| Prueba rápida | 34 OK y 0 fallas. Los 4 servicios con 2 de 2 tareas, `api-socios` con 2 destinos sanos en el ALB, y token y API key hasta `api-socios`. Omitió los servicios sin imagen |

Extractos: `2026-11-22-desplegar.log`.
