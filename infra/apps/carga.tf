# ---------------------------------------------------------------------------
# Experimento de carga (HU-W09): tarea k6 puntual dentro de la VPC, contra el ALB
# interno en el listener de api-socios. No se ejecuta sola: la lanza
# tests/e2e/test_hu_w09_carga.py con run-task, con solo variables de entorno en
# containerOverrides (la tarea no llama a AWS: sin rol de tarea).
# Dos definiciones con el mismo contenedor, rol, log group y SG: <prefijo>-carga
# (experiments/hu-w09/carga.js) y <prefijo>-carga-alta (alta.js). El script viaja en
# base64 en HU_W09_SCRIPT y el command fijo lo decodifica a /tmp/s.js: sin S3 ni URL
# prefirmada, cuya firma y token de sesión k6 imprimía al arrancar. Una definición
# admite 64 KiB (cuota de ECS no ajustable); las pruebas exigen menos de 48 KiB.
# Cambiar un script crea una revisión nueva de su definición al aplicar exp.
# Todo depende de load_test_enabled (false por omisión; solo envs/exp lo enciende).
# Las pruebas buscan las familias y el SG por nombre, sin salidas.
# ---------------------------------------------------------------------------
locals {
  load_test = var.load_test_enabled ? 1 : 0
  # Puerto del listener de api-socios en el ALB, del catálogo de platform. En AWS siempre está
  # (platform/catalog.tf); si faltara (fixtures reducidos de las pruebas), no se abre el ALB.
  load_test_port = try(local.p.catalog["api-socios"].listener_port, null)
  # Familia (sufijo de <prefijo>-) y script de cada definición de tarea k6.
  load_test_tasks = var.load_test_enabled ? {
    carga = { family = "carga", script = filebase64("${path.module}/../../experiments/hu-w09/carga.js") }
    alta  = { family = "carga-alta", script = filebase64("${path.module}/../../experiments/hu-w09/alta.js") }
  } : {}
}

resource "aws_cloudwatch_log_group" "load_test" {
  count             = local.load_test
  name              = "/ecs/${local.prefix}/carga"
  retention_in_days = var.load_test_log_retention_days
  kms_key_id        = local.p.kms_key_arn
}

# Rol de ejecución: solo escribir en su log group. La imagen es pública (sin ECR) y no hay secretos.
resource "aws_iam_role" "load_test_execution" {
  count              = local.load_test
  name               = "${local.prefix}-carga-exec"
  assume_role_policy = local.task_trust
}

resource "aws_iam_role_policy" "load_test_execution" {
  count = local.load_test
  role  = aws_iam_role.load_test_execution[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
      Resource = ["${aws_cloudwatch_log_group.load_test[0].arn}:*"]
    }]
  })
}

resource "aws_ecs_task_definition" "load_test" {
  for_each                 = local.load_test_tasks
  family                   = "${local.prefix}-${each.value.family}"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = tostring(var.load_test_cpu)
  memory                   = tostring(var.load_test_memory)
  execution_role_arn       = aws_iam_role.load_test_execution[0].arn
  container_definitions = jsonencode([{
    name      = "k6"
    image     = var.load_test_image
    essential = true
    # El ENTRYPOINT de la imagen es k6; sh -c decodifica el script y cede el proceso a k6.
    # -q quita el progreso y el encabezado; los avisos de k6 y las líneas HU-W09-* siguen.
    entryPoint  = ["sh", "-c"]
    command     = ["echo \"$HU_W09_SCRIPT\" | base64 -d > /tmp/s.js && exec k6 run --log-format=raw -q /tmp/s.js"]
    environment = [{ name = "HU_W09_SCRIPT", value = each.value.script }]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.load_test[0].name
        awslogs-region        = local.p.region
        awslogs-stream-prefix = "k6"
      }
    }
  }])
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }
}

# SG de k6: sin entradas; sale a todo (imagen por el NAT, CloudWatch Logs, ALB y API Gateway).
resource "aws_security_group" "load_test" {
  count       = local.load_test
  name        = "${local.prefix}-carga"
  description = "Tarea k6 del experimento de carga (HU-W09)"
  vpc_id      = local.p.vpc_id
}

resource "aws_vpc_security_group_egress_rule" "load_test" {
  count             = local.load_test
  security_group_id = aws_security_group.load_test[0].id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

# ALB interno <- k6: solo desde su SG y solo en el listener de api-socios (como network.tf).
resource "aws_vpc_security_group_ingress_rule" "load_test_alb" {
  count                        = var.load_test_enabled && local.load_test_port != null ? 1 : 0
  security_group_id            = local.p.alb.security_group_id
  referenced_security_group_id = aws_security_group.load_test[0].id
  ip_protocol                  = "tcp"
  from_port                    = local.load_test_port
  to_port                      = local.load_test_port
  description                  = "k6 (HU-W09)"
}
