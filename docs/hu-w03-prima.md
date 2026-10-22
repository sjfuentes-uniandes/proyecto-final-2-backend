# HU-W03 · Cálculo de prima

> Como socio de distribución quisiera que Solventa calcule la prima usando el producto y las reglas vigentes para iniciar una cotización válida. (Issue #44)

Este documento recoge el diseño, la tabla de decisión, las pruebas y la evidencia de la historia. La infraestructura está en [infra/PLATAFORMA.md](../infra/PLATAFORMA.md#hu-w03-cálculo-de-prima) y el humo extendido en [infra/DESPLIEGUE.md](../infra/DESPLIEGUE.md) (sección 5.4).

## 1. Alcance y decisiones

| Tema | Decisión | Fundamento |
| --- | --- | --- |
| Pantallas | Los tres mockups del issue son del **portal del asesor**: "Nueva cotización" (versión del producto, canal, coberturas, "Cotizar sin perfilar"), "Revisa los datos antes de continuar" (límites del producto) y "Calculando tu oferta" (pasos y presupuesto). Se implementan en `bff-web` y en el portal. La pantalla termina en la **prima calculada**; la oferta con vigencia, exclusiones y contrato público del socio es de HU-W04 | Issue #44 y #45 |
| Perfil de riesgo | **Opcional (0..1)**: con `perfilId` se usa su nivel; sin él, el factor `SIN_PERFIL` de las reglas. El perfil debe ser del mismo cliente y producto, y de los mismos datos declarados (si no, `PERFIL_NO_CORRESPONDE`). Un perfil `INFORMACION_INSUFICIENTE` (sin nivel) cotiza con `SIN_PERFIL` | `modelo_dominio.puml`: `Cotizacion "1" o-- "0..1" PerfilRiesgo : utiliza` |
| Cliente | **Obligatorio (1)**: `clienteId` debe existir en `clientes` (`CLIENTE_NO_ENCONTRADO`). No se exige identidad `VERIFICADO` para cotizar (se exige al perfilar y lo exigirá la suscripción) | `Cliente "1" --> "0..*" Cotizacion : recibe` |
| Socio | **Opcional (0..1)**: `socioId` es el canal de venta. Con `X-Actor-Rol: SOCIO` es el propio socio (`X-Actor-Id` = `SOC-NNNNNN`), que no puede usar perfiles (son del back-office) ni cotizar a nombre de otro socio, y solo lee sus cotizaciones | `SocioDistribucion "0..1" --> "0..*" Cotizacion : solicita` |
| Producto y reglas | `catalogo` versiona `productos` (ramo, país, moneda ISO 4217, estado, vigencia `[desde, hasta)`, coberturas) y `reglas_prima` (versión, vigencia, definición, hash; inmutables), con el mismo patrón de versión, hash canónico y validación de los modelos de riesgo | `modelo_dominio.puml` (Producto, Regla de precio), `modelo_informacion.puml` (Producto, cobertura y regla versionada) |
| Quién decide la vigencia | `catalogo` entrega el producto tal como está (para guardarlo en caché por id) y `cotizacion` aplica la tabla de decisión en la fecha de la cotización. Las reglas sí se resuelven por fecha (`?fecha=`) | Una caché por id sirve a cualquier fecha |
| Fecha de la cotización | El reloj de `cotizacion` (UTC). La edad se calcula con la fecha UTC, como el perfil (DT-27). No se acepta una fecha del cliente: no se puede cotizar con reglas pasadas | Regla de la historia: vigentes "en la fecha de cotización" |
| Caché | **`catalogo` escribe e invalida, `cotizacion` solo lee** (Valkey de HU-S02). Sin `CACHE_URL`, `cotizacion` usa una caché TTL en memoria (como `CatalogoHttp`). Contrato de claves: [contracts/README.md](../contracts/README.md#caché-de-catálogo-hu-w03) | `CatalogoReglas --> CacheCatalogo : actualiza / invalida`, `PerfilCotizacion --> CacheCatalogo : lectura` (componentes, despliegue e información) |
| `PRODUCTO_NO_ENCONTRADO` | **422** en `POST /v1/cotizaciones` (el producto es un campo del cuerpo y AC2 trata inexistente, inactivo y vencido como un mismo rechazo funcional); **404** cuando el producto es el recurso de la ruta (`/v1/productos/{id}/condiciones` y `catalogo`) | [contracts/codigos-error.md](../contracts/codigos-error.md) |
| `PRODUCTO_NO_DISPONIBLE` | Se conserva: lo sigue usando el perfilamiento (`/v1/perfiles`) cuando no hay modelo de riesgo vigente. La cotización usa los cuatro códigos nuevos | `cotizacion/adapters/inbound/http/perfilamiento.py` |
| Montos | `Decimal` en todo el cálculo (contexto propio de 34 dígitos), texto decimal en el JSON y `numeric(20,4)` en la base. La API recibe la suma asegurada como entero (COP), igual que el perfil | Regla "nada de float" |
| Diagramas | **Ningún cambio** en `docs/arquitectura`: todas las llamadas (`bff-web` → `cotizacion` → `catalogo`, `clientes`) y la caché ya estaban | Contrato acordado |

## 2. Tabla de decisión (AC2)

Implementada en `services/cotizacion/src/cotizacion/domain/cotizacion/elegibilidad.py`. Las condiciones se evalúan en orden y la primera que falla es la causa (una sola, estable).

| # | Producto | Estado | Fecha en `[vigenteDesde, vigenteHasta)` | Reglas con `vigenteDesde ≤ fecha` | Resultado |
| - | -------- | ------ | --------------------------------------- | --------------------------------- | --------- |
| 1 | No existe | — | — | — | 422 `PRODUCTO_NO_ENCONTRADO` |
| 2 | Existe | `INACTIVO` | — | — | 422 `PRODUCTO_INACTIVO` |
| 3 | Existe | `ACTIVO` | No | — | 422 `PRODUCTO_FUERA_DE_VIGENCIA` |
| 4 | Existe | `ACTIVO` | Sí | No | 422 `REGLAS_NO_VIGENTES` |
| 5 | Existe | `ACTIVO` | Sí | Sí | Se calcula la prima |

Precedencia completa del caso de uso (`application/cotizacion/calcular_prima.py`): 1) filas 1–3 del producto, 2) fallas técnicas de `catalogo` o `clientes` (503 `DEPENDENCIA_NO_DISPONIBLE`, falla cerrada), 3) fila 4, cliente inexistente y perfil, 4) coberturas y límites (`COBERTURA_NO_DISPONIBLE`, `DATOS_FUERA_DE_RANGO`), 5) cálculo. Así, un producto vencido responde siempre `PRODUCTO_FUERA_DE_VIGENCIA` aunque `clientes` esté caído.

**Límites exactos de fecha:** `vigenteDesde` incluido y `vigenteHasta` excluido (`test_vigencia_semiabierta_en_los_limites`: −1 µs, exacto, +1 µs); reglas con `vigenteDesde` igual a la fecha son vigentes (`test_reglas_en_el_limite_exacto`).

## 3. Cálculo de la prima (AC1)

`domain/cotizacion/prima.py`. Con R = redondeo a la escala y con el modo de las reglas (`ROUND_HALF_EVEN`, escala 0 en COP):

```
prima_base     = R(suma_asegurada × tasa_base)
prima_ajustada = R(prima_base × factor_nivel × Π factores por variable)   # EDAD y PLAZO por tramos
recargo_c      = R(prima_ajustada × tasa_c)                             # por cobertura opcional elegida
subtotal       = prima_ajustada + Σ recargo_c
prima_total    = min(max(subtotal, R(prima_minima)), R(prima_maxima))
ajuste_limite  = prima_total − subtotal
```

Cada línea del desglose queda redondeada y las sumas son exactas: el desglose cuadra con la prima. Reglas semilla `vida-hipotecario` 2026.09.1 (COP, mensual): tasa base 0,00042; nivel BAJO 0,90 · MEDIO 1,00 · ALTO 1,30 · SIN_PERFIL 1,15; EDAD ≤29 0,80 · ≤39 0,95 · ≤49 1,15 · ≤59 1,55 · resto 2,20; PLAZO ≤120 0,95 · ≤240 1,00 · resto 1,08; recargos Enfermedades graves 18 % y Auxilio funerario 4 %; prima mínima 15.000 y máxima 2.500.000; límites: suma 10.000.000–1.000.000.000, plazo 12–360 meses y edad 18–70 años.

**Ejemplo C01** (1990-04-18, 180 meses, 180.000.000 COP, sin perfil, con Enfermedades graves, el 2026-10-15): base 75.600 → × 1,15 × 0,95 × 1,00 = 82.593 → recargo 14.867 → **prima mensual 97.460 COP** (`test_ac1_prima_persistida_con_la_version_de_reglas`).

## 4. Clases de equivalencia y límites (N1)

`services/cotizacion/tests/unit/cotizacion/test_prima.py` y `test_elegibilidad.py`.

| Variable | Clases inválidas | Límites probados | Válidas |
| --- | --- | --- | --- |
| Suma asegurada | 0; < 10.000.000; > 1.000.000.000 | 9.999.999 ✗ · 10.000.000 ✓ · 1.000.000.000 ✓ · 1.000.000.001 ✗ | 180.000.000 |
| Plazo | < 12; > 360 | 11 ✗ · 12 ✓ · 360 ✓ · 361 ✗ | 180 |
| Edad (de la fecha de nacimiento) | < 18; > 70; nacimiento futuro | cumple 18 hoy ✓ · los cumple mañana ✗ · 29-feb · 70 ✓ · 71 ✗ | 36 |
| Coberturas opcionales | una obligatoria; una desconocida | — | ninguna, una, las dos (orden indiferente, repetidas cuentan una vez) |
| Nivel de riesgo | — | BAJO, MEDIO, ALTO, sin perfil | — |
| Prima | — | por debajo del mínimo (ajuste +12.127), igual al mínimo (sin ajuste), por encima del máximo (ajuste −297.296) | — |
| Valores grandes | — | 999.999.999.999 × 0,000123456789 sin pérdida de dígitos | — |
| Redondeo | — | 0,5 / 1,5 / 2,5 en HALF_EVEN; HALF_UP; DOWN; UP; escala 2 (0,125 → 0,12) y −2 (a centenas) | — |

## 5. Determinismo (AC3)

- Misma entrada y mismas reglas → mismo `Desglose` y misma `huellaCalculo` (sha256 del JSON canónico de producto, versión y hash de las reglas, moneda, entradas y desglose; sin identificadores ni instantes).
- El resultado no depende del contexto decimal global (`test_no_depende_del_contexto_decimal_global` cambia la precisión a 3 y el redondeo a FLOOR).
- La huella cambia si cambia la versión o el hash de las reglas.
- N2: dos solicitudes con claves distintas guardan dos filas con la misma huella y el mismo hash de reglas (`test_ac3_misma_entrada_y_version_mismo_resultado`). N3: la misma comprobación por el borde.

## 6. Persistencia, PII y seguridad

- **`cotizaciones`** (migración `003`, aditiva e inmutable; reversa en `migraciones/down/003_cotizaciones.sql`): producto y su versión, moneda, periodicidad, versión **y hash** de las reglas, perfil, entradas mínimas (suma, plazo, **edad** —no la fecha de nacimiento ni el documento—, nivel y coberturas, columna marcada `sensible`), coberturas incluidas, desglose, prima total, huella, estado `CALCULADA`, `Idempotency-Key` con huella de la solicitud (única por actor), `calculado_en`, duración, `correlation_id` y actor.
- **Idempotencia:** misma clave y mismo cuerpo → 200 con la misma cotización; otro cuerpo → 422 `IDEMPOTENCIA_CONFLICTO`; carrera de la misma clave → gana la primera.
- **Autorización:** `ASESOR` cotiza y lee cualquiera; `SOCIO` solo cotiza para sí, sin perfiles, y lee solo las suyas (las ajenas responden 404, como si no existieran). En el portal, solo el grupo `asesores`.
- **Logs:** el cierre (`cotizacion calculada`) lleva solo identificadores. `solventa_common.logging` redacta además las claves `*nacimiento*` y `*sumaasegurada*`. Los centinelas de la suite de módulo (`test_zz_centinelas.py`: fecha de nacimiento, suma asegurada y valores de C01) pasan con las pruebas de HU-W03 incluidas.
- **Portal:** los datos que permiten cotizar con el perfil (fecha de nacimiento y suma) quedan solo en memoria (`BorradorCotizacionService`), no en `sessionStorage` ni `localStorage`.
- **Bandit** (`uvx bandit -r services/catalogo/src services/cotizacion/src libs/solventa-common/src`): 0 altos. Medios: 14 B608 (SQL con f-string de **columnas constantes** y valores siempre parametrizados), 7 nuevos (`productos_pg` 5, `cotizaciones_pg` 2) con el mismo patrón que los 7 previos (`modelos_pg`, `perfiles_pg`). Bajos: 3 B101 previos en `errors.py`.

## 7. Observabilidad

- `operacion("cotizacion")` mide el caso de uso: alimenta la meta p95 de 250 ms (`latency_targets` en `int`), que ya tenía alarma y ahora recibe datos.
- Una operación por dependencia (`cotizacion.dependencia.catalogo.producto`, `.catalogo.reglas_prima`, `.clientes.identidad`, `.perfil`), lecturas (`cotizacion.consultar`, `cotizacion.condiciones`), `catalogo` (`producto.consultar`, `reglas_prima.consultar`) y la caché (`cache.leer|escribir|borrar`, con `vacio` para el fallo de caché). Detalle en [contracts/operaciones.md](../contracts/operaciones.md).
- La respuesta del cálculo trae `duracionMs`, `presupuestoMs` y los `pasos` con su duración y el origen del catálogo (`CACHE`, `MEMORIA` o `CATALOGO`): el portal los muestra en el mockup "Calculando tu oferta".

## 8. Pruebas y resultados (ejecutados el 2026-10-21, en local)

| Nivel | Qué | Resultado |
| --- | --- | --- |
| N1 `cotizacion` | Dominio, caso de uso, adaptador de catálogo + caché, repositorio y API (`tests/unit/cotizacion/`, 150 pruebas nuevas) | `make test-cov SERVICE=cotizacion`: **538 passed**; líneas **98,97 %**, ramas **97,24 %**. Solo el dominio y la aplicación de HU-W03: **100 % / 100 %** en el dominio, 99 % en la aplicación |
| N1 `catalogo` | Validación de reglas, semillas, API, repositorio y caché (`tests/unit/productos/`) | `make test-cov SERVICE=catalogo`: **150 passed**; líneas **99,56 %**, ramas **98,72 %**; dominio y aplicación de productos **100 % / 100 %** |
| N1 librería | `solventa_common.cache` y sanitización | `uv run pytest libs/solventa-common/tests/unit`: **193 passed** |
| N1 `bff-web` | Rutas de cotización y condiciones | `make test-cov SERVICE=bff-web`: **222 passed**; líneas **98,47 %** |
| N2 `cotizacion` | API real con `catalogo`, `clientes`, PostgreSQL y Valkey efímeros (`tests/module/test_cotizacion_w03.py`, 13 pruebas) + W10, W11, W12, W31 y centinelas | `make test-module SERVICE=cotizacion`: **81 passed** |
| N2 `catalogo` | Migraciones y semilla idempotentes, inmutabilidad, API sobre la base, Cache-Aside contra Valkey (`tests/module/test_productos.py`) | `make test-module SERVICE=catalogo`: **16 passed** |
| Contrato | OpenAPI regenerados y verificados, Schemathesis sobre las rutas nuevas, CT-W03-01 (esquema de reglas del consumidor = publicado), DTO de `bff-web` = `cotizacion`, valor de la caché = respuesta de la API | `make test-contract`: `cotizacion` **23**, `catalogo` **10**, `bff-web` **20**, `api-socios` **18** passed |
| N3 R1 | `tests/e2e/test_hu_w03_r1_prima.py` contra `int` | **No ejecutado**: la plataforma está destruida. Sin ambiente se omite con el motivo (4 skipped) |
| Regresión | `make test-unit` (todo el monorepo) y lint | **2107 passed**; `make lint` limpio en `catalogo`, `cotizacion` y `bff-web` |
| Portal | `npm run test:ci` (Karma + compuerta por feature) y `ng build` | **379 SUCCESS**; feature `cotizacion` líneas **99,44 %**, ramas **89,41 %**; `perfilamiento` 98,24 % / 89,92 %; build correcto |

## 9. AC → evidencia

| Criterio | Evidencia |
| --- | --- |
| **AC1** Prima en la moneda del producto y versión de reglas conservada | N1 `test_c01_sin_perfil`, `test_recargos_de_las_coberturas_opcionales`, `test_ac1_calcula_en_la_moneda_del_producto_y_conserva_la_version`; N2 `test_ac1_prima_persistida_con_la_version_de_reglas` (la fila guarda versión y **hash publicado por catalogo**, moneda, entradas y desglose) y `test_ac1_con_el_perfil_de_riesgo`; N3 `test_hu_w03_r1_solicitud_hasta_prima_persistida`; portal: `nuevo-perfilamiento.cotizacion.spec.ts` y `cotizacion-calculada.component.spec.ts` |
| **AC2** Inexistente, inactivo o vencido → causa funcional estable | Tabla de decisión (§2): N1 `test_tabla_de_decision`, `test_vigencia_semiabierta_en_los_limites`, `test_ac2_tabla_de_decision_en_el_caso_de_uso`, `test_el_producto_decide_antes_que_una_falla_tecnica_de_clientes`; API `test_codigos_estables`; N2 `test_ac2_causa_funcional_estable_sin_filas` (4 causas, sin filas); N3 `test_hu_w03_r1_producto_vencido_es_un_fallo_controlado`; códigos en `contracts/codigos-error.md` |
| **AC3** Mismo producto, datos y versión → misma prima y desglose | N1 `test_misma_entrada_mismo_desglose_y_misma_huella`, `test_no_depende_del_contexto_decimal_global`, `test_ac3_misma_solicitud_misma_prima_y_desglose`; N2 `test_ac3_misma_entrada_y_version_mismo_resultado`; N3 (segunda clave) |
| Regla: producto y reglas activos y vigentes en la fecha | §2 y pruebas de AC2 |
| Regla: se conservan moneda, versión, entradas y desglose | Migración `003`, `test_la_fila_vuelve_igual`, N2 AC1 |
| Seguridad: autorización del socio y sin PII en logs | `test_un_socio_solo_lee_sus_cotizaciones` (N1) y `test_un_socio_no_ve_cotizaciones_de_otro` (N2); `test_mide_la_operacion_y_no_registra_pii` y centinelas N2; Bandit (§6) |
| AWS/IaC: ECS de cotización, repositorio, caché y permisos mínimos | Sin cambios de Terraform (ya los creó HU-S02); `make infra-probar` paso 9 (Valkey y `CACHE_URL`); [infra/PLATAFORMA.md](../infra/PLATAFORMA.md#hu-w03-cálculo-de-prima) |
| Evidencia: cobertura, tabla de decisión, contrato y R1 | §8, §2, `contracts/openapi/{catalogo,cotizacion,bff-web}.yaml`, `contracts/modelos/reglas-prima.v1.schema.json`; R1 pendiente de ambiente |

## 10. Pendiente

- **Ejecutar en AWS (`int`)**: desplegar (`make infra-desplegar ENV=int`, o `make infra-microservicios SERVICES=catalogo,cotizacion,bff-web` si la plataforma ya existe), correr `make infra-probar ENV=int SMOKE_DLQ=0` (paso 9) y `uv run pytest tests/e2e -k hu_w03` (N3 R1), y medir el p95 real de `cotizacion` contra la meta de 250 ms en el tablero.
- **Revisión por otra persona** del diseño de reglas de prima (tarifas de la semilla, precedencia de causas y uso del perfil) y del contrato interno antes de HU-W04.
- **Portal en el navegador:** las pantallas se probaron con Karma (incluye axe), no con una revisión visual contra Figma (el conector de Figma no estaba autorizado en esta sesión; se usaron los PNG del issue).
- **Canal de venta:** el portal ofrece solo "Directo · asesor"; listar socios para el asesor necesita una ruta de `bff-web` accesible al grupo `asesores` (hoy `/socios` es de `administradores-socios`). El backend ya acepta `socioId`.
- **HU-W04:** oferta con vigencia, exclusiones e identificador público, contrato para el socio en `api-socios` (que ya puede llamar a `/v1/cotizaciones` con `X-Actor-Rol: SOCIO`) y cambio de estado de la cotización (la fila es inmutable: el estado posterior se registra aparte).
- **Publicación de reglas:** no hay API para publicar productos o reglas nuevas (se siembran al arrancar). Al agregarla, debe invalidar las claves `catalogo:v1:*` del producto.
- **Frontend:** después de fusionar el backend, `scripts/sync-contracts.sh <sha>` en el repo del frontend para fijar `contracts/openapi/VERSION` (el `bff-web.yaml` ya es el de esta rama).
