#!/usr/bin/env bash
# Llena fis.json con los ARN del ambiente (salidas de Terraform) en resultados/fis.json.
# Solo lee: no crea la plantilla ni inicia el experimento; imprime los comandos para
# ejecutarlos con confirmación (docs/hu-w29-multizona.md, paso 5).
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require terraform aws jq
require_env
tf_init platform

exp="$(experimento)"
prefijo="${NAME}-${ENV}"
# Condición de parada: la alarma crítica de 5XX del API de canales (HU-W28). Si la tasa de 5XX
# supera api_5xx_rate en 3 de 5 minutos, FIS restaura las ACL y termina el experimento.
alarma="arn:aws:cloudwatch:${AWS_REGION}:$(account_id):alarm:${prefijo}-api-canales-5xx"
aws cloudwatch describe-alarms --alarm-names "${prefijo}-api-canales-5xx" --query 'MetricAlarms[0].AlarmName' \
  --output text | grep -q "${prefijo}-api-canales-5xx" || die "No existe la alarma ${prefijo}-api-canales-5xx: aplicar infra/apps (HU-W28)."

jq --arg rol "$(jq -r .fis_role_arn <<<"${exp}")" \
  --arg privada "$(jq -r '.zone_subnet_arns[0]' <<<"${exp}")" \
  --arg datos "$(jq -r '.zone_subnet_arns[1]' <<<"${exp}")" \
  --arg db "$(jq -r .db_instance_arn <<<"${exp}")" \
  --arg alarma "${alarma}" --arg prefijo "${prefijo}" --arg ambiente "${ENV}" '
  .roleArn = $rol
  | .stopConditions[0].value = $alarma
  | .targets.SubredesZonaA.resourceArns = [$privada, $datos]
  | .targets.BasePrincipal.resourceArns = [$db]
  | .tags.Name = "\($prefijo)-hu-w29-multizona" | .tags.Environment = $ambiente
' "${EXPERIMENTO_DIR}/fis.json" >"${RESULTADOS}/fis.json"

! grep -q '\${' "${RESULTADOS}/fis.json" || die "Quedaron variables sin llenar en ${RESULTADOS}/fis.json."
[ "$(jq -r .db_multi_az <<<"${exp}")" = "true" ] || die "RDS no es Multi-AZ: high_availability = true antes del experimento."

log "Plantilla lista: ${RESULTADOS}/fis.json (zona $(jq -r .zone <<<"${exp}"))"
cat <<EOF
Siguientes pasos (cada uno con confirmación):
  aws fis create-experiment-template --cli-input-json file://${RESULTADOS}/fis.json \\
    --query experimentTemplate.id --output text > ${RESULTADOS}/plantilla.txt
  aws fis start-experiment --experiment-template-id \$(cat ${RESULTADOS}/plantilla.txt) \\
    --tags Historia=HU-W29 --query experiment.id --output text > ${RESULTADOS}/experimento.txt
EOF
