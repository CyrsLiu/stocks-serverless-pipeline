# Public API endpoint consumed by the frontend.
output "movers_endpoint" {
  description = "GET endpoint for the movers API."
  value       = "${aws_api_gateway_stage.prod.invoke_url}/movers"
}

# Base stage URL (without resource path).
output "api_stage_invoke_url" {
  description = "Base invoke URL for API stage."
  value       = aws_api_gateway_stage.prod.invoke_url
}

# Public static website endpoint.
output "frontend_website_url" {
  description = "Public S3 static website URL."
  value       = "http://${aws_s3_bucket_website_configuration.frontend.website_endpoint}"
}

# Useful operational references.
output "dynamodb_table_name" {
  description = "DynamoDB table storing daily winners."
  value       = aws_dynamodb_table.movers.name
}

output "ingestion_lambda_name" {
  description = "Lambda function name for daily ingestion."
  value       = aws_lambda_function.ingestion.function_name
}
