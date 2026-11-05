# Ambiente exp (HU-S03 AC4) con su archivo versionado. Se ejecuta aparte, desde infra-validar:
#   terraform -chdir=infra/platform test -test-directory=tests/exp -var-file=../envs/exp/platform.tfvars
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


run "ambiente_exp" {
  command = plan

  assert {
    condition     = local.prefix == "solventa-exp" && aws_vpc.main.cidr_block == "10.61.0.0/16"
    error_message = "exp tiene prefijo y VPC propios: no comparte recursos con int."
  }
  assert {
    condition = (
      aws_db_instance.main.instance_class == "db.t4g.small" && !aws_db_instance.main.deletion_protection &&
      aws_db_instance.main.skip_final_snapshot && length(aws_nat_gateway.main) == (var.high_availability ? 2 : 1) &&
      aws_db_instance.main.multi_az == var.high_availability
    )
    error_message = "AC4: RDS mayor que int, destruible sin pasos manuales, NAT Gateway (uno por zona con high_availability) y Multi-AZ según high_availability."
  }
  assert {
    condition = (
      aws_s3_bucket.policy_documents.force_destroy && aws_s3_bucket.evidence.force_destroy && aws_s3_bucket.audit.force_destroy &&
      aws_s3_bucket_object_lock_configuration.audit.rule[0].default_retention[0].mode == "GOVERNANCE"
    )
    error_message = "AC4: los buckets se destruyen con objetos y la auditoría usa GOVERNANCE para poder vaciarla."
  }
  assert {
    condition     = contains(keys(aws_api_gateway_usage_plan.partner), "experimento") && var.waf_rate_limit >= 50000 && var.waf_telemetria_rate_limit >= 200000
    error_message = "Plan de uso experimento y límites del WAF amplios para la carga."
  }
}
