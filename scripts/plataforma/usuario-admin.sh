#!/usr/bin/env bash
# Crea (o completa) un usuario del back-office en Cognito y le asigna grupos.
# Sirve para el primer administrador: los siguientes se crean desde el portal
# (Administración › Usuarios). Cognito envía al correo la contraseña temporal; en
# el primer ingreso el usuario la cambia y registra su aplicación de autenticación.
#
#   EMAIL=ana@solventa.co NOMBRE="Ana Pérez" make infra-usuario-admin
#   GRUPOS=operacion EMAIL=... make infra-usuario-admin     (por defecto administradores,operacion)
#   Si el usuario no ha ingresado aún, volver a ejecutarlo reenvía la contraseña temporal.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require terraform aws jq
require_env

EMAIL="${EMAIL:-}"
NOMBRE="${NOMBRE:-}"
GRUPOS="${GRUPOS:-administradores,operacion}"
[ -n "${EMAIL}" ] || die "Indique EMAIL=correo@dominio (y opcionalmente NOMBRE=\"Nombre Apellido\")."
[[ "${EMAIL}" =~ ^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$ ]] || die "EMAIL no tiene formato de correo."

tf_init platform
pool=$(platform_output platform | jq -r '.cognito.backoffice_pool_id')
[ -n "${pool}" ] && [ "${pool}" != "null" ] || die "No se encontró el pool del back-office (¿make infra-plataforma?)."

log "Usuario ${EMAIL} en el pool ${pool} (${ENV})"
if estado=$(aws cognito-idp admin-get-user --user-pool-id "${pool}" --username "${EMAIL}" \
  --query UserStatus --output text 2>/dev/null); then
  if [ "${estado}" = "FORCE_CHANGE_PASSWORD" ] || [ "${REENVIAR:-0}" = "1" ]; then
    # Sin primer ingreso todavía: se genera otra contraseña temporal y se reenvía el correo.
    aws cognito-idp admin-create-user --user-pool-id "${pool}" --username "${EMAIL}" \
      --message-action RESEND --desired-delivery-mediums EMAIL >/dev/null
    echo "  ya existía (${estado}); se reenvió la contraseña temporal a ${EMAIL}"
  else
    warn "El usuario ya existe (${estado}); solo se asignan los grupos."
  fi
else
  # JSON con jq: un nombre con comas o espacios no rompe la sintaxis abreviada del CLI.
  atributos=$(jq -nc --arg email "${EMAIL}" --arg nombre "${NOMBRE}" \
    '[{Name: "email", Value: $email}, {Name: "email_verified", Value: "true"}]
     + (if $nombre == "" then [] else [{Name: "name", Value: $nombre}] end)')
  aws cognito-idp admin-create-user --user-pool-id "${pool}" --username "${EMAIL}" \
    --user-attributes "${atributos}" --desired-delivery-mediums EMAIL >/dev/null
  echo "  creado; Cognito envió la contraseña temporal a ${EMAIL}"
fi

IFS=', ' read -r -a grupos <<<"${GRUPOS}"
for grupo in "${grupos[@]}"; do
  aws cognito-idp admin-add-user-to-group --user-pool-id "${pool}" --username "${EMAIL}" --group-name "${grupo}"
  echo "  grupo ${grupo}"
done

portal=$(terraform -chdir="${INFRA_DIR}/platform" output -json web 2>/dev/null | jq -r '.url // empty')
log "Listo. Ingreso: ${portal:-<url del portal>}/ingresar"
echo "  El correo llega desde no-reply@verificationemail.com (revisar spam); Cognito permite 50 correos al día por cuenta."
