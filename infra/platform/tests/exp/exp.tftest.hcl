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

# HU-W29 (N1): dos zonas reales sin punto único de falla en la zona A, salvo la caché
# (descartable), y el rol de AWS FIS limitado a la zona A y a la base del ambiente.
run "multizona_hu_w29" {
  command = plan

  override_resource {
    target          = aws_vpc.main
    override_during = plan
    values          = { id = "vpc-exp", arn = "arn:aws:ec2:us-east-1:123456789012:vpc/vpc-exp" }
  }
  override_resource {
    target          = aws_subnet.private[0]
    override_during = plan
    values          = { id = "subnet-priv-a", arn = "arn:aws:ec2:us-east-1:123456789012:subnet/subnet-priv-a" }
  }
  override_resource {
    target          = aws_subnet.private[1]
    override_during = plan
    values          = { id = "subnet-priv-b", arn = "arn:aws:ec2:us-east-1:123456789012:subnet/subnet-priv-b" }
  }
  override_resource {
    target          = aws_subnet.database[0]
    override_during = plan
    values          = { id = "subnet-db-a", arn = "arn:aws:ec2:us-east-1:123456789012:subnet/subnet-db-a" }
  }
  override_resource {
    target          = aws_subnet.database[1]
    override_during = plan
    values          = { id = "subnet-db-b", arn = "arn:aws:ec2:us-east-1:123456789012:subnet/subnet-db-b" }
  }
  override_resource {
    target          = aws_nat_gateway.main[0]
    override_during = plan
    values          = { id = "nat-a" }
  }
  override_resource {
    target          = aws_nat_gateway.main[1]
    override_during = plan
    values          = { id = "nat-b" }
  }
  override_resource {
    target          = aws_s3_bucket.experiments[0]
    override_during = plan
    values          = { id = "solventa-exp-experimentos-123456789012", arn = "arn:aws:s3:::solventa-exp-experimentos-123456789012" }
  }
  override_resource {
    target          = aws_secretsmanager_secret.service_db
    override_during = plan
    values          = { arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:solventa-exp/db/servicio" }
  }
  override_resource {
    target          = aws_cloudwatch_log_group.reconciliation[0]
    override_during = plan
    values          = { arn = "arn:aws:logs:us-east-1:123456789012:log-group:/ecs/solventa-exp/reconciliacion" }
  }
  override_resource {
    target          = aws_db_instance.main
    override_during = plan
    values = {
      arn                = "arn:aws:rds:us-east-1:123456789012:db:solventa-exp"
      address            = "solventa-exp.abc.us-east-1.rds.amazonaws.com"
      port               = 5432
      master_user_secret = [{ secret_arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:rds-master", kms_key_id = "k", secret_status = "active" }]
    }
  }

  assert {
    condition = (
      var.high_availability && aws_db_instance.main.multi_az && length(aws_nat_gateway.main) == 2 && length(aws_instance.nat) == 0 &&
      aws_route.private_egress[0].nat_gateway_id == "nat-a" && aws_route.private_egress[1].nat_gateway_id == "nat-b"
    )
    error_message = "N1: RDS Multi-AZ y un NAT Gateway por zona; cada subred privada sale por el NAT de su zona."
  }
  assert {
    condition = (
      aws_subnet.private[0].availability_zone != aws_subnet.private[1].availability_zone &&
      aws_subnet.database[0].availability_zone != aws_subnet.database[1].availability_zone &&
      aws_subnet.private[0].availability_zone == "us-east-1a" && aws_subnet.database[0].availability_zone == "us-east-1a"
    )
    error_message = "N1: las subredes privadas y de datos están en dos zonas; la zona A es la que interrumpe FIS."
  }
  assert {
    condition = (
      length(aws_iam_role.fis) == 1 &&
      jsondecode(aws_iam_role.fis[0].assume_role_policy).Statement[0].Principal.Service == "fis.amazonaws.com" &&
      jsondecode(aws_iam_role.fis[0].assume_role_policy).Statement[0].Condition.StringEquals["aws:SourceAccount"] == "123456789012"
    )
    error_message = "Rol de FIS solo para experimentos de la cuenta."
  }
  assert {
    condition = (
      [for s in jsondecode(aws_iam_role_policy.fis[0].policy).Statement : s.Resource if s.Sid == "CambiarAclDeLaZonaA"][0] == [
        "arn:aws:ec2:us-east-1:123456789012:subnet/subnet-priv-a",
        "arn:aws:ec2:us-east-1:123456789012:subnet/subnet-db-a",
        "arn:aws:ec2:us-east-1:123456789012:network-acl/*",
      ] &&
      [for s in jsondecode(aws_iam_role_policy.fis[0].policy).Statement : s.Resource if s.Sid == "FailoverDeLaBase"][0] == ["arn:aws:rds:us-east-1:123456789012:db:solventa-exp"] &&
      [for s in jsondecode(aws_iam_role_policy.fis[0].policy).Statement : s.Condition if s.Sid == "AdministrarSoloAclDeFalla"][0] == { StringEquals = { "ec2:ResourceTag/managedByFIS" = "true" } } &&
      !strcontains(aws_iam_role_policy.fis[0].policy, "subnet-priv-b") && !strcontains(aws_iam_role_policy.fis[0].policy, "subnet-db-b") &&
      alltrue([for s in jsondecode(aws_iam_role_policy.fis[0].policy).Statement : s.Effect == "Allow" && !contains(s.Action, "*")])
    )
    error_message = "Mínimo privilegio: FIS solo cambia la ACL de las subredes de la zona A, administra solo sus propias ACL y reinicia solo la base del ambiente."
  }
  assert {
    condition = (
      aws_s3_bucket.experiments[0].bucket == "solventa-exp-experimentos-123456789012" && aws_s3_bucket.experiments[0].force_destroy &&
      aws_s3_bucket_public_access_block.experiments[0].restrict_public_buckets &&
      output.platform.experiments_bucket.name == aws_s3_bucket.experiments[0].id
    )
    error_message = "Bucket de informes propio de exp, privado y destruible con el ambiente; apps lo recibe en el contrato de platform."
  }
  assert {
    condition = (
      sort(keys(aws_cloudwatch_event_target.ecs_events)) == tolist(["ecs-events", "fis-events", "rds-events"]) &&
      jsondecode(aws_cloudwatch_event_rule.rds.event_pattern).resources == ["arn:aws:rds:us-east-1:123456789012:db:solventa-exp"]
    )
    error_message = "AC3: la cronología guarda los eventos de ECS, del failover de RDS y de FIS en el mismo grupo de logs."
  }
  assert {
    condition = (
      tolist(local.reconciliation_services) == tolist(["cotizacion", "polizas", "dinero", "adaptador-telemetria", "pagos-parametricos", "siniestros"]) &&
      length(jsondecode(aws_iam_role_policy.reconciliation[0].policy).Statement[1].Resource) == 6 &&
      strcontains(aws_ecs_task_definition.reconciliation[0].container_definitions, "default_transaction_read_only=on") &&
      !strcontains(aws_ecs_task_definition.reconciliation[0].container_definitions, "MASTER")
    )
    error_message = "AC2: la reconciliación lee en solo lectura, con el usuario de cada servicio y nunca con el maestro."
  }
  assert {
    condition     = output.experiment.zone == "us-east-1a" && output.experiment.db_multi_az && length(output.experiment.zone_subnet_arns) == 2
    error_message = "El output experiment entrega a los scripts la zona, las subredes y la base."
  }
}
