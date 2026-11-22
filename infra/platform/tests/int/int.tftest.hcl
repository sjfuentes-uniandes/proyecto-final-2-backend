# Ambiente int (HU-W25) con su archivo versionado y el mismo fixture que tests/exp.
# Solo afirma lo que cambia HU-W25 (Container Insights y su salida); nunca fija otros valores
# de int, para no romper los cambios de otros en envs/int/platform.tfvars.
# Se ejecuta aparte, desde infra-validar (infra/plataforma.mk):
#   terraform -chdir=infra/platform test -test-directory=tests/int -var-file=../envs/int/platform.tfvars
mock_provider "aws" {
  mock_data "aws_availability_zones" {
    defaults = { names = ["us-east-1a", "us-east-1b", "us-east-1c"] }
  }
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  mock_data "aws_ssm_parameter" {
    defaults = { value = "ami-0123456789abcdef0" }
  }
  mock_resource "aws_db_instance" {
    defaults = {
      address            = "solventa-int.abc.us-east-1.rds.amazonaws.com"
      port               = 5432
      master_user_secret = [{ secret_arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:rds-master", kms_key_id = "k", secret_status = "active" }]
    }
  }
  mock_resource "aws_kms_key" {
    defaults = { arn = "arn:aws:kms:us-east-1:123456789012:key/0000" }
  }
  mock_resource "aws_cognito_user_pool" {
    defaults = { arn = "arn:aws:cognito-idp:us-east-1:123456789012:userpool/us-east-1_x" }
  }
  mock_resource "aws_sqs_queue" {
    defaults = { arn = "arn:aws:sqs:us-east-1:123456789012:q" }
  }
  mock_resource "aws_sns_topic" {
    defaults = { arn = "arn:aws:sns:us-east-1:123456789012:t" }
  }
  mock_resource "aws_s3_bucket" {
    defaults = { arn = "arn:aws:s3:::b" }
  }
  mock_resource "aws_cloudwatch_log_group" {
    defaults = { arn = "arn:aws:logs:us-east-1:123456789012:log-group:g" }
  }
  mock_resource "aws_cloudfront_distribution" {
    defaults = { arn = "arn:aws:cloudfront::123456789012:distribution/E1", domain_name = "d111.cloudfront.net" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/r" }
  }
  mock_resource "aws_lb" {
    override_during = plan
    defaults        = { arn = "arn:aws:elasticloadbalancing:us-east-1:123456789012:loadbalancer/net/a/1", dns_name = "access.elb.amazonaws.com" }
  }
  mock_resource "aws_api_gateway_stage" {
    defaults = { arn = "arn:aws:apigateway:us-east-1::/restapis/a/stages/v1" }
  }
  mock_resource "aws_wafv2_web_acl" {
    defaults = { arn = "arn:aws:wafv2:us-east-1:123456789012:regional/webacl/a/1" }
  }
}

# HU-W25 (25.1): int publica RunningTaskCount para el escalado por tarea de pagos-parametricos.
run "ambiente_int" {
  command = plan

  assert {
    condition     = [for s in aws_ecs_cluster.main.setting : s.value if s.name == "containerInsights"] == ["enabled"]
    error_message = "HU-W25: en int, el clúster queda con containerInsights = enabled (envs/int/platform.tfvars)."
  }
  assert {
    condition     = output.platform.container_insights == true
    error_message = "HU-W25: en int, la salida platform.container_insights es true."
  }
}
