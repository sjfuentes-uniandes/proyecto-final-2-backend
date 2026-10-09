terraform {
  required_version = ">= 1.10.0, < 2.0.0"
  # Estado remoto en S3 (key = <ambiente>/apps.tfstate). Ver infra/DESPLIEGUE.md.
  backend "s3" {}
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
}

data "terraform_remote_state" "platform" {
  backend = "s3"
  config = {
    bucket = var.state_bucket
    key    = "${var.environment}/platform.tfstate"
    region = var.state_region
  }
}

locals {
  p         = data.terraform_remote_state.platform.outputs.platform
  prefix    = local.p.prefix
  partition = data.aws_partition.current.partition

  # Solo se despliegan los servicios con imagen publicada. Así cada historia
  # puede habilitar su servicio cuando su imagen exista, sin tocar platform.
  deployed = { for name, service in local.p.catalog : name => service if contains(keys(var.image_digests), name) }

  sizing = {
    for name in keys(local.deployed) : name => merge(
      { cpu = var.task_cpu, memory = var.task_memory, min = var.min_replicas, max = var.max_replicas },
      { for key, value in try(var.service_overrides[name], {}) : key => value if value != null },
      var.paused ? { min = 0, max = 0 } : {}
    )
  }
}

provider "aws" {
  region              = local.p.region
  allowed_account_ids = [local.p.account_id]
  default_tags {
    tags = {
      Project     = local.p.name
      Environment = local.p.environment
      ManagedBy   = "Terraform"
      Stack       = "apps"
    }
  }
}

data "aws_partition" "current" {}

# Falla en plan si un digest no está publicado en su repositorio.
data "aws_ecr_image" "service" {
  for_each        = local.deployed
  repository_name = each.value.ecr.name
  image_digest    = var.image_digests[each.key]
}
