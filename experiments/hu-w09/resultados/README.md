# HU-W09 · Resultados de la corrida del 2026-11-22

Corrida en EXP, con las pruebas desde `main` @ `274771d` (#93). La conclusión está en `../informe.md`, y la configuración, en `../manifiesto.md` (§3).

| Archivo | Qué es |
| --- | --- |
| `2026-11-22-preparar.log`, `-lanzar.log` y `-recoger.log` | El paso «Pruebas E2E» del job de `E2E (N3)` de cada prueba. Runs `121921951728` (preparar) y `121922925722` (lanzar); el de `recoger` se descargó sin número |
| `2026-11-22-desplegar.log` y `-destruir.log` | Extractos de `Infra - Desplegar` (desde `0f7045e`) y de `Infra - Destruir` (desde `274771d`): el checkout, los hitos, los resúmenes de Terraform, la prueba rápida completa, el vaciado de ECR y la API key del socio. Sus archivos completos pasan de 1 MB por el detalle de Terraform |
| `2026-11-22-<paso>-resumen.md` | El resumen de cada paso, reconstruido del log: el `GITHUB_STEP_SUMMARY` no viene en el archivo de logs de GitHub |
| `2026-11-22-recogida.json` | La línea `HU-W09-RECOGIDA` de `recoger`, con formato. Es la fuente de las tablas y las gráficas del informe |
| `2026-11-22-latencia-p95.svg` y `-latencia-p99.svg` | Latencia por etapa: k6, `socio.cotizar` y `cotizacion` |
| `2026-11-22-errores.svg` | Resultado de las solicitudes de k6 por etapa |
| `2026-11-22-tareas.svg` | `api-socios` en el tiempo: `desiredCount` y `SampleCount` de CPU |


**Cómo se prepararon:**
- **Limpieza:** de cada log de `E2E (N3)`, solo el paso «Pruebas E2E»; de desplegar y destruir, los extractos. Sin códigos ANSI ni BOM y con finales de línea LF.
- **Redacción:**
  - el ID de la cuenta en los ARN y en las URL de ECR, como `<cuenta>`;
  - los correos, como `<correo>` (no apareció ninguno);
  - los ID de API Gateway de la prueba rápida, como `<api-id>`;
  - los dos valores que las pruebas enmascaran con `::add-mask::` (el `clientId` del socio en `preparar` y el id de su API key en `lanzar`, que también aparece en `Infra - Destruir`), como `***`. El log descargado de GitHub los trae en claro en la línea de la orden (hallazgo H-5 del informe, §9).
- **Revisión:** no hay `Bearer`, `x-api-key`, firmas `X-Amz-` ni `ClientSecret` en ningún archivo. Las credenciales del job aparecen como `***`.
- **Gráficas:** las generó un script de una sola vez a partir del JSON. El script no se versiona: no es parte del protocolo.
