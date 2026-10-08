#!/usr/bin/env bash
# Prueba rápida del ambiente desplegado (no requiere lógica de negocio: usa
# GET /health de los servicios). Verifica de punta a punta:
#   1. Servicios ECS desplegados con todas sus tareas corriendo.
#   2. Destinos saludables en el ALB interno (servicios de acceso).
#   3. API de canales: sin token o con token inválido -> 401, con X-Correlation-Id.
#   4. API de socios con socio de prueba (client_credentials + API key):
#      token válido -> 200 desde api-socios; sin API key -> rechazo en el borde.
#   5. Opcional (SMOKE_ALERTA=1): mensaje de prueba al tópico de alertas.
# Sale con 1 si algún chequeo falla.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require terraform aws jq curl
require_env
export_apps_vars

pass=0
fail=0
ok() { printf '  \033[32mOK\033[0m   %s\n' "$*"; pass=$((pass + 1)); }
ko() { printf '  \033[31mFALLA\033[0m %s\n' "$*"; fail=$((fail + 1)); }
skip() { printf '  \033[33mOMITE\033[0m %s\n' "$*"; }
# check <éxito> <mensaje si pasa> <mensaje si falla>; éxito: "true" o "false".
check() { if [ "$1" = "true" ]; then ok "$2"; else ko "$3"; fi; }
# is <valor> <regex>: "true" si el valor coincide con la expresión.
is() { if [[ "$1" =~ $2 ]]; then echo true; else echo false; fi; }

# http_code <args de curl...>: imprime el código HTTP (000 si no hubo respuesta).
http_code() { curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$@" || true; }

tf_init platform
platform="$(platform_output platform)"
api_urls="$(platform_output api_urls)"
canales=$(jq -r '.canales' <<<"${api_urls}")
socios=$(jq -r '.socios_mtls // .socios' <<<"${api_urls}")

tf_init apps
deployed="$(terraform -chdir="${INFRA_DIR}/apps" output -json deployed_services 2>/dev/null || echo '{}')"
cluster=$(jq -r '.cluster.name' <<<"${platform}")

log "1. Servicios ECS (${ENV})"
if [ "$(jq 'length' <<<"${deployed}")" = "0" ]; then
  skip "No hay servicios desplegados (make infra-aplicaciones)."
else
  mapfile -t names < <(jq -r '.[].service_name' <<<"${deployed}")
  for ((i = 0; i < ${#names[@]}; i += 10)); do
    while read -r name running desired; do
      if [ "${running}" = "${desired}" ] && [ "${desired}" -gt 0 ]; then
        ok "${name}: ${running}/${desired} tareas"
      else
        ko "${name}: ${running}/${desired} tareas"
      fi
    done < <(aws ecs describe-services --cluster "${cluster}" --services "${names[@]:i:10}" \
      --query 'services[].[serviceName,runningCount,desiredCount]' --output text)
  done
fi

log "2. Destinos del ALB interno"
for service in $(jq -r --argjson d "${deployed}" '.target_groups | keys[] | select(. as $k | $d | has($k))' <<<"${platform}"); do
  target_group=$(jq -r --arg s "${service}" '.target_groups[$s].arn' <<<"${platform}")
  # shellcheck disable=SC2016 # comillas invertidas: literal JMESPath, no expansión.
  healthy=$(aws elbv2 describe-target-health --target-group-arn "${target_group}" \
    --query 'length(TargetHealthDescriptions[?TargetHealth.State==`healthy`])' --output text)
  check "$(is "${healthy}" '^[1-9][0-9]*$')" "${service}: ${healthy} destino(s) saludable(s)" "${service}: sin destinos saludables"
done

log "3. API de canales (${canales})"
headers_file=$(mktemp)
trap 'rm -f "${headers_file}"' EXIT
code=$(http_code -D "${headers_file}" "${canales}/web/health")
headers=$(tr -d '\r' <"${headers_file}")
check "$(is "${code}" '^401$')" "sin token -> 401" "sin token -> ${code:-sin respuesta} (esperado 401)"
check "$(is "${headers,,}" 'x-correlation-id:')" "la respuesta del borde incluye X-Correlation-Id" "falta X-Correlation-Id en la respuesta del borde"
code=$(http_code -H "Authorization: Bearer token-invalido" "${canales}/movil/health")
check "$(is "${code}" '^401$')" "token inválido -> 401" "token inválido -> ${code} (esperado 401)"

log "4. API de socios (${socios})"
partner=$(terraform -chdir="${INFRA_DIR}/platform" output -json partners 2>/dev/null |
  jq -c 'to_entries | map(select(.value.enabled and .value.client_id != null)) | first // empty')
if [ -z "${partner}" ]; then
  skip "No hay socios de prueba habilitados (partners en infra/envs/${ENV}/platform.tfvars)."
else
  name=$(jq -r '.key' <<<"${partner}")
  token_url=$(platform_output cognito | jq -r '.partners.token_url')
  token=$(curl -s --max-time 20 -u "$(jq -r '.value.client_id' <<<"${partner}"):$(jq -r '.value.client_secret' <<<"${partner}")" \
    -d grant_type=client_credentials "${token_url}" | jq -r '.access_token // empty')
  if [ -z "${token}" ]; then
    ko "${name}: no se obtuvo token client_credentials de Cognito"
  else
    ok "${name}: token client_credentials obtenido"
    api_key=$(jq -r '.value.api_key' <<<"${partner}")
    code=$(http_code -H "Authorization: Bearer ${token}" "${socios}/health")
    check "$(is "${code}" '^40[13]$')" "token sin API key -> ${code} (rechazado en el borde)" "token sin API key -> ${code} (esperado 401/403)"
    if jq -e 'has("api-socios")' <<<"${deployed}" >/dev/null; then
      code=$(http_code -H "Authorization: Bearer ${token}" -H "x-api-key: ${api_key}" "${socios}/health")
      check "$(is "${code}" '^200$')" "token + API key -> 200 desde api-socios (WAF, VPC Link, ALB y ECS)" \
        "token + API key -> ${code} (esperado 200)"
    else
      skip "api-socios no está desplegado; no se prueba el recorrido completo."
    fi
  fi
fi

if [ "${SMOKE_ALERTA:-0}" = "1" ]; then
  log "5. Canal de alertas"
  topic=$(jq -r '.alerts_topic_arn' <<<"${platform}")
  if aws sns publish --topic-arn "${topic}" --subject "[${ENV}] Prueba del canal de alertas" \
    --message "Mensaje de prueba de make infra-probar. No requiere acción." >/dev/null; then
    ok "mensaje publicado en el tópico de alertas (verificar el correo)"
  else
    ko "no se pudo publicar en el tópico de alertas"
  fi
fi

log "Resultado: ${pass} OK, ${fail} con falla"
[ "${fail}" -eq 0 ]
