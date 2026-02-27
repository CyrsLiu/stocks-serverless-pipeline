# Project-level naming and region settings.
variable "project_name" {
  description = "Prefix for all resources."
  type        = string
  default     = "stocks-serverless-pipeline"
}

variable "aws_region" {
  description = "AWS region for all resources."
  type        = string
  default     = "us-east-1"
}

# API credential injected into ingestion Lambda.
variable "massive_api_key" {
  description = "Massive/Polygon API key."
  type        = string
  sensitive   = true
}

# Provider and watchlist behavior.
variable "stock_api_base_url" {
  description = "Base URL for the stock API."
  type        = string
  default     = "https://api.polygon.io"
}

variable "watchlist" {
  description = "Tickers to evaluate each day."
  type        = list(string)
  default     = ["AAPL", "MSFT", "GOOGL", "AMZN", "TSLA", "NVDA"]
}

variable "schedule_expression" {
  description = "EventBridge cron/rate expression for ingestion."
  type        = string
  default     = "cron(10 22 ? * MON-FRI *)"
}

# Shared keying and API stage naming.
variable "partition_key_value" {
  description = "Static partition key to support range queries by date."
  type        = string
  default     = "WATCHLIST"
}

variable "api_stage_name" {
  description = "API Gateway stage name."
  type        = string
  default     = "prod"
}

variable "frontend_bucket_name" {
  description = "Optional custom bucket name for the frontend."
  type        = string
  default     = null
}

# Cost/reliability controls.
variable "lambda_log_retention_days" {
  description = "CloudWatch log retention in days for Lambda log groups."
  type        = number
  default     = 7
}

variable "eventbridge_maximum_retry_attempts" {
  description = "Maximum retry attempts for failed EventBridge target invocations."
  type        = number
  default     = 1
}

variable "eventbridge_maximum_event_age_seconds" {
  description = "Maximum age of an event that EventBridge retries."
  type        = number
  default     = 3600
}

variable "dynamodb_ttl_days" {
  description = "How many days to keep mover records before DynamoDB TTL expiration."
  type        = number
  default     = 365
}
