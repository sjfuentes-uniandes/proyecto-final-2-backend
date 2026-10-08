# Auditoría inmutable de decisiones de identidad y consentimiento (HU-W31,
# HU-M08, HU-M09): versionado + Object Lock con retención por defecto.
locals {
  audit_sse = var.use_customer_managed_key ? "aws:kms" : "AES256"
}

resource "aws_s3_bucket" "audit" {
  bucket              = "${local.prefix}-auditoria-${local.account_id}"
  object_lock_enabled = true
  force_destroy       = var.audit_lock_mode == "GOVERNANCE"
}

resource "aws_s3_bucket_versioning" "audit" {
  bucket = aws_s3_bucket.audit.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_object_lock_configuration" "audit" {
  bucket = aws_s3_bucket.audit.id
  rule {
    default_retention {
      mode = var.audit_lock_mode
      days = var.audit_retention_days
    }
  }
  depends_on = [aws_s3_bucket_versioning.audit]
}

resource "aws_s3_bucket_server_side_encryption_configuration" "audit" {
  bucket = aws_s3_bucket.audit.id
  rule {
    bucket_key_enabled = true
    apply_server_side_encryption_by_default {
      sse_algorithm     = local.audit_sse
      kms_master_key_id = local.kms_key_arn
    }
  }
}

resource "aws_s3_bucket_public_access_block" "audit" {
  bucket                  = aws_s3_bucket.audit.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "audit" {
  bucket = aws_s3_bucket.audit.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_policy" "audit" {
  bucket = aws_s3_bucket.audit.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource  = [aws_s3_bucket.audit.arn, "${aws_s3_bucket.audit.arn}/*"]
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      },
      {
        Sid       = "DenyUnencryptedWrites"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:PutObject"
        Resource  = "${aws_s3_bucket.audit.arn}/*"
        Condition = { StringNotEquals = { "s3:x-amz-server-side-encryption" = local.audit_sse }, Null = { "s3:x-amz-server-side-encryption" = "false" } }
      }
    ]
  })
  depends_on = [aws_s3_bucket_public_access_block.audit]
}
