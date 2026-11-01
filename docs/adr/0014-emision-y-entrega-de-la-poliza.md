# 0014. Emisión y entrega de la póliza

- Estado: propuesto
- Fecha: 2026-10-29 · Decide: por ratificar en la revisión de la PR del backend de HU-W17 (mismo procedimiento que las ADR 0012 y 0013) · Historias/ASR: HU-W17; relación con HU-M11 y HU-W18; recorrido R3
- Fuente: `docs/exploracion-hu-w17.md` (backend `feat/hu-w17/emision-poliza` @ `6551d3e`)

## Contexto

- La ADR 0013 fija el orden aceptación → recaudo → firma de Solventa y emisión, y difiere la firma del cliente y la compensación.
- `polizas` ya consume `polizas-recaudos` (HU-W16) en el modo (a) del inbox, con una parada pensada para transacciones de milisegundos.
- La infraestructura, que no se cambia, ya declara lo que necesita la emisión:
  - el bucket de pólizas, cifrado y versionado, sin Object Lock, en el que solo escribe `polizas` y leen `polizas` y `consulta`;
  - la llamada `polizas → adaptador-firma`, cuyo secreto es `aliados/firma`, con un timeout de 5000 ms;
  - las colas `avisos` (`PolizaEmitida`) y `consulta-proyeccion` (prefijo `Poliza`), con DLQ;
  - la meta `emision` p95 de 1500 ms.
- `avisos` (HU-M11) y `consulta` (HU-W18) son de otro dueño y todavía son esqueletos: `avisos` no publica eventos de vuelta.
- El ramo del catálogo es el texto `VIDA_HIPOTECARIO`: no hay código numérico. `polizas` no conoce ni el nombre del titular ni el del canal.

## Decisión

1. **Disparo.**
   - El primer `RecaudoConfirmado` de una aceptación conocida crea, en la misma transacción que la proyección del recaudo, **una emisión `EMITIENDO`** (una por aceptación).
   - Una **tarea emisora** de `polizas`, encendida por defecto, la procesa: PDF → subida al bucket → firma de la huella por `adaptador-firma` → `ACTIVA` con su número y `PolizaEmitida` v1 en el outbox, en una sola transacción.
   - Antes de procesar una póliza, la tarea la **reserva** (60 s), para que nunca haya dos subidas a la vez de la misma clave.
   - Un fallo deja la emisión `EMITIENDO`; la misma tarea la reintenta, con puntos de control persistidos para no repetir la subida ni la firma.
   - Pasada una hora, se mide como error. **Sin compensación.**
2. **Número:** `100-<año>-<6 dígitos>`, con una secuencia global, donde el año es el año en que se registra la emisión (America/Bogota). **`100` es una convención de Solventa para el producto `vida-hipotecario`**, junto con su nombre de ramo, «Vida hipotecario», en una tabla de constantes de `polizas`. No es un dato del catálogo.
   - El número **se reserva al crear la emisión**, porque el PDF lo necesita antes de `ACTIVA`.
   - Se acepta un hueco en la secuencia.
3. **Vigencia:** desde las 00:00 (America/Bogota) del día siguiente a la aceptación y hasta el día anterior a su aniversario. Un año, sin depender del plazo del crédito.
4. **Documento:**
   - un PDF determinista (`reportlab`, `invariant=1`), con los datos de la decisión, la aceptación y el recaudo;
   - la fecha del documento, y con ella `emitidaEn`, es la de **registro de la emisión** (`creada_en`). Así el PDF es idéntico entre reintentos;
   - no se define `SOURCE_DATE_EPOCH` en el entorno: `reportlab` la usa y cambiaría el `/ID` del archivo;
   - el titular y el canal van solo como identificadores: el PDF no incluye nombres;
   - se guarda en el bucket versionado, con el cifrado exigido, y se entrega con una URL prefirmada por `versionId`, con vencimiento corto, que genera `polizas`.
5. **Firma y verificación:**
   - se firma la huella sha256 del PDF, no el PDF;
   - `firmaId` y `selloTiempo` se guardan en la base y viajan en `PolizaEmitida`;
   - «verificable» significa que `polizas` confirma que un número y una huella coinciden con lo firmado. Basta con el versionado; sin Object Lock.
6. **`PolizaEmitida` v1:** referencia estable al documento (clave, `versionId` y huella) y a la firma, más los identificadores (póliza, número, cotización, `claveRecaudo` y cliente), el producto, el ramo, las coberturas, la vigencia, la prima, su periodicidad, la suma y la moneda. Sin nombres ni datos de contacto.
   - Las coberturas salen de **la instantánea de la oferta aceptada, no del catálogo vigente**: lo pidió el dueño de `consulta`, que no puede llamar a `catalogo`.
   - `occurredAt` = `emitidaEn`.
   - **Su esquema lo revisa el dueño de `avisos` y `consulta` antes de construir el productor.**
7. **AC3 de HU-W17:** W17 garantiza que reintentar nunca emite otra póliza y que el evento apunta siempre al mismo documento. Reintentar el aviso, deduplicarlo por dispositivo y manejar los tokens es de HU-M11.
8. **Pantallas** (portal, grupo `asesores`):
   - solo pasos reales, con su hora: aceptada → recaudada → documento firmado por Solventa → emitida;
   - el nombre del **titular** se muestra cuando la póliza está `ACTIVA`:
     - lo compone `bff-web` con `nombres_de_clientes`, sobre la lectura de `clientes` para `ASESOR`, que expone `nombreCompleto` desde `75e8e05`. `polizas` entrega `clienteId` en `GET …/poliza`;
     - si `clientes` falla, el nombre se omite, sin un 503;
   - **el nombre del canal no se muestra:** no hay una ruta de socios para `asesores`.

## Diferencias con los mockups

| Mockup | Qué muestra | Qué se implementa |
|---|---|---|
| W17-1 | Firma de las condiciones por el cliente, antes del recaudo | Nada (ADR 0013) |
| W17-2 | «Condiciones firmadas» antes de «Prima recaudada» | «Documento firmado por Solventa», **después** del recaudo |
| W17-2 y W17-4 | «Aviso al cliente / aviso enviado por push» y «El cliente recibió el aviso en su aplicación móvil» | No se muestra: `polizas` no puede saber si el aviso llegó |
| W17-2 y W17-4 | «Conciliación del movimiento · programada al cierre del día» | No se muestra: la conciliación de `dinero` corre cada pocos minutos y no al cierre |
| W17-2 y W17-4 | Nota de compensación, «Ver un caso compensado» | No se muestra (W17-3, diferido) |
| W17-2 y W17-4 | «Presupuesto del recorrido 1,50 s», «Total de la emisión» | No se muestra: ningún tramo medible corresponde a esas cifras. La meta se vigila con la alarma `emision` |
| W17-3 | Emisión fallida y compensada, con devolución automática | Diferido. Un fallo deja la emisión `EMITIENDO` y se reintenta |
| W17-4 | Nombre del titular y del canal | Titular: sí, en pantalla con la póliza activa (no en el PDF). Canal: no se muestra, por falta de una ruta de socios para `asesores` |
| W17-4 | Número `100-2026-004817` y «Ramo: Vida hipotecario» | Sí, con `100` como convención (decisión 2) |

## Alternativas descartadas

- **Emitir dentro del consumidor de `polizas-recaudos`** (modo b): una firma de hasta 5 s por mensaje rompe el presupuesto de parada del consumidor. Además, un pendiente cuyo mensaje ya se borró quedaría sin dueño.
- **Firmar el PDF por dentro** (PAdES): no hay librería en el repo y cambia la huella que se firma.
- **Leer el ramo de `catalogo`:** devuelve la versión vigente y no la de la oferta, y el ramo es un texto sin código.
- **Que `polizas` llame a `clientes` y a `api-socios` para el PDF:** exige cambiar `polizas.calls` en `infra/`.
- **Object Lock en el bucket de pólizas:** es un cambio de `infra/` y no hace falta para verificar.

## Consecuencias

- **Se gana:**
  - una emisión reintentable sin duplicados y fuera del camino del consumidor;
  - con la reserva, nunca hay dos subidas a la vez de la misma clave. Sin ella, quedaría referenciada una versión no actual del objeto, que el bucket borra a los 90 días (`infra/platform/storage.tf:132-133`);
  - un documento reproducible y verificable;
  - un contrato de evento estable para M11 y W18;
  - todo con la infraestructura existente.
- **Se sacrifica:**
  - el PDF no lleva nombres; la pantalla muestra el titular, pero no el canal;
  - la pantalla no confirma el aviso;
  - no hay compensación: una emisión imposible queda `EMITIENDO`, medida como error;
  - el número usa un código de ramo propio.
- **Hay que cambiar:** con una clave KMS propia (`use_customer_managed_key = true`), `polizas` debería enviar `SSEKMSKeyId`, y `infra/apps/ecs.tf` debería entregarle la clave. En `int` se usa `AES256` y no aplica.
- **Si más adelante se quiere el PDF con nombres o la compensación:** habrá que cambiar `infra/` (`polizas.calls`, y un evento con su cola `polizas → dinero`).
