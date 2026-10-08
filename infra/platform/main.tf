terraform {
  required_version = ">= 1.10.0, < 2.0.0"
  # Estado remoto en S3; bucket, key y región los entrega scripts/plataforma/lib.sh
  # (key = <ambiente>/platform.tfstate). Ver infra/DESPLIEGUE.md.
  backend "s3" {}
  required_providers {
    aws    = { source = "hashicorp/aws", version = "~> 6.0" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
  }
}

provider "aws" {
  region = var.aws_region
  default_tags {
    tags = {
      Project     = var.name
      Environment = var.environment
      ManagedBy   = "Terraform"
      Stack       = "platform"
    }
  }
}

data "aws_availability_zones" "available" {
  state = "available"
}

data "aws_partition" "current" {}

data "aws_caller_identity" "current" {}

locals {
  prefix     = "${var.name}-${var.environment}"
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition
  azs        = slice(data.aws_availability_zones.available.names, 0, 2)
}
