# 0013. El recaudo lo hace `dinero` de forma asíncrona, a partir de `OfertaAceptada`

- Estado: propuesto
- Fecha: 2026-10-28 · Decide: por ratificar en la revisión de la PR del backend de HU-W16 (mismo procedimiento que la ADR 0012) · Historias/ASR: HU-W16, HU-W17; recorrido R3
- Fuente: `docs/exploracion-hu-w16-w17.md` (backend `main` @ `3334623`)

## Contexto

- La infraestructura **no se puede cambiar** en este trabajo. Ya declara el recaudo asíncrono:
  - `dinero` consume la cola `dinero-aceptaciones`, filtrada por `eventType = OfertaAceptada`, llama solo a `adaptador-pagos` y publica en el tópico de negocio (`infra/platform/catalog.tf:60-66`, `:142-147`).
  - `polizas` consume `polizas-recaudos` (prefijo `Recaudo`).
  - **Nadie tiene `dinero` en `calls`:** ni `bff-web` ni `polizas`.
  - `PLATAFORMA.md:136` descarta a propósito `polizas → dinero` por REST, aunque el diagrama VA-003A lo dibuja (`diagrama_componentes_responsabilidades.puml:85`).
- `OfertaAceptada` v1 (HU-W15) se emite al aceptar, en el outbox de `polizas`. Lleva `claveRecaudo` (= `aceptacionId`) y `prima {valor, moneda, periodicidad}`, sin datos del medio de pago. La guía de HU-W15 (`docs/hu-w15-aceptacion.md` §2) pide consumirlo con inbox por `eventId`, usar `claveRecaudo` como clave única del cobro, tratar el valor como `Decimal` y no revalidar la vigencia.
- El relay del outbox no existe (EXT-04): `solventa_common/outbox.py` e `inbox.py` están vacíos.
- Los mockups (W16, «Paso 3 de 3»; W17-1) ponen la firma del cliente antes del recaudo, un formulario de medio de pago que llena el asesor y un botón «Confirmar y recaudar». No hay ninguna historia para la firma del cliente. HU-W17 («cliente **que pagó** la prima… que Solventa firme») y `PLATAFORMA.md:148-153` ponen el recaudo antes de la firma y la emisión.

## Decisión

1. **Dueño:** el recaudo vive en `dinero`, como dicen el diagrama («Recauda primas») y el catálogo. Los datos del proveedor de pagos solo los maneja `adaptador-pagos`.
2. **Disparo:** asíncrono.
   - `polizas` publica `OfertaAceptada` mediante el relay.
   - `dinero` la consume con inbox por `eventId`, crea el movimiento `PENDIENTE` con commit **antes** de cobrar, y cobra por `adaptador-pagos`.
   - Después publica `RecaudoConfirmado` o `RecaudoRechazado` v1, que las colas existentes enrutan a `polizas`, `avisos` y `auditoria`.
3. **Orden del recorrido R3:** aceptación → recaudo → firma de Solventa y emisión (HU-W17). La firma de las condiciones por el cliente queda fuera de alcance mientras no exista una historia para ella.
4. **Idempotencia:** `claveRecaudo` es la clave única del movimiento (índice único) y la clave idempotente hacia el proveedor. Una entrega duplicada o concurrente no cobra dos veces.
5. **Incertidumbre:** un timeout, un 5xx o una respuesta inválida dejan el movimiento `PENDIENTE`; nunca se asume rechazo. Una conciliación dentro de `dinero`, **encendida por defecto**, consulta de forma periódica el estado por `claveRecaudo` de los movimientos `PENDIENTE` con cierta antigüedad. Solo una respuesta definitiva cambia el estado. Si el proveedor responde que el cobro no existe, se reintenta el cobro con la misma `claveRecaudo`, que es idempotente en el proveedor. Va encendida por defecto por dos razones: en los ambientes no se puede fijar una variable nueva sin cambiar `infra/`, y un `PENDIENTE` cuyo mensaje ya se borró de la cola no aparece en ninguna alarma.
6. **Medio de pago:** ningún dato de tarjeta ni de cuenta entra en Solventa. En el MVP, el proveedor simulado emula una domiciliación previa y elige el caso por un pagador sintético que envía `dinero`.
7. **Alcance:** solo el primer cobro de la prima. Los cobros recurrentes y la compensación (devolución) quedan fuera.
8. **Relay e inbox:** se implementan en `solventa_common` y se cablean solo en `polizas` y `dinero`. Lo hace esta historia, como Plataforma habilitante.
9. **Web:** el paso de recaudo se muestra **de solo lectura**. `polizas` proyecta el estado desde `polizas-recaudos` y `bff-web` lo lee de `polizas`.

## Diferencias con los mockups (para la revisión visual)

| Mockup | Qué muestra | Qué se implementa |
|---|---|---|
| W16 | Formulario de medio de pago llenado por el asesor | No se implementa: no hay canal hacia `dinero` ni hacia `adaptador-pagos` sin cambiar `infra/` |
| W16 | Botón «Confirmar y recaudar» | No existe: el cobro empieza al aceptar |
| W16 | «Pendiente de confirmación», referencia y primer cobro | Sí, de solo lectura |
| W16 | «Los siguientes cobros serán el día 1 de cada mes» | Diferido (cobros recurrentes) |
| W16 y W17-1 | Paso 2, «Firma electrónica» del cliente, antes del recaudo; «nunca se cobra sin firmar» | No se implementa (sin historia). El cobro ocurre sin firma del cliente |
| W17-3 | Devolución automática de la prima | Diferido: necesita un evento `polizas → dinero` que el filtro de `dinero-aceptaciones` no admite |

## Alternativas descartadas

- **Flujo del mockup (el asesor confirma el cobro desde el portal):** exige `bff-web → dinero` en `calls`, u otro tipo de evento en el filtro de `dinero-aceptaciones`. Ambas cosas son cambios de `infra/`.
- **`polizas → dinero` por REST (diagrama VA-003A):** `PLATAFORMA.md` lo descarta y `dinero` no es descubrible (`platform.tftest.hcl:292-294`).
- **Medio de pago dentro de `OfertaAceptada`:** el evento se emite antes de conocerlo y no debe llevar datos financieros del pagador.
- **Recaudo dentro de `polizas`:** contradice el catálogo y el diagrama, y mezcla datos de dinero en la base de pólizas.

## Consecuencias

- **Se gana:**
  - El diseño coincide con la infraestructura ya desplegable, con HU-W16 («mensajes duplicados») y con HU-W17.
  - La clave idempotente queda fija desde HU-W15.
  - El relay desbloquea también la Parte 2 del consentimiento (su PR 2.2).
- **Se sacrifica:**
  - El flujo que muestran los mockups y el indicador de pasos de la web (`siguientePaso = FIRMA`).
  - El cobro empieza sin confirmación del asesor y sin firma del cliente.
  - La consistencia del recorrido es eventual.
  - La conciliación corre en cada tarea de `dinero` y consulta al proveedor aunque no haya pendientes antiguos; la guarda `WHERE estado = 'PENDIENTE'` evita efectos dobles entre tareas.
- **Hay que cambiar (otra persona, en `infra/`), si se quiere el flujo del mockup más adelante:** `bff-web.calls` += `dinero`, o un filtro nuevo en `dinero-aceptaciones`, y un canal para el medio de pago. Para el recorrido en `int` ya hacía falta EXT-01 (`polizas.calls` += `cotizacion`, `catalogo`).
- Si el equipo no ratifica esta ADR, el plan de HU-W16 se detiene después de los pasos de Plataforma (16.1–16.6), que sirven con cualquier flujo.
