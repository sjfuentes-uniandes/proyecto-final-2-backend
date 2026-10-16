# Registro móvil con verificación biométrica previa (HU-M06).
#
# La persona verifica su identidad antes de tener cuenta; `clientes` le entrega un token firmado
# que la app envía en el SignUp de Cognito (ClientMetadata). Este Lambda pre sign-up rechaza el
# registro si el token falta o no corresponde al correo: así no se puede crear una cuenta móvil
# saltándose la biometría. El Lambda solo valida la firma: no llama a ningún servicio, por eso no
# necesita estar en la VPC.

# Secreto compartido: `clientes` firma el token con él y el Lambda lo valida. Lleva también el
# app client móvil, para que el Lambda exija el token solo a la app (el portal web no lo usa).
# El valor depende del app client y el pool del Lambda, así que el Lambda recibe el ARN del
# secreto, no su valor, y lo lee en tiempo de ejecución.
resource "random_password" "registro_token" {
  length  = 48
  special = false
}

resource "aws_secretsmanager_secret" "registro_token" {
  name                    = "${local.prefix}/registro/token"
  description             = "Firma del token de registro (HU-M06): la emite clientes y la valida el Lambda pre sign-up"
  kms_key_id              = local.kms_key_arn
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "registro_token" {
  secret_id = aws_secretsmanager_secret.registro_token.id
  secret_string = jsonencode({
    secret           = random_password.registro_token.result
    mobile_client_id = aws_cognito_user_pool_client.mobile.id
  })
}

# El código se empaqueta junto con el validador del token de la librería común: una sola fuente
# de verdad para firmar (clientes) y validar (Lambda).
data "archive_file" "pre_sign_up" {
  type        = "zip"
  output_path = "${path.module}/.terraform/pre_sign_up.zip"
  source {
    content  = file("${path.module}/../lambdas/pre_sign_up/handler.py")
    filename = "handler.py"
  }
  source {
    content  = file("${path.module}/../../libs/solventa-common/src/solventa_common/token_registro.py")
    filename = "token_registro.py"
  }
}

resource "aws_cloudwatch_log_group" "pre_sign_up" {
  name              = "/aws/lambda/${local.prefix}-pre-sign-up"
  retention_in_days = var.log_retention_days
  kms_key_id        = local.kms_key_arn
}

resource "aws_iam_role" "pre_sign_up" {
  name = "${local.prefix}-pre-sign-up"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# Mínimo privilegio: escribir sus logs y leer un solo secreto.
resource "aws_iam_role_policy" "pre_sign_up" {
  role = aws_iam_role.pre_sign_up.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [
        { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = ["${aws_cloudwatch_log_group.pre_sign_up.arn}:*"] },
        { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = [aws_secretsmanager_secret.registro_token.arn] },
      ],
      local.kms_key_arn != null ? [{ Effect = "Allow", Action = ["kms:Decrypt"], Resource = [local.kms_key_arn] }] : []
    )
  })
}

resource "aws_lambda_function" "pre_sign_up" {
  function_name    = "${local.prefix}-pre-sign-up"
  description      = "Pre sign-up del pool clientes: solo deja registrar la app móvil con un token de verificación biométrica (HU-M06)"
  role             = aws_iam_role.pre_sign_up.arn
  runtime          = "python3.12"
  handler          = "handler.handler"
  filename         = data.archive_file.pre_sign_up.output_path
  source_code_hash = data.archive_file.pre_sign_up.output_base64sha256
  # Cognito espera la respuesta del trigger como máximo 5 s.
  timeout     = 5
  memory_size = 128
  environment {
    variables = {
      REGISTRO_TOKEN_SECRET_ARN = aws_secretsmanager_secret.registro_token.arn
    }
  }
  depends_on = [aws_cloudwatch_log_group.pre_sign_up, aws_iam_role_policy.pre_sign_up]
}

resource "aws_lambda_permission" "pre_sign_up" {
  statement_id  = "AllowCognitoPreSignUp"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.pre_sign_up.function_name
  principal     = "cognito-idp.amazonaws.com"
  source_arn    = aws_cognito_user_pool.customers.arn
}
