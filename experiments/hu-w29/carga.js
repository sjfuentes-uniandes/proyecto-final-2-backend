// Carga de fondo de bajo volumen del experimento multizona (HU-W29, AC1 y AC2).
// Corre antes, durante y después de la falla, por el borde real (WAF → API Gateway → bff-web):
//   R1  cotizar                      POST /web/v1/cotizaciones (Idempotency-Key)
//   R3  perfil → cotización → decisión → aceptación con Idempotency-Key (HU-W15)
//   R5  eventos firmados (HMAC-SHA256, D5): desactivado hasta HU-W21/HU-W22 (R5=1 lo activa)
//
// Cada respuesta queda en la consola como una línea JSON con "hu29"; con --console-output se
// guarda en resultados/carga.log y reconciliar.py toma las confirmadas (2xx) con su clave.
// Un reintento usa la MISMA clave: así se prueba que reintentar durante la falla no duplica.
//
//   k6 run --console-output=resultados/carga.log \
//     -e CANALES=<api_urls.canales> -e TOKEN=<id token del back-office, grupo asesores> \
//     -e DURACION=30m experiments/hu-w29/carga.js
//
// La lógica de los recorridos es la de las N3 (tests/e2e/test_hu_w03_r1_prima.py y
// test_hu_w13_w15_r3.py). Reutilizable por HU-W09 (Santi-MISO) cambiando las tasas.
import crypto from 'k6/crypto';
import http from 'k6/http';
import { sleep } from 'k6';

const CANALES = __ENV.CANALES;
const TOKEN = __ENV.TOKEN;
const DURACION = __ENV.DURACION || '30m';
const R1_POR_MINUTO = Number(__ENV.R1_POR_MINUTO || 60);
const R3_POR_MINUTO = Number(__ENV.R3_POR_MINUTO || 6);
const R5_POR_MINUTO = Number(__ENV.R5_POR_MINUTO || 30);
const REINTENTOS = Number(__ENV.REINTENTOS || 3);
const TIMEOUT = __ENV.TIMEOUT || '10s';

// Datos sintéticos C01 / S01 (contracts/datos-sinteticos/cotizaciones-suscripcion.json): aprobada.
const CLIENTE = __ENV.CLIENTE_ID || '00000000-0000-4000-8000-000000000001';
const DATOS = { fechaNacimiento: '1990-04-18', plazoCreditoMeses: 180, sumaAsegurada: 180000000 };
const VERSION_DECLARACIONES = __ENV.VERSION_DECLARACIONES || '2026.10.1';
const DECLARACIONES = { leidoCondiciones: true, informacionVeraz: true, debitoAutomatico: true, avisosElectronicos: true };

function escenario(exec, porMinuto) {
  return {
    executor: 'constant-arrival-rate', exec, rate: porMinuto, timeUnit: '1m', duration: DURACION,
    preAllocatedVUs: Math.max(2, Math.ceil(porMinuto / 6)), maxVUs: Math.max(10, porMinuto),
  };
}

export const options = {
  scenarios: Object.assign(
    { r1: escenario('r1', R1_POR_MINUTO), r3: escenario('r3', R3_POR_MINUTO) },
    __ENV.R5 === '1' ? { r5: escenario('r5', R5_POR_MINUTO) } : {},
  ),
  // Sin umbrales: el criterio lo aplican reconciliar.py (AC2) y el informe (AC1, AC3).
  summaryTrendStats: ['avg', 'p(95)', 'p(99)', 'max'],
};

export function setup() {
  if (!CANALES || !TOKEN) throw new Error('Indique -e CANALES=<api_urls.canales> -e TOKEN=<id token del back-office>');
}

function uuid() {
  const b = new Uint8Array(crypto.randomBytes(16));
  b[6] = (b[6] & 0x0f) | 0x40;
  b[8] = (b[8] & 0x3f) | 0x80;
  const h = Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('');
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}

function registrar(recorrido, paso, clave, respuesta, intento, extra) {
  let cuerpo = null;
  try { cuerpo = respuesta.json(); } catch (_) { /* 5xx del borde o timeout: sin JSON */ }
  console.log(JSON.stringify(Object.assign({
    hu29: 1, recorrido, paso, clave, intento, status: respuesta.status,
    confirmada: respuesta.status >= 200 && respuesta.status < 300,
    id: cuerpo && cuerpo.id ? cuerpo.id : null,
    correlationId: respuesta.headers['X-Correlation-Id'] || null,
    duracionMs: Math.round(respuesta.timings.duration), instante: new Date().toISOString(),
  }, extra || {})));
  return cuerpo;
}

// POST con Idempotency-Key; reintenta con la misma clave ante 5xx, 429 o timeout (status 0).
function enviar(recorrido, paso, ruta, cuerpo, extra) {
  const clave = uuid();
  const parametros = {
    headers: { Authorization: `Bearer ${TOKEN}`, 'Content-Type': 'application/json', 'Idempotency-Key': clave },
    timeout: TIMEOUT, tags: { recorrido, paso },
  };
  for (let intento = 1; intento <= REINTENTOS; intento++) {
    const respuesta = http.post(`${CANALES}/web/v1${ruta}`, JSON.stringify(cuerpo), parametros);
    const json = registrar(recorrido, paso, clave, respuesta, intento, extra);
    if (respuesta.status >= 200 && respuesta.status < 300) return json;
    if (respuesta.status !== 0 && respuesta.status !== 429 && respuesta.status < 500) return null;
    sleep(Math.min(2 ** intento, 8));
  }
  return null;
}

export function r1() {
  enviar('R1', 'cotizacion', '/cotizaciones', {
    clienteId: CLIENTE, productoId: 'vida-hipotecario', datosAsegurado: DATOS, coberturasOpcionales: ['ENFERMEDADES_GRAVES'],
  });
}

export function r3() {
  const perfil = enviar('R3', 'perfil', '/perfiles', { clienteId: CLIENTE, productoId: 'vida-hipotecario', datosDeclarados: DATOS });
  if (!perfil) return;
  const cotizacion = enviar('R3', 'cotizacion', '/cotizaciones', {
    clienteId: CLIENTE, productoId: 'vida-hipotecario', datosAsegurado: DATOS, perfilId: perfil.id,
  });
  if (!cotizacion) return;
  const decision = enviar('R3', 'decision', '/suscripciones', { cotizacionId: cotizacion.id });
  if (!decision || decision.resultado !== 'APROBADA') return;
  enviar('R3', 'aceptacion', `/cotizaciones/${decision.cotizacionId}/aceptacion`, {
    decisionId: decision.id, versionDeclaraciones: VERSION_DECLARACIONES, declaraciones: DECLARACIONES,
  }, { cotizacionId: decision.cotizacionId });
}

// R5: pendiente de HU-W21 (contrato de la entrada firmada). Se configura sin tocar el código:
//   R5_URL        entrada de telemetría (POST /telemetria/v1/eventos en API Gateway)
//   R5_SECRETO    hmac_secret del aliado telemetria (ALLY_TELEMETRIA_CREDENTIALS)
//   R5_PLANTILLA  cuerpo JSON con {{eventId}} y {{instante}}
//   R5_CABECERA_FIRMA / R5_CABECERA_INSTANTE  nombres de los encabezados de HU-W21
// La firma es HMAC-SHA256 de "<instante>.<cuerpo>"; ajustar si HU-W21 define otro mensaje.
const R5_PLANTILLA = __ENV.R5 === '1' ? open(__ENV.R5_PLANTILLA) : '';

export function r5() {
  const eventId = uuid();
  const instante = new Date().toISOString();
  const cuerpo = R5_PLANTILLA.replaceAll('{{eventId}}', eventId).replaceAll('{{instante}}', instante);
  const firma = crypto.hmac('sha256', __ENV.R5_SECRETO, `${instante}.${cuerpo}`, 'hex');
  const parametros = {
    headers: { 'Content-Type': 'application/json', [__ENV.R5_CABECERA_FIRMA]: firma, [__ENV.R5_CABECERA_INSTANTE]: instante },
    timeout: TIMEOUT, tags: { recorrido: 'R5', paso: 'evento' },
  };
  for (let intento = 1; intento <= REINTENTOS; intento++) {
    const respuesta = http.post(__ENV.R5_URL, cuerpo, parametros);
    registrar('R5', 'evento', eventId, respuesta, intento);
    if (respuesta.status >= 200 && respuesta.status < 300) return;
    if (respuesta.status !== 0 && respuesta.status !== 429 && respuesta.status < 500) return;
    sleep(Math.min(2 ** intento, 8));
  }
}
