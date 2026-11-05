# AWS WAF regional delante de ambos APIs (nodo "API Gateway + WAF" de la
# Entrega 8). Configuración mínima: un Web ACL con tres reglas.
#   1. Límite por IP (protege cuotas y capacidad ante abuso), salvo la telemetría.
#   2. Reglas comunes administradas por AWS (OWASP básico).
#   3. Límite por IP propio de la entrada de telemetría (HU-S03): el aliado envía
#      ráfagas desde pocas IP y no debe agotar el límite de los canales. Se compara
#      el final de la ruta porque la URL execute-api antepone el stage (/v1/...).
resource "aws_wafv2_web_acl" "api" {
  name  = "${local.prefix}-api"
  scope = "REGIONAL"

  default_action {
    allow {}
  }

  rule {
    name     = "rate-limit-ip"
    priority = 0
    action {
      block {}
    }
    statement {
      rate_based_statement {
        limit              = var.waf_rate_limit
        aggregate_key_type = "IP"
        scope_down_statement {
          not_statement {
            statement {
              byte_match_statement {
                search_string         = "/telemetria/v1/eventos"
                positional_constraint = "ENDS_WITH"
                field_to_match {
                  uri_path {}
                }
                text_transformation {
                  priority = 0
                  type     = "LOWERCASE"
                }
              }
            }
          }
        }
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.prefix}-rate-limit-ip"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "aws-common"
    priority = 1
    override_action {
      none {}
    }
    statement {
      managed_rule_group_statement {
        vendor_name = "AWS"
        name        = "AWSManagedRulesCommonRuleSet"
        # La selfie y la prueba de vida (HU-M06) superan el límite de 8 KB del cuerpo.
        rule_action_override {
          name = "SizeRestrictions_BODY"
          action_to_use {
            count {}
          }
        }
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.prefix}-aws-common"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "rate-limit-telemetria"
    priority = 2
    action {
      block {}
    }
    statement {
      rate_based_statement {
        limit              = var.waf_telemetria_rate_limit
        aggregate_key_type = "IP"
        scope_down_statement {
          byte_match_statement {
            search_string         = "/telemetria/v1/eventos"
            positional_constraint = "ENDS_WITH"
            field_to_match {
              uri_path {}
            }
            text_transformation {
              priority = 0
              type     = "LOWERCASE"
            }
          }
        }
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.prefix}-rate-limit-telemetria"
      sampled_requests_enabled   = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "${local.prefix}-api"
    sampled_requests_enabled   = true
  }
}

resource "aws_wafv2_web_acl_association" "api" {
  for_each     = local.apis
  resource_arn = aws_api_gateway_stage.main[each.key].arn
  web_acl_arn  = aws_wafv2_web_acl.api.arn
}
