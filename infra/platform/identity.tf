# OAuth2/OIDC con Amazon Cognito. Tres directorios separados:
#   clientes   -> portal web y aplicación móvil (HU-M07, HU-W30, HU-M08/M09)
#   backoffice -> administradores de Solventa (HU-W01, HU-W02)
#   socios     -> credenciales máquina a máquina por socio (HU-W01)
# El API Gateway valida los tokens antes de llegar a los BFF.

locals {
  cognito_domain_suffix = substr(sha1("${local.account_id}-${var.aws_region}"), 0, 8)
  portal_url            = "https://${aws_cloudfront_distribution.web.domain_name}"
  web_callback_urls     = concat(["https://${aws_cloudfront_distribution.web.domain_name}/auth/callback"], var.web_callback_urls)
  web_logout_urls       = concat(["https://${aws_cloudfront_distribution.web.domain_name}/"], [for url in var.web_callback_urls : replace(url, "/auth/callback", "/")])
}

# --- Clientes ---------------------------------------------------------------
resource "aws_cognito_user_pool" "customers" {
  name                     = "${local.prefix}-clientes"
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]
  deletion_protection      = var.db_deletion_protection ? "ACTIVE" : "INACTIVE"
  mfa_configuration        = "OPTIONAL"

  software_token_mfa_configuration {
    enabled = true
  }

  password_policy {
    minimum_length                   = 10
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = false
    temporary_password_validity_days = 3
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }

  # Enlaza la identidad Cognito con el cliente verificado en el servicio clientes.
  schema {
    name                     = "customer_id"
    attribute_data_type      = "String"
    mutable                  = true
    developer_only_attribute = false
    string_attribute_constraints {
      min_length = 1
      max_length = 64
    }
  }
}

resource "aws_cognito_user_pool_domain" "customers" {
  domain       = "${local.prefix}-clientes-${local.cognito_domain_suffix}"
  user_pool_id = aws_cognito_user_pool.customers.id
}

resource "aws_cognito_user_pool_client" "web" {
  name                                 = "portal-web"
  user_pool_id                         = aws_cognito_user_pool.customers.id
  generate_secret                      = false
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email", "profile"]
  supported_identity_providers         = ["COGNITO"]
  callback_urls                        = local.web_callback_urls
  logout_urls                          = local.web_logout_urls
  explicit_auth_flows                  = ["ALLOW_USER_SRP_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]
  prevent_user_existence_errors        = "ENABLED"
  enable_token_revocation              = true
  access_token_validity                = 60
  id_token_validity                    = 60
  refresh_token_validity               = 1
  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "days"
  }
}

# La biometría del dispositivo desbloquea el refresh token guardado en el
# almacén seguro; al revocarlo (HU-M07 AC4) el servidor exige autenticación completa.
resource "aws_cognito_user_pool_client" "mobile" {
  name                                 = "app-movil"
  user_pool_id                         = aws_cognito_user_pool.customers.id
  generate_secret                      = false
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email", "profile"]
  supported_identity_providers         = ["COGNITO"]
  callback_urls                        = var.mobile_callback_urls
  logout_urls                          = var.mobile_callback_urls
  explicit_auth_flows                  = ["ALLOW_USER_SRP_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]
  prevent_user_existence_errors        = "ENABLED"
  enable_token_revocation              = true
  access_token_validity                = 30
  id_token_validity                    = 30
  refresh_token_validity               = 30
  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "days"
  }
}

# --- Back-office --------------------------------------------------------------
resource "aws_cognito_user_pool" "backoffice" {
  name                     = "${local.prefix}-backoffice"
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]
  deletion_protection      = var.db_deletion_protection ? "ACTIVE" : "INACTIVE"
  mfa_configuration        = "ON"

  software_token_mfa_configuration {
    enabled = true
  }

  # Los usuarios los crea un administrador (portal: Administración › Usuarios, o
  # make infra-usuario-admin para el primero). Cognito envía la contraseña temporal.
  admin_create_user_config {
    allow_admin_create_user_only = true
    invite_message_template {
      email_subject = "Acceso al portal de gestión de Solventa"
      email_message = "Se creó su acceso al portal de gestión de Solventa (${local.portal_url}/ingresar). Usuario: {username}. Contraseña temporal: {####}. Al ingresar deberá cambiarla y registrar una aplicación de autenticación."
      sms_message   = "Solventa: usuario {username}, contraseña temporal {####}"
    }
  }

  password_policy {
    minimum_length                   = 12
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = true
    temporary_password_validity_days = 3
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }
}

resource "aws_cognito_user_group" "backoffice" {
  for_each = {
    administradores        = "Crea usuarios del back-office y asigna sus grupos"
    administradores-socios = "Gestiona socios, credenciales y cuotas"
    operacion              = "Consulta tableros y alertas"
  }
  name         = each.key
  description  = each.value
  user_pool_id = aws_cognito_user_pool.backoffice.id
}

resource "aws_cognito_user_pool_domain" "backoffice" {
  domain       = "${local.prefix}-backoffice-${local.cognito_domain_suffix}"
  user_pool_id = aws_cognito_user_pool.backoffice.id
}

resource "aws_cognito_user_pool_client" "backoffice" {
  name                                 = "backoffice-web"
  user_pool_id                         = aws_cognito_user_pool.backoffice.id
  generate_secret                      = false
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email", "profile"]
  supported_identity_providers         = ["COGNITO"]
  callback_urls                        = local.web_callback_urls
  logout_urls                          = local.web_logout_urls
  explicit_auth_flows                  = ["ALLOW_USER_SRP_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]
  prevent_user_existence_errors        = "ENABLED"
  enable_token_revocation              = true
  # Minutos para responder los retos del ingreso (por defecto 3): el primer ingreso
  # cambia la contraseña y registra la aplicación TOTP con el QR en la misma sesión.
  auth_session_validity  = 15
  access_token_validity  = 30
  id_token_validity      = 30
  refresh_token_validity = 12
  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "hours"
  }
}

# --- Socios (client_credentials) ------------------------------------------------
resource "aws_cognito_user_pool" "partners" {
  name                = "${local.prefix}-socios"
  deletion_protection = var.db_deletion_protection ? "ACTIVE" : "INACTIVE"
  admin_create_user_config {
    allow_admin_create_user_only = true
  }
}

resource "aws_cognito_user_pool_domain" "partners" {
  domain       = "${local.prefix}-socios-${local.cognito_domain_suffix}"
  user_pool_id = aws_cognito_user_pool.partners.id
}

resource "aws_cognito_resource_server" "partners" {
  identifier   = "solventa"
  name         = "API de socios de Solventa"
  user_pool_id = aws_cognito_user_pool.partners.id
  dynamic "scope" {
    for_each = var.partner_scopes
    content {
      scope_name        = scope.key
      scope_description = scope.value
    }
  }
}

locals {
  partner_scope_ids = [for scope in keys(var.partner_scopes) : "${aws_cognito_resource_server.partners.identifier}/${scope}"]
}

# Socios de prueba. clientId único + secreto generado por Cognito (HU-W01 AC1);
# desactivar uno elimina su cliente y su API key sin tocar a los demás (AC3).
resource "aws_cognito_user_pool_client" "partner" {
  for_each                             = { for name, partner in var.partners : name => partner if partner.enabled }
  name                                 = "socio-${each.key}"
  user_pool_id                         = aws_cognito_user_pool.partners.id
  generate_secret                      = true
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["client_credentials"]
  allowed_oauth_scopes                 = [for scope in each.value.scopes : "${aws_cognito_resource_server.partners.identifier}/${scope}"]
  supported_identity_providers         = ["COGNITO"]
  enable_token_revocation              = true
  access_token_validity                = 60
  token_validity_units {
    access_token = "minutes"
  }
  lifecycle {
    precondition {
      condition     = alltrue([for scope in each.value.scopes : contains(keys(var.partner_scopes), scope)])
      error_message = "El socio ${each.key} usa un permiso que no existe en partner_scopes."
    }
  }
}
