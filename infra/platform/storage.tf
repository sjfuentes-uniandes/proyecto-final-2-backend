# Auditoría inmutable de decisiones de identidad y consentimiento (HU-W31,
# HU-M08, HU-M09): versionado + Object Lock con retención por defecto.
locals {
  # Cifrado de los buckets de la plataforma (auditoría y documentos de póliza).
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

# Documentos de póliza firmados (HU-W17, HU-M10). Solo `polizas` escribe
# (document_writers en apps); la lectura es por URL prefirmada que firman los
# servicios de document_readers. Versionado para no perder un documento
# reemplazado, cifrado, sin acceso público y solo por TLS 1.2 o superior.
resource "aws_s3_bucket" "policy_documents" {
  bucket        = "${local.prefix}-polizas-${local.account_id}"
  force_destroy = var.policy_documents_force_destroy
}

resource "aws_s3_bucket_versioning" "policy_documents" {
  bucket = aws_s3_bucket.policy_documents.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "policy_documents" {
  bucket = aws_s3_bucket.policy_documents.id
  rule {
    bucket_key_enabled = true
    apply_server_side_encryption_by_default {
      sse_algorithm     = local.audit_sse
      kms_master_key_id = local.kms_key_arn
    }
  }
}

resource "aws_s3_bucket_public_access_block" "policy_documents" {
  bucket                  = aws_s3_bucket.policy_documents.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "policy_documents" {
  bucket = aws_s3_bucket.policy_documents.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "policy_documents" {
  bucket = aws_s3_bucket.policy_documents.id
  rule {
    id     = "versiones-anteriores"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = var.policy_documents_noncurrent_days
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
  depends_on = [aws_s3_bucket_versioning.policy_documents]
}

# Rechaza transporte sin TLS y toda escritura que no declare el cifrado del
# bucket: polizas envía ServerSideEncryption = POLIZAS_BUCKET_SSE.
resource "aws_s3_bucket_policy" "policy_documents" {
  bucket = aws_s3_bucket.policy_documents.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource  = [aws_s3_bucket.policy_documents.arn, "${aws_s3_bucket.policy_documents.arn}/*"]
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      },
      {
        Sid       = "DenyOutdatedTls"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource  = [aws_s3_bucket.policy_documents.arn, "${aws_s3_bucket.policy_documents.arn}/*"]
        Condition = { NumericLessThan = { "s3:TlsVersion" = "1.2" } }
      },
      {
        Sid       = "DenyUnencryptedWrites"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:PutObject"
        Resource  = "${aws_s3_bucket.policy_documents.arn}/*"
        Condition = { Null = { "s3:x-amz-server-side-encryption" = "true" } }
      },
      {
        Sid       = "DenyOtherEncryption"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:PutObject"
        Resource  = "${aws_s3_bucket.policy_documents.arn}/*"
        Condition = { StringNotEquals = { "s3:x-amz-server-side-encryption" = local.audit_sse }, Null = { "s3:x-amz-server-side-encryption" = "false" } }
      }
    ]
  })
  depends_on = [aws_s3_bucket_public_access_block.policy_documents]
}
