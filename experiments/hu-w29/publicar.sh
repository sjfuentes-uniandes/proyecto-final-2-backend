#!/usr/bin/env bash
# Publica resultados/informe.json (salida de reconciliar.py) en el bucket de experimentos de exp,
# donde lo lee bff-web para Operación › Conciliación (I05):
#   s3://<bucket>/hu-w29/<experimento>/informe.json   (histórico)
#   s3://<bucket>/hu-w29/informe.json                 (el último; el que muestra el portal)
# Escribe en S3: pedir confirmación antes (docs/hu-w29-multizona.md, paso 8).
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require terraform aws jq
require_env
informe="${RESULTADOS}/informe.json"
[ -f "${informe}" ] || die "Falta ${informe}: ejecutar reconciliar.py."
jq -e '.version == 1 and .experimento != null' "${informe}" >/dev/null || die "${informe} no es un informe de reconciliar.py."
tf_init platform

exp="$(experimento)"
bucket="$(jq -r .bucket <<<"${exp}")"
sse="$(jq -r .bucket_sse <<<"${exp}")"
id="$(jq -r .experimento "${informe}")"
for clave in "hu-w29/${id}/informe.json" "hu-w29/informe.json"; do
  aws s3 cp "${informe}" "s3://${bucket}/${clave}" --sse "${sse}" --content-type application/json >/dev/null
  echo "  s3://${bucket}/${clave}"
done
log "Informe publicado"
