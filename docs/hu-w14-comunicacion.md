# HU-W14 · Comunicación de aseguramiento

Issue: [sjfuentes-uniandes/proyecto-final#56](https://github.com/sjfuentes-uniandes/proyecto-final/issues/56). Plan: `docs/sprint_2/plan_maestro_sprint_2.md` §2.2 (fuera de este repositorio). Parte de lo que dejó HU-W13 ([hu-w13-suscripcion.md](hu-w13-suscripcion.md)): la decisión inmutable, sus reglas evaluadas y el filtrado de las reglas `RESERVADA` por rol.

> Objetivo: que el portal comunique cada decisión en lenguaje comprensible, sin exponer reglas internas, y decida qué mostrar **solo** con el bloque `comunicacion`, sin reinterpretar `reglasEvaluadas`.

## 1. Diseño en una página

| Tema | Decisión | Por qué |
| --- | --- | --- |
| Dónde se calcula | `polizas/domain/suscripcion/comunicacion.py`: función pura `comunicar(decision, alternativas, ahora, compromiso_revision_horas)`, dirigida por una tabla | Determinista y fácil de probar fila por fila; sin E/S ni reloj propio |
| Cuándo | **Al leer**, en `POST /v1/suscripciones` (también en la repetición idempotente) y en `GET /v1/suscripciones/{decisionId}`, con el caso de uso `ComunicarDecision`. No se persiste | Se deriva de datos inmutables (la decisión y su instantánea de la oferta) y del reloj: una aprobada deja de admitir contratación cuando vence, sin escribir nada |
| Contrato | Campo **opcional** `comunicacion` (`ComunicacionDto`) en `DecisionSuscripcion` de `polizas.yaml`, copiado en `bff-web.yaml` | Cambio aditivo: un cliente de la versión W13 sigue validando |
| Alternativas | Las de la **versión de reglas con que se decidió** (`por_version`, caché en memoria de 24 h, DS2-18), solo en un rechazo | Coherentes con la decisión aunque catalogo publique otra versión |
| Si catalogo falla | Falla **abierta** solo para las alternativas: `alternativas = []`, resultado `error`/`timeout` en `suscripcion.dependencia.catalogo.reglas` y el aviso «alternativas no disponibles» en el log | La decisión ya está registrada; las alternativas son informativas |
| Qué no lleva | Ni códigos de regla (`SUS-NNN`) ni valores del caso. Todos sus campos son conjuntos cerrados, fechas, booleanos o números; `productoId` sigue el patrón de producto (`^[a-z0-9-]{1,64}$`) | DS2-07 y la regla del issue: «no expone reglas internas sensibles» |

### 1.1 Tabla de comunicación

| Resultado | Oferta | `condicion` | `admiteContratacion` | `siguientePaso` | Datos adicionales |
| --- | --- | --- | --- | --- | --- |
| `APROBADA` | vigente (`ahora < vigenteHasta`) | `APROBADA_SIN_AJUSTES` | `true` | `ACEPTAR_OFERTA` | `vigenteHasta` de `condiciones` |
| `APROBADA` | vencida (`ahora ≥ vigenteHasta`) | `APROBADA_SIN_AJUSTES` | `false` | `COTIZAR_NUEVAMENTE` | `vigenteHasta` |
| `RECHAZADA` | cualquiera | `NO_ASEGURABLE` | `false` | `COTIZAR_ALTERNATIVA` | `motivoCliente` de la regla determinante (`NO_ELEGIBLE` si es `RESERVADA`) y `alternativas` |
| `REVISION_REQUERIDA` | cualquiera | `EN_REVISION` | `false` | `ESPERAR_REVISION` | `plazoRespuestaHoras` = `compromiso_revision_horas` de polizas (8 h, R-04) y `motivoCliente` según R-12 |

- **R-12 (revisión):** `motivoCliente` es el de la primera regla `EXPLICABLE`, por `orden`, que no se cumple o no es evaluable. Si solo fallan reglas `RESERVADA` (por ejemplo, listas no consultadas), es `null` y el portal usa un texto genérico. **Una revisión nunca comunica `NO_ELEGIBLE`.**
- Campos vacíos: `vigenteHasta` solo en la aprobada; `alternativas` solo en el rechazo; `plazoRespuestaHoras` solo en la revisión. Ni el rechazo ni la revisión llevan nada que sugiera cobertura (AC3).
- Defensa: un rechazo cuya regla determinante no aparezca entre las evaluadas comunica `NO_ELEGIBLE`; en revisión se salta una regla `EXPLICABLE` que declare `NO_ELEGIBLE`.

### 1.2 Ejemplo (S02, rechazo por edad)

```json
"comunicacion": {
  "condicion": "NO_ASEGURABLE",
  "admiteContratacion": false,
  "siguientePaso": "COTIZAR_ALTERNATIVA",
  "vigenteHasta": null,
  "motivoCliente": "EDAD_FUERA_DE_RANGO",
  "alternativas": [
    {"productoId": "vida-senior", "descriptor": {"codigo": "EDAD_MAXIMA", "valor": "80"}},
    {"productoId": "proteccion-pagos", "descriptor": {"codigo": "SIN_LIMITE_EDAD", "valor": null}},
    {"productoId": "auxilio-funerario", "descriptor": {"codigo": "EDAD_MAXIMA", "valor": "85"}}
  ],
  "plazoRespuestaHoras": null
}
```

## 2. Textos permitidos por valor

El backend solo entrega códigos; los textos viven en el catálogo del portal (`features/contratacion/contratacion.i18n.ts`, es-CO y en-US, DS2-13). Estos son los textos de referencia para ese catálogo (W14-F03). Reglas: frases cortas, sin jerga, sin códigos de regla, sin cifras de las reglas (pueden cambiar con la versión) y sin prometer cobertura fuera de `APROBADA_SIN_AJUSTES` vigente.

| Código | es-CO | en-US |
| --- | --- | --- |
| `condicion` `APROBADA_SIN_AJUSTES` | Aprobada sin ajustes | Approved with no changes |
| `condicion` `NO_ASEGURABLE` | No podemos asegurar esta solicitud con este producto | We can't insure this request with this product |
| `condicion` `EN_REVISION` | En revisión | Under review |
| `motivoCliente` `EDAD_FUERA_DE_RANGO` | Tu edad está fuera del rango que cubre este producto. No es una valoración de tu situación financiera, y otros productos tienen límites de edad distintos | Your age is outside the range this product covers. This is not an assessment of your finances, and other products have different age limits |
| `motivoCliente` `PLAZO_FUERA_DE_RANGO` | El plazo de tu crédito es mayor que el que cubre este producto | Your loan term is longer than this product covers |
| `motivoCliente` `NO_ELEGIBLE` | Por ahora no podemos ofrecerte este producto. Tu asesor puede mostrarte otras opciones | We can't offer you this product right now. Your advisor can show you other options |
| `motivoCliente` `SUMA_REQUIERE_REVISION` | El valor que quieres asegurar necesita la revisión de un suscriptor | The amount you want to insure needs to be reviewed by an underwriter |
| `motivoCliente` `PERFIL_REQUIERE_REVISION` | Necesitamos revisar con más detalle la información de tu perfil | We need to take a closer look at your profile information |
| `motivoCliente` `null` (revisión) | Un suscriptor revisará tu solicitud | An underwriter will review your request |
| `siguientePaso` `ACEPTAR_OFERTA` | Puedes continuar a la aceptación. La oferta está vigente hasta el {fecha} | You can continue to acceptance. The offer is valid until {date} |
| `siguientePaso` `COTIZAR_NUEVAMENTE` | La oferta venció el {fecha}. Para continuar, haz una nueva cotización | The offer expired on {date}. To continue, request a new quote |
| `siguientePaso` `COTIZAR_ALTERNATIVA` | Puedes cotizar uno de los productos alternativos | You can get a quote for one of the alternative products |
| `siguientePaso` `ESPERAR_REVISION` | Aún no tienes cobertura. Un suscriptor revisará tu solicitud y te responderá en un plazo de hasta {horas} horas | You are not covered yet. An underwriter will review your request and reply within {hours} hours |
| descriptor `EDAD_MAXIMA` | hasta {valor} años | up to age {value} |
| descriptor `SIN_LIMITE_EDAD` | sin límite de edad | no age limit |
| producto `vida-senior` | Vida sénior | Senior life |
| producto `proteccion-pagos` | Protección de pagos | Payment protection |
| producto `auxilio-funerario` | Auxilio funerario | Funeral assistance |

`NO_ELEGIBLE` no menciona listas, verificaciones ni antecedentes. La fecha va en la zona del usuario; el contrato la entrega en UTC.

## 3. Recorrido local

### 3.1 Levantar el ambiente

Como en [hu-w13-suscripcion.md §2.1](hu-w13-suscripcion.md#21-levantar-el-ambiente): `make dev-up` (con `docker compose down -v` una vez si la base `polizas` no existe). Si el 8080 está ocupado, publica `bff-web` en otro puerto con un *override* que no se versiona.

### 3.2 Los tres resultados

`scripts/dev/recorrido_suscripcion.py` siembra y decide los casos sintéticos. La respuesta del `POST` ya trae `comunicacion`; para leerla después, como asesor:

```bash
uv run python scripts/dev/recorrido_suscripcion.py S01 S02 S06
```

```bash
curl -s localhost:8080/v1/suscripciones/<decisionId> -H 'X-Authenticated-Sub: sub-asesor' -H 'X-Authenticated-Groups: [asesores]' | jq .comunicacion
```

Esperado: S01 `APROBADA_SIN_AJUSTES` con `admiteContratacion = true`; S02 `NO_ASEGURABLE` con `EDAD_FUERA_DE_RANGO` y tres alternativas; S04 `NO_ASEGURABLE` con `NO_ELEGIBLE`; S06 `EN_REVISION` con `SUMA_REQUIERE_REVISION` y `plazoRespuestaHoras = 8`.

### 3.3 Aprobada vencida

La oferta vale 7 días (reglas de prima `2026.10.1`, HU-W04), así que en local no vence sola. Para ver la comunicación vencida de una decisión real **sin escribir en la base ni cambiar código**, levanta una segunda instancia de `polizas` (solo para leer) con el reloj adelantado, sobre la misma base local:

```bash
SOLVENTA_DATABASE_URL=postgresql://solventa:solventa@localhost:5432/polizas \
SOLVENTA_COTIZACION_URL=http://localhost:8085 SOLVENTA_CATALOGO_URL=http://localhost:8084 \
uv run python -c "
from datetime import timedelta
import uvicorn
from polizas.adapters.outbound import reloj
real = reloj.RelojSistema.ahora
reloj.RelojSistema.ahora = lambda self: real(self) + timedelta(days=8)
from polizas.main import crear_app
uvicorn.run(crear_app(), host='127.0.0.1', port=18088)"
```

```bash
curl -s localhost:18088/v1/suscripciones/<decisionId de S01> -H 'X-Actor-Id: sub-asesor' -H 'X-Actor-Rol: ASESOR' | jq .comunicacion
```

Esperado: `admiteContratacion = false`, `siguientePaso = COTIZAR_NUEVAMENTE` y el mismo `vigenteHasta`.

### 3.4 Resultado del recorrido

2026-10-23, local, `bff-web` → `polizas` → `cotizacion`/`catalogo` con el código de la rama `feat/HU-W14-comunicacion`. Las dependencias que W04 y W14 no cambian (`clientes`, adaptadores, simulador, Postgres y LocalStack) corrieron en `compose` y `catalogo`, `cotizacion`, `polizas` y `bff-web` con `uv run uvicorn`, porque Docker no pudo descargar las imágenes base para reconstruirlos.

| Caso | Decisión | `comunicacion` (POST y GET por `bff-web`, como asesor) |
| --- | --- | --- |
| S01 aprobada (C01) | 201 `APROBADA` | `APROBADA_SIN_AJUSTES`, `admiteContratacion = true`, `ACEPTAR_OFERTA`, `vigenteHasta` = la de `condiciones` (+7 días) |
| S02 68 años (C04) | 201 `RECHAZADA` (SUS-001) | `NO_ASEGURABLE`, `false`, `COTIZAR_ALTERNATIVA`, `EDAD_FUERA_DE_RANGO`, 3 alternativas |
| S04 listas restrictivas (C14) | 201 `RECHAZADA` (SUS-015, reservada) | `NO_ASEGURABLE`, `NO_ELEGIBLE`, 3 alternativas; SUS-015 llega al asesor sin `valorCaso` |
| S06 suma 620.000.000 (C15) | 201 `REVISION_REQUERIDA` (SUS-004) | `EN_REVISION`, `false`, `ESPERAR_REVISION`, `SUMA_REQUIERE_REVISION`, `plazoRespuestaHoras = 8` |
| S01 con el reloj +8 días (§3.3) | la misma decisión | `APROBADA_SIN_AJUSTES`, `admiteContratacion = false`, `COTIZAR_NUEVAMENTE`, mismo `vigenteHasta` |

En todos los casos el `GET` comunicó lo mismo que el `POST`, ninguna comunicación tuvo un código `SUS-NNN` y cada respuesta trajo `X-Correlation-Id`. Los logs de los cuatro servicios no tienen errores, fechas de nacimiento ni nombres; se midieron `suscripcion.evaluar`, `suscripcion.consultar`, `suscripcion.dependencia.*` (`polizas`) y `suscripcion.solicitar`/`suscripcion.consultar` (`bff-web`). La falla de `catalogo` al leer (alternativas vacías) se prueba en MT-W14-01.

## 4. Pruebas

| Nivel | Qué | Dónde |
| --- | --- | --- |
| N1 `polizas` (UT-W14-01) | Tabla de comunicación: S01–S08 × vigente/vencida, límites de la vigencia (−1 µs, exacto, +1 µs), rechazo por reglas reservadas, R-12, sin alternativas, sin códigos ni valores de reglas, pureza | `services/polizas/tests/unit/suscripcion/test_comunicacion.py` |
| N1 `polizas` | Caso de uso: alternativas de la versión guardada, solo en el rechazo, vencimiento con el reloj, falla abierta medida y registrada sin PII | `test_comunicar.py`, `test_api_comunicacion.py`, `test_adaptadores_http.py` |
| N2 `polizas` (MT-W14-01) | `POST` y `GET` con PostgreSQL efímero, aprobada vencida sin cambios en la base, `catalogo` caído en otra réplica | `services/polizas/tests/module/test_comunicacion_w14.py` |
| Contrato | `polizas.yaml` y `bff-web.yaml` regenerados; DTO de `bff-web` iguales a los de `polizas`; `comunicacion` opcional (un cuerpo de W13 valida) | `tests/contract/test_openapi.py` de `polizas` y `bff-web` |
| N1 `bff-web` (UT-W14-02) | La comunicación pasa tal cual al asesor, sin valores reservados ni códigos de regla; una comunicación fuera del contrato es 503; un campo extra no llega al portal; una decisión sin comunicación sigue pasando | `services/bff-web/tests/unit/test_suscripcion.py` |
