# Se aplica UNA vez por cuenta, con credenciales de administrador. Crea lo que
# los ambientes necesitan antes de existir y que no se destruye con ellos:
#   - bucket S3 del estado remoto de Terraform (bloqueo nativo con use_lockfile)
#   - proveedor OIDC de GitHub y rol que asumen los workflows (sin llaves)
# Su propio estado es local (infra/bootstrap/terraform.tfstate, ignorado por Git);
# si se pierde, los recursos se pueden importar de nuevo.
terraform {
  required_version = ">= 1.10.0, < 2.0.0"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
}

provider "aws" {
  region = var.aws_region
  default_tags {
    tags = { Project = var.name, ManagedBy = "Terraform", Stack = "bootstrap" }
  }
}

variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "name" {
  description = "Debe coincidir con NAME del Makefile (prefijo del bucket de estado)."
  type        = string
  default     = "solventa"
}

# GitHub emite el sub del token con los ID del dueño y del repositorio
# (repo:owner@<id>/repo@<id>:...). Fijar los ID evita que otro repositorio con el
# mismo nombre (renombrado o recreado) pueda asumir el rol.
variable "github_repository" {
  description = "owner@<id>/repo@<id> autorizado a asumir el rol de despliegue (tal como aparece en el sub del token)."
  type        = string
  default     = "sjfuentes-uniandes@196879525/proyecto-final-2-backend@1409601103"
}

variable "create_github_oidc_provider" {
  description = "false si la cuenta ya tiene el proveedor token.actions.githubusercontent.com."
  type        = bool
  default     = true
}

data "aws_caller_identity" "current" {}

data "aws_partition" "current" {}

locals {
  state_bucket = "${var.name}-tfstate-${data.aws_caller_identity.current.account_id}"
  oidc_url     = "token.actions.githubusercontent.com"
  oidc_arn     = var.create_github_oidc_provider ? aws_iam_openid_connect_provider.github[0].arn : "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:oidc-provider/${local.oidc_url}"
}

# --- Estado remoto ---------------------------------------------------------------
resource "aws_s3_bucket" "state" {
  bucket = local.state_bucket
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    id     = "versiones-antiguas"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = 90
    }
  }
}

resource "aws_s3_bucket_policy" "state" {
  bucket = aws_s3_bucket.state.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.state.arn, "${aws_s3_bucket.state.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
  depends_on = [aws_s3_bucket_public_access_block.state]
}

# --- GitHub Actions (OIDC) ---------------------------------------------------------
resource "aws_iam_openid_connect_provider" "github" {
  count          = var.create_github_oidc_provider ? 1 : 0
  url            = "https://${local.oidc_url}"
  client_id_list = ["sts.amazonaws.com"]
}

resource "aws_iam_role" "github" {
  name                 = "${var.name}-github-deploy"
  max_session_duration = 7200
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = local.oidc_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = { "${local.oidc_url}:aud" = "sts.amazonaws.com" }
        StringLike   = { "${local.oidc_url}:sub" = "repo:${var.github_repository}:*" }
      }
    }]
  })
}

# Terraform crea VPC, IAM, RDS, Cognito, API Gateway, WAF, etc.: el rol necesita
# permisos amplios. Solo lo pueden asumir workflows de este repositorio.
resource "aws_iam_role_policy_attachment" "github" {
  role       = aws_iam_role.github.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/AdministratorAccess"
}

output "state_bucket" {
  value = aws_s3_bucket.state.id
}

output "github_role_arn" {
  description = "Guardar como variable AWS_ROLE_ARN del repositorio en GitHub."
  value       = aws_iam_role.github.arn
}
