// Alta interna del socio del experimento de carga (HU-W09, decisión 3).
//
// POST {ALB_URL}/admin/socios por el listener de api-socios del ALB interno, sin X-Partner-*
// (con ellos, /admin/* responde 404). Plan estandar y permiso cotizar; la preparación mueve
// después la API key al plan experimento de API Gateway (decisión 2).
//
// Imprime UNA línea, con el prefijo «HU-W09-ALTA », con solo id y clientId. Nunca el clientSecret
// ni la apiKey, que son la única vez que aparecen (SocioCreado). Si la respuesta no es 201,
// termina con un código distinto de 0 y el estado y el código de error del servicio.
//
//   k6 run --log-format=raw -e ALB_URL=http://<dns del ALB>:8083 <URL prefirmada de este archivo>
import http from 'k6/http';
import exec from 'k6/execution';

const PREFIJO = 'HU-W09-ALTA';
const ALB_URL = (__ENV.ALB_URL || '').replace(/\/+$/, '');

export const options = { vus: 1, iterations: 1 };

export default function () {
  if (ALB_URL === '') exec.test.abort('HU-W09: falta la variable ALB_URL');

  const fecha = new Date().toISOString().slice(0, 10);
  const respuesta = http.post(
    `${ALB_URL}/admin/socios`,
    JSON.stringify({ nombre: `Socio experimento de carga HU-W09 ${fecha}`, canal: 'plataforma_digital', plan: 'estandar', permisos: ['cotizar'] }),
    { headers: { 'Content-Type': 'application/json', 'X-Correlation-Id': `hu-w09-alta-${Date.now()}` }, timeout: '15s' },
  );

  if (respuesta.status !== 201) {
    // Del cuerpo de error solo el código ({codigo, mensaje, correlationId, detalles}).
    let codigo = null;
    try { codigo = respuesta.json('codigo'); } catch (_) { /* sin JSON: red, ALB o cuerpo vacío */ }
    exec.test.abort(`HU-W09: el alta del socio respondió HTTP ${respuesta.status}${codigo ? ` (${codigo})` : ''}; se esperaba 201`);
  }

  const socio = respuesta.json();
  console.log(`${PREFIJO} ${JSON.stringify({ id: socio.id, clientId: socio.clientId })}`);
}

// Sin el resumen de k6: la única salida es la línea de arriba (o el motivo del aborto).
export function handleSummary() {
  return {};
}
