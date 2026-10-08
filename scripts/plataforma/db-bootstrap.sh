#!/usr/bin/env bash
# Paso 2: crea (idempotente) una base y un usuario por servicio en RDS con la
# tarea puntual db-bootstrap y espera su resultado.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require terraform aws jq
require_env
tf_init platform

task="$(platform_output db_bootstrap_task)"
cluster=$(jq -r .cluster <<<"${task}")
subnets=$(jq -r '.subnets | join(",")' <<<"${task}")
security_group=$(jq -r .security_group <<<"${task}")
log_group=$(jq -r .log_group <<<"${task}")

log "Preparando bases por servicio (${ENV})"
task_arn=$(aws ecs run-task --cluster "${cluster}" \
  --task-definition "$(jq -r .task_definition <<<"${task}")" \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[${subnets}],securityGroups=[${security_group}],assignPublicIp=DISABLED}" \
  --query 'tasks[0].taskArn' --output text)
[ -n "${task_arn}" ] && [ "${task_arn}" != "None" ] || die "No se pudo iniciar la tarea db-bootstrap."

echo "Tarea ${task_arn##*/}; esperando a que termine..."
aws ecs wait tasks-stopped --cluster "${cluster}" --tasks "${task_arn}"

result=$(aws ecs describe-tasks --cluster "${cluster}" --tasks "${task_arn}" \
  --query 'tasks[0].{exit:containers[0].exitCode,reason:stoppedReason}' --output json)
aws logs tail "${log_group}" --since 15m --format short 2>/dev/null | tail -20 || true

[ "$(jq -r .exit <<<"${result}")" = "0" ] ||
  die "db-bootstrap falló: $(jq -r '.reason' <<<"${result}"). Revisar el log group ${log_group}."
log "Bases listas"
