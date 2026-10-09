# Portal web (Angular) y back-office: sitio estático privado en S3 servido por
# CloudFront con Origin Access Control. Las APIs no pasan por aquí: el portal
# llama al API de canales en API Gateway.
resource "aws_s3_bucket" "web" {
  bucket        = "${local.prefix}-web-${local.account_id}"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "web" {
  bucket                  = aws_s3_bucket.web.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "web" {
  bucket = aws_s3_bucket.web.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "web" {
  bucket = aws_s3_bucket.web.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_cloudfront_origin_access_control" "web" {
  name                              = "${local.prefix}-web"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

locals {
  # Políticas administradas de CloudFront.
  cache_policy_optimized   = "658327ea-f89d-4fab-a63d-7e88639e58f6"
  response_security_policy = "67f7725c-6f97-4210-82d7-5512b31e9d03"
}

resource "aws_cloudfront_distribution" "web" {
  enabled             = true
  comment             = "${local.prefix} portal web"
  default_root_object = "index.html"
  price_class         = "PriceClass_100"
  http_version        = "http2and3"

  origin {
    origin_id                = "web"
    domain_name              = aws_s3_bucket.web.bucket_regional_domain_name
    origin_access_control_id = aws_cloudfront_origin_access_control.web.id
  }

  default_cache_behavior {
    target_origin_id           = "web"
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD"]
    cached_methods             = ["GET", "HEAD"]
    compress                   = true
    cache_policy_id            = local.cache_policy_optimized
    response_headers_policy_id = local.response_security_policy
  }

  # Enrutamiento del SPA: las rutas del cliente devuelven index.html.
  dynamic "custom_error_response" {
    for_each = [403, 404]
    content {
      error_code            = custom_error_response.value
      response_code         = 200
      response_page_path    = "/index.html"
      error_caching_min_ttl = 10
    }
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }
}

resource "aws_s3_bucket_policy" "web" {
  bucket = aws_s3_bucket.web.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "CloudFrontRead"
      Effect    = "Allow"
      Principal = { Service = "cloudfront.amazonaws.com" }
      Action    = "s3:GetObject"
      Resource  = "${aws_s3_bucket.web.arn}/*"
      Condition = { StringEquals = { "AWS:SourceArn" = aws_cloudfront_distribution.web.arn } }
    }]
  })
  depends_on = [aws_s3_bucket_public_access_block.web]
}

# Configuración pública del portal (sin secretos). La lee el despliegue del frontend
# (repo proyecto-final-2-frontend: make desplegar / CD web) para generar config.json,
# sin acceso al estado de Terraform.
resource "aws_ssm_parameter" "web_config" {
  name        = "/${var.name}/${var.environment}/web/config"
  description = "Bucket, distribución y configuración pública del portal web"
  type        = "String"
  value = jsonencode({
    bucket         = aws_s3_bucket.web.id
    distributionId = aws_cloudfront_distribution.web.id
    url            = local.portal_url
    config = {
      apiBaseUrl   = aws_api_gateway_stage.main["canales"].invoke_url
      dashboardUrl = "https://${var.aws_region}.console.aws.amazon.com/cloudwatch/home?region=${var.aws_region}#dashboards/dashboard/${local.prefix}"
      cognito = {
        region     = var.aws_region
        backoffice = { userPoolId = aws_cognito_user_pool.backoffice.id, clientId = aws_cognito_user_pool_client.backoffice.id }
        clientes   = { userPoolId = aws_cognito_user_pool.customers.id, clientId = aws_cognito_user_pool_client.web.id }
      }
    }
  })
}
