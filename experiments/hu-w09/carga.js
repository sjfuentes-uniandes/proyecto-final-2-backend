// Experimento de carga de la cotización (HU-W09, ARQ-002 y ARQ-003).
//
// Dos tramos:
//   alb    La curva completa, dentro de la VPC de EXP: POST {ALB_URL}/cotizaciones contra el
//          listener de api-socios del ALB interno, con los encabezados X-Partner-* que pondría
//          API Gateway. Seis etapas (e1..e6) de 500 a 50.000 por minuto, de 5 minutos cada una.
//   apigw  Verificación de extremo a extremo (WAF → API Gateway → api-socios), DESPUÉS de la
//          curva y solo si llegan todas sus variables: token client_credentials y API key.
//
// Lo lanza tests/e2e/test_hu_w09_carga.py como tarea Fargate (infra/apps/carga.tf, familia
// <prefijo>-carga). Este archivo viaja en base64 en la variable HU_W09_SCRIPT de la definición de
// tarea, y el contenedor (entryPoint sh -c) lo decodifica y lo corre:
//   echo "$HU_W09_SCRIPT" | base64 -d > /tmp/s.js && exec k6 run --log-format=raw -q /tmp/s.js
// La configuración llega como variables de entorno en las sobrescrituras de run_task:
//   ALB_URL=http://<dns del ALB>:8083, SOCIO_CLIENT_ID=<clientId>, SOCIO_KEY_ID=<id de la API key>
//   [APIGW_URL=<api de socios>, APIGW_TOKEN_URL=<token de Cognito>, APIGW_CLIENT_SECRET=...,
//    APIGW_API_KEY=... y APIGW_SCOPE=solventa/cotizar]
// En local, las mismas variables van como -e de k6:
//   k6 run --log-format=raw -e ALB_URL=http://localhost:8083 -e SOCIO_CLIENT_ID=... -e SOCIO_KEY_ID=... carga.js
//
// Resultado: una línea por etapa en el log, con el prefijo «HU-W09-ETAPA » y un JSON compacto
// (handleSummary), más un resumen de texto breve por etapa (sin jslib). Al empezar, una línea
// «HU-W09-INICIO » con t0 y la configuración no secreta; con --log-format=raw sale como JSON sin
// el envoltorio logfmt. k6 no juzga: los thresholds solo existen para que aparezcan las
// submétricas por escenario y nunca fallan. El juicio lo hace informe.md.
//
// Nunca imprime el secreto, la API key ni los tokens (ni los cuerpos de las respuestas).
//
// Solo para validar en local (los valores reales son los de por omisión):
//   ESCALA=0.01          multiplica las tasas de las etapas (por omisión, 1)
//   DURACION_ETAPA=20s   duración de cada etapa (por omisión, 5m)
import http from 'k6/http';
import crypto from 'k6/crypto';
import encoding from 'k6/encoding';
import exec from 'k6/execution';
import { Counter } from 'k6/metrics';
// Sin imports remotos (p. ej., jslib): si el dominio no respondiera desde el NAT de EXP, k6 no
// arrancaría y se perdería la ventana. El resumen de texto lo arma handleSummary.

const PREFIJO_ETAPA = 'HU-W09-ETAPA';
const PREFIJO_INICIO = 'HU-W09-INICIO';
const PREFIJO_TOKENS = 'HU-W09-TOKENS';

// --- Configuración ----------------------------------------------------------------------------

function texto(nombre, porOmision) {
  const valor = __ENV[nombre];
  return valor === undefined || valor === '' ? porOmision : valor;
}

function numero(nombre, porOmision, minimo, maximo) {
  const valor = Number(texto(nombre, String(porOmision)));
  if (!Number.isFinite(valor) || valor <= minimo || valor > maximo) {
    throw new Error(`HU-W09: ${nombre} debe ser un número mayor que ${minimo} y hasta ${maximo}`);
  }
  return valor;
}

function segundos(nombre, porOmision) {
  const valor = texto(nombre, porOmision);
  const partes = /^(\d+)(s|m)$/.exec(valor);
  if (!partes || Number(partes[1]) === 0) {
    throw new Error(`HU-W09: ${nombre} debe tener la forma <n>s o <n>m (por ejemplo, 5m)`);
  }
  return Number(partes[1]) * (partes[2] === 'm' ? 60 : 1);
}

const ESCALA = numero('ESCALA', 1, 0, 1);
const DURACION_ETAPA_S = segundos('DURACION_ETAPA', '5m');
const TIMEOUT = texto('TIMEOUT', '3s');

// Tramo por el ALB: obligatorio.
const ALB_URL = texto('ALB_URL', '').replace(/\/+$/, '');
const SOCIO_CLIENT_ID = texto('SOCIO_CLIENT_ID', '');
const SOCIO_KEY_ID = texto('SOCIO_KEY_ID', '');
// Como el claim scope del token: separados por espacio, con el servidor de recursos (solventa).
const SOCIO_SCOPES = texto('SOCIO_SCOPES', 'solventa/cotizar');

const faltantesAlb = ['ALB_URL', 'SOCIO_CLIENT_ID', 'SOCIO_KEY_ID'].filter((n) => texto(n, '') === '');
if (faltantesAlb.length > 0) {
  throw new Error(`HU-W09: faltan las variables del tramo por el ALB: ${faltantesAlb.join(', ')}`);
}
if (!SOCIO_SCOPES.split(' ').some((s) => s.endsWith('/cotizar'))) {
  throw new Error('HU-W09: SOCIO_SCOPES debe incluir el permiso cotizar (por ejemplo, solventa/cotizar)');
}

// Tramo por API Gateway: todo o nada. Solo se nombran las variables que faltan, nunca sus valores.
const VARIABLES_APIGW = ['APIGW_URL', 'APIGW_TOKEN_URL', 'APIGW_CLIENT_SECRET', 'APIGW_API_KEY', 'APIGW_SCOPE'];
const presentesApigw = VARIABLES_APIGW.filter((n) => texto(n, '') !== '');
if (presentesApigw.length > 0 && presentesApigw.length < VARIABLES_APIGW.length) {
  const faltan = VARIABLES_APIGW.filter((n) => texto(n, '') === '');
  throw new Error(`HU-W09: el tramo por API Gateway necesita también: ${faltan.join(', ')}`);
}
const CON_APIGW = presentesApigw.length === VARIABLES_APIGW.length;
const APIGW_URL = texto('APIGW_URL', '').replace(/\/+$/, '');
const APIGW_TOKEN_URL = texto('APIGW_TOKEN_URL', '');
const APIGW_CLIENT_SECRET = texto('APIGW_CLIENT_SECRET', '');
const APIGW_API_KEY = texto('APIGW_API_KEY', '');
const APIGW_SCOPE = texto('APIGW_SCOPE', '');
// El WAF de EXP bloquea una IP con más de 50.000 solicitudes en 5 minutos (waf_rate_limit en
// envs/exp/platform.tfvars), unas 10.000 por minuto, y k6 sale por un solo NAT. 6.000 por minuto
// (100 por segundo) deja un 40 % de margen para la ventana deslizante del WAF, y queda muy por
// debajo del plan experimento (500 por segundo) y del stage (1.000 por segundo). Si la API key se
// queda en estandar (50 por segundo; decisión 2, opción c), use APIGW_TASA=2400.
const APIGW_TASA = Math.max(1, Math.round(numero('APIGW_TASA', 6000, 0, 9000) * ESCALA));
const APIGW_DURACION_S = segundos('APIGW_DURACION', '5m');

// Datos: clientes sintéticos con identidad VERIFICADO (contracts/datos-sinteticos/
// clientes-perfilamiento.json; los siembra clientes con CLIENTES_SEMILLA_SINTETICA). Se excluyen
// C11 (identidad RECHAZADO) y C12 (PENDIENTE). La cotización por socios va sin perfilId: no llama
// a adaptador-datos ni al simulador, así que el comportamiento de las fuentes de cada cliente no
// interviene (calcular_prima.py, _consultar).
const CLIENTES_POR_OMISION = 'C01,C02,C03,C04,C05,C06,C07,C08,C09,C10,C13,C14,C15,C16,C17,C18,C19';
const CLIENTES = texto('CLIENTES', CLIENTES_POR_OMISION).split(',').map((c) => c.trim());
const PRODUCTO_ID = texto('PRODUCTO_ID', 'vida-hipotecario');
const COBERTURAS = texto('COBERTURAS', 'ENFERMEDADES_GRAVES').split(',').filter((c) => c !== '');

function clienteId(codigo) {
  const partes = /^C(\d{2})$/.exec(codigo);
  const n = partes ? Number(partes[1]) : 0;
  if (n < 1 || n > 33) throw new Error(`HU-W09: cliente sintético desconocido en CLIENTES: ${codigo}`);
  return `00000000-0000-4000-8000-${n.toString(16).padStart(12, '0')}`;
}

// Un cuerpo por cliente, armado una vez por VU (datos de C01: los 33 declaran los mismos).
const CUERPOS = CLIENTES.map((codigo) => JSON.stringify({
  clienteId: clienteId(codigo),
  productoId: PRODUCTO_ID,
  datosAsegurado: { fechaNacimiento: '1990-04-18', plazoCreditoMeses: 180, sumaAsegurada: 180000000 },
  coberturasOpcionales: COBERTURAS,
}));

// --- Etapas y dimensionamiento -----------------------------------------------------------------
//
// Con constant-arrival-rate, los VUs ocupados son tasa por segundo × latencia:
//   preAllocatedVUs = tasa/s × 0,5 s × 1,5. Se supone una latencia de 0,5 s, el doble de la meta
//                     de cotizacion (250 ms) para cubrir api-socios y el ALB, con un 50 % de margen.
//   maxVUs          = tasa/s × 2 s. El presupuesto de socio.cotizar es de 1,6 s (latency_budget_ms):
//                     api-socios responde antes o corta; 2 s le da un 25 % de margen.
// e6 (50.000/min = 833/s): 625 preasignados y hasta 1.667. k6 comparte los VUs entre escenarios que
// no se solapan: el grupo se dimensiona con el mayor solape, el de e5 y e6 durante el gracefulStop
// (938 preasignados al inicio y, en el peor caso, 834 + 1.667 = 2.501). La memoria de la tarea
// (infra/apps, load_test_memory) se midió en local con este script (informe, «Configuración»).
// Si una etapa llega a maxVUs, k6 descarta iteraciones (dropped_iterations): si la latencia de esa
// etapa supera los 2 s, el cuello es el sistema; si no, es k6, y la etapa no cuenta (decisión 15).
const ETAPAS_POR_MINUTO = [500, 2000, 5000, 10000, 25000, 50000];
const LATENCIA_SUPUESTA_S = 0.5;
const MARGEN_PREASIGNADOS = 1.5;
const LATENCIA_MAXIMA_S = 2;

function vus(porMinuto) {
  const porSegundo = porMinuto / 60;
  return {
    preAllocatedVUs: Math.max(5, Math.ceil(porSegundo * LATENCIA_SUPUESTA_S * MARGEN_PREASIGNADOS)),
    maxVUs: Math.max(10, Math.ceil(porSegundo * LATENCIA_MAXIMA_S)),
  };
}

// gracefulStop corto: con el timeout de 3 s, lo pendiente de una etapa termina en los primeros
// segundos de la siguiente y no se mezcla con ella (cada solicitud lleva la etiqueta de su escenario).
const ETAPAS = ETAPAS_POR_MINUTO.map((porMinuto, i) => {
  const tasa = Math.max(1, Math.round(porMinuto * ESCALA));
  return { nombre: `e${i + 1}`, tramo: 'alb', pedidaPorMinuto: tasa, inicioS: i * DURACION_ETAPA_S, duracionS: DURACION_ETAPA_S };
});
const PAUSA_ANTES_DE_APIGW_S = 30;
if (CON_APIGW) {
  ETAPAS.push({
    nombre: 'gw', tramo: 'apigw', pedidaPorMinuto: APIGW_TASA,
    inicioS: ETAPAS_POR_MINUTO.length * DURACION_ETAPA_S + PAUSA_ANTES_DE_APIGW_S, duracionS: APIGW_DURACION_S,
  });
}

const errores4xx = new Counter('hu_w09_errores_4xx');
const errores5xx = new Counter('hu_w09_errores_5xx');
const erroresRed = new Counter('hu_w09_errores_red');
const tokensFallidos = new Counter('hu_w09_tokens_fallidos'); // solicitudes de token fallidas
const sinToken = new Counter('hu_w09_sin_token'); // iteraciones de gw terminadas sin token

// Thresholds que siempre se cumplen: hacen aparecer las submétricas por escenario en el resumen.
function umbrales() {
  const t = {};
  for (const etapa of ETAPAS) {
    const filtro = `{scenario:${etapa.nombre},tipo:cotizacion}`;
    t[`http_req_duration${filtro}`] = ['p(95)>=0', 'p(99)>=0'];
    t[`http_req_failed${filtro}`] = ['rate>=0'];
    t[`http_reqs${filtro}`] = ['count>=0'];
    t[`hu_w09_errores_4xx${filtro}`] = ['count>=0'];
    t[`hu_w09_errores_5xx${filtro}`] = ['count>=0'];
    t[`hu_w09_errores_red${filtro}`] = ['count>=0'];
    t[`dropped_iterations{scenario:${etapa.nombre}}`] = ['count>=0'];
  }
  return t;
}

export const options = {
  scenarios: Object.fromEntries(ETAPAS.map((etapa) => [etapa.nombre, Object.assign({
    executor: 'constant-arrival-rate',
    exec: etapa.tramo === 'alb' ? 'cotizarPorAlb' : 'cotizarPorApigw',
    rate: etapa.pedidaPorMinuto,
    timeUnit: '1m',
    duration: `${etapa.duracionS}s`,
    startTime: `${etapa.inicioS}s`,
    gracefulStop: '5s',
  }, vus(etapa.pedidaPorMinuto))])),
  thresholds: umbrales(),
  summaryTrendStats: ['avg', 'min', 'med', 'max', 'p(90)', 'p(95)', 'p(99)'],
  // Solo se necesita el estado; el cuerpo del token se pide aparte (responseType: 'text').
  discardResponseBodies: true,
  setupTimeout: '30s',
};

// --- Solicitudes -------------------------------------------------------------------------------

function uuid() {
  const b = new Uint8Array(crypto.randomBytes(16));
  b[6] = (b[6] & 0x0f) | 0x40;
  b[8] = (b[8] & 0x3f) | 0x80;
  const h = Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('');
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}

function cuerpo() {
  return CUERPOS[exec.scenario.iterationInTest % CUERPOS.length];
}

function contar(respuesta) {
  const etiquetas = { tipo: 'cotizacion' };
  if (respuesta.status === 0) erroresRed.add(1, etiquetas);
  else if (respuesta.status >= 500) errores5xx.add(1, etiquetas);
  else if (respuesta.status >= 400) errores4xx.add(1, etiquetas);
}

// La misma UUID es la Idempotency-Key y el sufijo del X-Correlation-Id: una sola generación por
// solicitud, y el log de los servicios se cruza con la clave.
function cotizar(url, encabezados) {
  const clave = uuid();
  const respuesta = http.post(url, cuerpo(), {
    headers: Object.assign({
      'Content-Type': 'application/json',
      'Idempotency-Key': clave,
      'X-Correlation-Id': `hu-w09-${exec.scenario.name}-${clave}`,
    }, encabezados),
    timeout: TIMEOUT,
    tags: { tipo: 'cotizacion', name: 'POST /cotizaciones' },
  });
  contar(respuesta);
}

export function cotizarPorAlb() {
  cotizar(`${ALB_URL}/cotizaciones`, {
    'X-Partner-Client-Id': SOCIO_CLIENT_ID,
    'X-Partner-Key-Id': SOCIO_KEY_ID,
    'X-Partner-Scopes': SOCIO_SCOPES,
  });
}

// Un token por VU (variables de módulo: cada VU tiene las suyas), renovado 60 s antes de vencer.
// Tras un fallo, el VU no vuelve a pedirlo hasta pasados ESPERA_TRAS_FALLO_MS: sin sleep, sus
// iteraciones terminan sin cotizar (cuentan en hu_w09_sin_token). Así, si Cognito falla, recibe a lo
// sumo una solicitud por VU cada 10 s, y no una por iteración. El aviso sale solo en el primer fallo
// de cada VU; los demás solo suman a hu_w09_tokens_fallidos.
let token = null;
let venceMs = 0;
let proximoIntentoMs = 0;
let avisado = false;
const RENOVAR_ANTES_MS = 60 * 1000;
const ESPERA_TRAS_FALLO_MS = 10 * 1000;

function tokenVigente() {
  if (token !== null && Date.now() < venceMs - RENOVAR_ANTES_MS) return token;
  if (Date.now() < proximoIntentoMs) return null;
  const respuesta = http.post(
    APIGW_TOKEN_URL,
    { grant_type: 'client_credentials', scope: APIGW_SCOPE },
    {
      headers: { Authorization: `Basic ${encoding.b64encode(`${SOCIO_CLIENT_ID}:${APIGW_CLIENT_SECRET}`)}` },
      timeout: '10s',
      responseType: 'text',
      tags: { tipo: 'token', name: 'POST token' },
    },
  );
  if (respuesta.status !== 200) {
    tokensFallidos.add(1);
    if (!avisado) {
      // Solo el estado: el cuerpo de un error de Cognito no se imprime.
      console.warn(`HU-W09: el token no se obtuvo (HTTP ${respuesta.status}); este VU lo reintenta cada ${ESPERA_TRAS_FALLO_MS / 1000} s y no vuelve a avisar (total en HU-W09-TOKENS)`);
      avisado = true;
    }
    token = null;
    proximoIntentoMs = Date.now() + ESPERA_TRAS_FALLO_MS;
    return null;
  }
  const datos = JSON.parse(respuesta.body);
  token = datos.access_token;
  venceMs = Date.now() + Number(datos.expires_in || 3600) * 1000;
  return token;
}

export function cotizarPorApigw() {
  const vigente = tokenVigente();
  if (vigente === null) {
    sinToken.add(1);
    return;
  }
  cotizar(`${APIGW_URL}/cotizaciones`, { Authorization: `Bearer ${vigente}`, 'x-api-key': APIGW_API_KEY });
}

// --- Inicio y resumen --------------------------------------------------------------------------

export function setup() {
  const t0 = new Date().toISOString();
  // Solo configuración no secreta: ni el clientId ni el id de la API key, ni nada del tramo apigw.
  console.log(`${PREFIJO_INICIO} ${JSON.stringify({
    t0, escala: ESCALA, duracionEtapaS: DURACION_ETAPA_S, clientes: CLIENTES, productoId: PRODUCTO_ID,
    tramos: CON_APIGW ? ['alb', 'apigw'] : ['alb'],
    etapas: ETAPAS.map((e) => ({ etapa: e.nombre, pedidaPorMinuto: e.pedidaPorMinuto, inicioS: e.inicioS, duracionS: e.duracionS })),
  })}`);
  return { t0 };
}

function valores(data, nombre) {
  const metrica = data.metrics[nombre];
  return metrica ? metrica.values : null;
}

function redondear(valor, decimales) {
  if (valor === null || valor === undefined || !Number.isFinite(valor)) return null;
  const f = 10 ** decimales;
  return Math.round(valor * f) / f;
}

// Los datos de cada etapa, tomados de las submétricas {scenario:eN,tipo:cotizacion}.
function datosPorEtapa(data) {
  const t0 = data.setup_data ? data.setup_data.t0 : null;
  return ETAPAS.map((etapa) => {
    const filtro = `{scenario:${etapa.nombre},tipo:cotizacion}`;
    const reqs = valores(data, `http_reqs${filtro}`);
    const fallidas = valores(data, `http_req_failed${filtro}`);
    const duracion = valores(data, `http_req_duration${filtro}`);
    const dropped = valores(data, `dropped_iterations{scenario:${etapa.nombre}}`);
    const cuenta = (nombre) => {
      const v = valores(data, `${nombre}${filtro}`);
      return v ? v.count : 0;
    };
    const solicitudes = reqs ? reqs.count : 0;
    return {
      etapa: etapa.nombre,
      tramo: etapa.tramo,
      t0,
      inicioS: etapa.inicioS,
      duracionS: etapa.duracionS,
      escala: ESCALA,
      pedidaPorMinuto: etapa.pedidaPorMinuto,
      solicitudes,
      logradaPorMinuto: redondear(solicitudes / (etapa.duracionS / 60), 1),
      errores: fallidas ? fallidas.passes : 0,
      tasaError: fallidas ? redondear(fallidas.rate, 5) : null,
      errores4xx: cuenta('hu_w09_errores_4xx'),
      errores5xx: cuenta('hu_w09_errores_5xx'),
      erroresRed: cuenta('hu_w09_errores_red'),
      p95Ms: duracion ? redondear(duracion['p(95)'], 1) : null,
      p99Ms: duracion ? redondear(duracion['p(99)'], 1) : null,
      dropped: dropped ? dropped.count : 0,
    };
  });
}

// Una línea por etapa: lo que la 9.4 lee del log de la tarea (prefijo fijo, JSON compacto).
export function lineasPorEtapa(data) {
  return datosPorEtapa(data).map((d) => `${PREFIJO_ETAPA} ${JSON.stringify(d)}`);
}

// Resumen de texto breve para leer el log a ojo: una fila por etapa.
export function resumenTexto(data) {
  const columnas = [
    ['etapa', (d) => d.etapa],
    ['tramo', (d) => d.tramo],
    ['pedida/min', (d) => d.pedidaPorMinuto],
    ['lograda/min', (d) => d.logradaPorMinuto],
    ['solicitudes', (d) => d.solicitudes],
    ['errores', (d) => d.errores],
    ['4xx/5xx/red', (d) => `${d.errores4xx}/${d.errores5xx}/${d.erroresRed}`],
    ['p95 ms', (d) => d.p95Ms],
    ['p99 ms', (d) => d.p99Ms],
    ['dropped', (d) => d.dropped],
  ];
  const filas = datosPorEtapa(data).map((d) => columnas.map(([, f]) => String(f(d) === null ? '-' : f(d))));
  const anchos = columnas.map(([titulo], i) => Math.max(titulo.length, ...filas.map((fila) => fila[i].length)));
  const linea = (celdas) => celdas.map((c, i) => c.padStart(anchos[i])).join('  ');
  const vus = valores(data, 'vus_max');
  const pie = [
    `t0 ${data.setup_data ? data.setup_data.t0 : '-'} · escala ${ESCALA} · etapa de ${DURACION_ETAPA_S} s · vus_max ${vus ? vus.max : '-'}`,
  ];
  return ['HU-W09 · resumen por etapa', linea(columnas.map(([titulo]) => titulo)), ...filas.map(linea), ...pie].join('\n');
}

export function handleSummary(data) {
  const fallidos = valores(data, 'hu_w09_tokens_fallidos');
  const sinTokenTotal = valores(data, 'hu_w09_sin_token');
  // fallidos: solicitudes de token fallidas; sin_token: iteraciones de gw terminadas sin token.
  const tokens = { fallidos: fallidos ? fallidos.count : 0, sin_token: sinTokenTotal ? sinTokenTotal.count : 0 };
  const pie = CON_APIGW ? `\n${PREFIJO_TOKENS} ${JSON.stringify(tokens)}` : '';
  return { stdout: `${lineasPorEtapa(data).join('\n')}${pie}\n\n${resumenTexto(data)}\n` };
}
