# Stocks Serverless Pipeline

Serverless AWS pipeline that computes the daily top stock mover (largest absolute % move) from:

`AAPL, MSFT, GOOGL, AMZN, TSLA, NVDA`

and publishes the last 7 winners to a public static dashboard.

## Architecture

1. **EventBridge Schedule** triggers ingestion Lambda once per trading day.
2. **Ingestion Lambda (`lambda/ingestion`)**:
   - Calls Massive/Polygon endpoints.
   - Uses Aggregates (daily bars) across the full watchlist to derive the latest trading dates.
   - Fills any missing records in the latest 7 trading days.
   - Uses Aggregates (daily bars) for backfill runs to fetch each ticker once.
   - Supports single-date runs via Daily Open/Close when `tradingDate` is passed.
   - Computes `% change = ((close - open) / open) * 100`.
   - Picks the biggest absolute mover.
   - Stores one row per day in DynamoDB.
3. **DynamoDB** stores daily winner records.
4. **API Lambda (`lambda/api`)** reads last 7 records.
5. **API Gateway REST API** exposes `GET /movers`.
6. **S3 Static Website** hosts SPA and fetches `/movers`.
   - Data-quality strip shows trading-day coverage, weekend-closed days, and missing trading days.
   - Insight cards show largest move of the week, most frequent winner, and average absolute move.

## Repo Layout

```text
.
|- frontend/
|  |- index.html
|  |- styles.css
|  |- app.js
|  `- config.js.tmpl
|- lambda/
|  |- ingestion/handler.py
|  `- api/handler.py
`- terraform/
   |- versions.tf
   |- variables.tf
   |- main.tf
   |- outputs.tf
   `- terraform.tfvars.example
```

## Prerequisites

- AWS account (Free Tier eligible)
- AWS CLI configured with deploy permissions
- Terraform `>= 1.5`
- Massive/Polygon API key

## Deployment

1. Set credentials/profile and region for AWS CLI.
2. Configure Terraform variables:

```powershell
cd terraform
Copy-Item terraform.tfvars.example terraform.tfvars
```

3. Edit `terraform.tfvars` and set `massive_api_key`.
4. Deploy:

```powershell
$env:AWS_PROFILE="stocks-pipeline"
terraform init
terraform plan
terraform apply
```

5. After apply, copy outputs:
   - `frontend_website_url` (public dashboard URL)
   - `movers_endpoint` (`GET /movers`)

6. Optional: run ingestion once so the dashboard has data immediately:

```powershell
$fn = terraform output -raw ingestion_lambda_name
aws lambda invoke --function-name $fn out.json
Get-Content out.json
```

## Notes on API Base URL

- Default is `https://api.polygon.io`, which is compatible with Massive/Polygon keys.
- If your account expects `api.massive.com`, set `stock_api_base_url` in `terraform.tfvars`.

## Key Configurations

- `watchlist`: ticker symbols to evaluate each run
- `schedule_expression`: EventBridge cron expression (UTC)
- `massive_api_key`: Massive/Polygon API key
- `lambda_log_retention_days`: CloudWatch retention to control cost
- `eventbridge_maximum_retry_attempts`: retries for failed ingestion trigger
- `eventbridge_maximum_event_age_seconds`: retry window
- `dynamodb_ttl_days`: automatic record expiry window for table size/cost control

## Backfill Mode

The ingestion Lambda supports a `backfill` mode that is rate-limit friendly:

- It fetches each ticker once for a date range using Aggregates daily bars.
- It computes daily winners from that cached ticker data locally.
- It writes one winner record per valid trading day in the range.

Example:

```powershell
$fn = terraform output -raw ingestion_lambda_name
$payload = '{"mode":"backfill","startDate":"2026-02-01","endDate":"2026-02-24"}'
aws lambda invoke --function-name $fn --payload $payload --cli-binary-format raw-in-base64-out backfill.json
Get-Content backfill.json
```

## Daily Gap Healing

Normal scheduled runs automatically enforce coverage for the most recent 7 trading days:

- It fetches recent daily bars for each watchlist ticker.
- It computes recent trading dates from the union of watchlist data.
- It checks which of the last 7 trading dates are already in DynamoDB.
- It backfills only missing trading dates in one pass (one aggregate fetch per ticker).

This prevents weekday gaps from lingering after transient API failures.

## Data Model (DynamoDB)

Partition key: `pk` (constant `WATCHLIST` by default)  
Sort key: `date` (`YYYY-MM-DD`)

Stored attributes:

- `date`
- `ticker`
- `percentChange`
- `closingPrice`
- `expiresAt` (TTL epoch seconds)

## Security

- No AWS credentials or API keys in source control.
- API key is injected via Terraform variable and Lambda environment variable.
- IAM policies are scoped to required DynamoDB actions (`PutItem` and `Query`).
- API Lambda invoke permission is restricted to `GET /movers`.
- If credentials are exposed in chat/history, rotate keys immediately.

## Cost Optimizations

- Lambda memory reduced to `128 MB` for ingestion/API.
- CloudWatch log retention default set to `7 days`.
- EventBridge retry attempts default set to `1`.
- DynamoDB TTL enabled via `expiresAt` (default retention `365` days).

## Local Validation

```powershell
python -m py_compile lambda\ingestion\handler.py lambda\api\handler.py
cd terraform
terraform fmt -recursive
terraform validate
```

## Cleanup

```powershell
cd terraform
terraform destroy
```

## Trade-offs / Challenges

- **Simplicity over market-calendar complexity**: Trading day detection uses a lookback probe against the API, rather than a full exchange holiday calendar.
- **Single table design**: One DynamoDB table with static partition key and date sort key keeps retrieval simple and cheap for last-7 queries.
- **Static hosting**: S3 website hosting is cost-effective and public, but does not include HTTPS on the S3 website endpoint by default (CloudFront can be added if needed).

## Deliverables

- Public GitHub repository: `https://github.com/CyrsLiu/stocks-serverless-pipeline`
- Live frontend URL: `http://stocks-serverless-pipeline-721559935642-us-east-2-site.s3-website.us-east-2.amazonaws.com`
- Live API URL: `https://qd031162hf.execute-api.us-east-2.amazonaws.com/prod/movers`

