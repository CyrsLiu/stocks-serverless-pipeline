# Caller identity is used to derive deterministic resource names.
data "aws_caller_identity" "current" {}

# Shared Lambda assume-role policy document.
data "aws_iam_policy_document" "lambda_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

# Shared naming/tags and frontend file discovery.
locals {
  name_prefix             = lower(replace(var.project_name, "_", "-"))
  ingestion_function_name = "${local.name_prefix}-ingestion"
  api_function_name       = "${local.name_prefix}-api"

  frontend_bucket = (
    var.frontend_bucket_name != null && trimspace(var.frontend_bucket_name) != ""
    ? var.frontend_bucket_name
    : "${local.name_prefix}-${data.aws_caller_identity.current.account_id}-${var.aws_region}-site"
  )

  tags = {
    Project   = var.project_name
    ManagedBy = "terraform"
  }

  frontend_files = [
    for file in fileset("${path.module}/../frontend", "**") :
    file
    if file != "config.js.tmpl"
  ]
}

# DynamoDB table stores one winner record per trading day.
resource "aws_dynamodb_table" "movers" {
  name         = "${local.name_prefix}-movers"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "date"

  attribute {
    name = "pk"
    type = "S"
  }

  attribute {
    name = "date"
    type = "S"
  }

  ttl {
    attribute_name = "expiresAt"
    enabled        = true
  }

  tags = local.tags
}

# IAM role and policy scope for ingestion Lambda.
resource "aws_iam_role" "ingestion_lambda" {
  name               = "${local.name_prefix}-ingestion-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
  tags               = local.tags
}

resource "aws_iam_role_policy_attachment" "ingestion_basic_logs" {
  role       = aws_iam_role.ingestion_lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "ingestion_dynamodb" {
  name = "${local.name_prefix}-ingestion-dynamodb"
  role = aws_iam_role.ingestion_lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem", "dynamodb:Query"]
        Resource = aws_dynamodb_table.movers.arn
      }
    ]
  })
}

# IAM role and policy scope for API Lambda.
resource "aws_iam_role" "api_lambda" {
  name               = "${local.name_prefix}-api-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
  tags               = local.tags
}

resource "aws_iam_role_policy_attachment" "api_basic_logs" {
  role       = aws_iam_role.api_lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "api_dynamodb" {
  name = "${local.name_prefix}-api-dynamodb"
  role = aws_iam_role.api_lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["dynamodb:Query"]
        Resource = aws_dynamodb_table.movers.arn
      }
    ]
  })
}

# Package Lambda source directories as deployment zips.
data "archive_file" "ingestion_zip" {
  type        = "zip"
  source_dir  = "${path.module}/../lambda/ingestion"
  output_path = "${path.module}/ingestion.zip"
}

data "archive_file" "api_zip" {
  type        = "zip"
  source_dir  = "${path.module}/../lambda/api"
  output_path = "${path.module}/api.zip"
}

# Explicit log groups allow retention control via Terraform.
resource "aws_cloudwatch_log_group" "ingestion" {
  name              = "/aws/lambda/${local.ingestion_function_name}"
  retention_in_days = var.lambda_log_retention_days
  tags              = local.tags
}

resource "aws_cloudwatch_log_group" "api" {
  name              = "/aws/lambda/${local.api_function_name}"
  retention_in_days = var.lambda_log_retention_days
  tags              = local.tags
}

# Ingestion Lambda: computes/stores daily top mover and backfills gaps.
resource "aws_lambda_function" "ingestion" {
  function_name    = local.ingestion_function_name
  role             = aws_iam_role.ingestion_lambda.arn
  runtime          = "python3.12"
  handler          = "handler.lambda_handler"
  filename         = data.archive_file.ingestion_zip.output_path
  source_code_hash = data.archive_file.ingestion_zip.output_base64sha256
  timeout          = 60
  memory_size      = 128

  environment {
    variables = {
      DYNAMODB_TABLE      = aws_dynamodb_table.movers.name
      MASSIVE_API_KEY     = var.massive_api_key
      STOCK_API_BASE_URL  = var.stock_api_base_url
      DYNAMODB_TTL_DAYS   = tostring(var.dynamodb_ttl_days)
      WATCHLIST           = join(",", var.watchlist)
      PARTITION_KEY_VALUE = var.partition_key_value
    }
  }

  tags = local.tags

  depends_on = [aws_cloudwatch_log_group.ingestion]
}

# API Lambda: reads last 7 winners from DynamoDB.
resource "aws_lambda_function" "api" {
  function_name    = local.api_function_name
  role             = aws_iam_role.api_lambda.arn
  runtime          = "python3.12"
  handler          = "handler.lambda_handler"
  filename         = data.archive_file.api_zip.output_path
  source_code_hash = data.archive_file.api_zip.output_base64sha256
  timeout          = 30
  memory_size      = 128

  environment {
    variables = {
      DYNAMODB_TABLE      = aws_dynamodb_table.movers.name
      PARTITION_KEY_VALUE = var.partition_key_value
    }
  }

  tags = local.tags

  depends_on = [aws_cloudwatch_log_group.api]
}

# EventBridge schedule for automatic weekday ingestion.
resource "aws_cloudwatch_event_rule" "daily_ingestion" {
  name                = "${local.name_prefix}-daily-ingestion"
  description         = "Run stock watchlist ingestion each trading day."
  schedule_expression = var.schedule_expression
  tags                = local.tags
}

resource "aws_cloudwatch_event_target" "ingestion_target" {
  rule      = aws_cloudwatch_event_rule.daily_ingestion.name
  target_id = "ingestion-lambda"
  arn       = aws_lambda_function.ingestion.arn

  retry_policy {
    maximum_event_age_in_seconds = var.eventbridge_maximum_event_age_seconds
    maximum_retry_attempts       = var.eventbridge_maximum_retry_attempts
  }
}

resource "aws_lambda_permission" "allow_eventbridge" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.ingestion.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.daily_ingestion.arn
}

# API Gateway REST surface exposing GET /movers.
resource "aws_api_gateway_rest_api" "movers_api" {
  name        = "${local.name_prefix}-api"
  description = "REST API for stock movers history."
  endpoint_configuration {
    types = ["REGIONAL"]
  }
}

resource "aws_api_gateway_resource" "movers" {
  rest_api_id = aws_api_gateway_rest_api.movers_api.id
  parent_id   = aws_api_gateway_rest_api.movers_api.root_resource_id
  path_part   = "movers"
}

resource "aws_api_gateway_method" "get_movers" {
  rest_api_id   = aws_api_gateway_rest_api.movers_api.id
  resource_id   = aws_api_gateway_resource.movers.id
  http_method   = "GET"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "get_movers" {
  rest_api_id             = aws_api_gateway_rest_api.movers_api.id
  resource_id             = aws_api_gateway_resource.movers.id
  http_method             = aws_api_gateway_method.get_movers.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.api.invoke_arn
}

resource "aws_lambda_permission" "allow_apigateway" {
  statement_id  = "AllowApiGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.movers_api.execution_arn}/*/GET/movers"
}

# Deployment trigger hashes method/integration/lambda code so stage redeploys on changes.
resource "aws_api_gateway_deployment" "movers" {
  rest_api_id = aws_api_gateway_rest_api.movers_api.id

  triggers = {
    redeployment = sha1(jsonencode({
      resource_id = aws_api_gateway_resource.movers.id
      method_id   = aws_api_gateway_method.get_movers.id
      integ_id    = aws_api_gateway_integration.get_movers.id
      lambda_hash = aws_lambda_function.api.source_code_hash
    }))
  }

  lifecycle {
    create_before_destroy = true
  }

  depends_on = [aws_api_gateway_integration.get_movers]
}

resource "aws_api_gateway_stage" "prod" {
  rest_api_id   = aws_api_gateway_rest_api.movers_api.id
  deployment_id = aws_api_gateway_deployment.movers.id
  stage_name    = var.api_stage_name
  tags          = local.tags
}

# Public S3 static website hosting for the frontend SPA.
resource "aws_s3_bucket" "frontend" {
  bucket        = local.frontend_bucket
  force_destroy = true
  tags          = local.tags
}

resource "aws_s3_bucket_website_configuration" "frontend" {
  bucket = aws_s3_bucket.frontend.id

  index_document {
    suffix = "index.html"
  }

  error_document {
    key = "index.html"
  }
}

resource "aws_s3_bucket_public_access_block" "frontend" {
  bucket = aws_s3_bucket.frontend.id

  block_public_acls       = false
  block_public_policy     = false
  ignore_public_acls      = false
  restrict_public_buckets = false
}

data "aws_iam_policy_document" "frontend_public_read" {
  statement {
    sid     = "AllowPublicRead"
    effect  = "Allow"
    actions = ["s3:GetObject"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    resources = ["${aws_s3_bucket.frontend.arn}/*"]
  }
}

resource "aws_s3_bucket_policy" "frontend_public_read" {
  bucket = aws_s3_bucket.frontend.id
  policy = data.aws_iam_policy_document.frontend_public_read.json

  depends_on = [aws_s3_bucket_public_access_block.frontend]
}

# Upload static frontend assets with content-type mapping.
resource "aws_s3_object" "frontend_assets" {
  for_each = toset(local.frontend_files)

  bucket = aws_s3_bucket.frontend.id
  key    = each.value
  source = "${path.module}/../frontend/${each.value}"
  etag   = filemd5("${path.module}/../frontend/${each.value}")

  content_type = (
    endswith(each.value, ".html") ? "text/html; charset=utf-8" :
    endswith(each.value, ".css") ? "text/css; charset=utf-8" :
    endswith(each.value, ".js") ? "application/javascript" :
    endswith(each.value, ".json") ? "application/json" :
    endswith(each.value, ".ico") ? "image/x-icon" :
    endswith(each.value, ".svg") ? "image/svg+xml" :
    "application/octet-stream"
  )
}

# Generate runtime config.js with deployed API stage URL.
resource "aws_s3_object" "frontend_config" {
  bucket = aws_s3_bucket.frontend.id
  key    = "config.js"
  content = templatefile("${path.module}/../frontend/config.js.tmpl", {
    api_base_url = aws_api_gateway_stage.prod.invoke_url
  })
  content_type = "application/javascript"
}
