# AWS WAF regional delante de ambos APIs (nodo "API Gateway + WAF" de la
# Entrega 8). Configuración mínima: un Web ACL con dos reglas.
#   1. Límite por IP (protege cuotas y capacidad ante abuso).
#   2. Reglas comunes administradas por AWS (OWASP básico).
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
