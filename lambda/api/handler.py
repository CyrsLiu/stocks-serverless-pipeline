# API Lambda that returns the latest 7 daily winners from DynamoDB.

import json
import logging
import os
from decimal import Decimal
from typing import Any, Dict

import boto3
from boto3.dynamodb.conditions import Key

LOGGER = logging.getLogger()
LOGGER.setLevel(logging.INFO)

# Runtime configuration from Lambda environment variables.
DYNAMODB_TABLE = os.environ["DYNAMODB_TABLE"]
PARTITION_KEY_VALUE = os.getenv("PARTITION_KEY_VALUE", "WATCHLIST")

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(DYNAMODB_TABLE)

DEFAULT_HEADERS = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET,OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}


def _normalize_value(value: Any) -> Any:
    # Convert DynamoDB Decimal values into JSON-safe native numbers.
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: _normalize_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize_value(v) for v in value]
    return value


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    # Serve GET /movers and handle CORS preflight.
    method = (event or {}).get("httpMethod", "GET")
    if method == "OPTIONS":
        return {"statusCode": 200, "headers": DEFAULT_HEADERS, "body": ""}
    if method != "GET":
        return {
            "statusCode": 405,
            "headers": DEFAULT_HEADERS,
            "body": json.dumps({"message": "Method not allowed"}),
        }

    try:
        # Query newest-first and return only the latest 7 records.
        response = table.query(
            KeyConditionExpression=Key("pk").eq(PARTITION_KEY_VALUE),
            ScanIndexForward=False,
            Limit=7,
        )
    except Exception:
        LOGGER.exception("Failed to query DynamoDB for movers")
        return {
            "statusCode": 500,
            "headers": DEFAULT_HEADERS,
            "body": json.dumps({"message": "Failed to fetch movers"}),
        }

    raw_items = [_normalize_value(item) for item in response.get("Items", [])]
    items = [
        {
            "date": item.get("date"),
            "ticker": item.get("ticker"),
            "percentChange": item.get("percentChange"),
            "closingPrice": item.get("closingPrice"),
        }
        for item in raw_items
    ]
    payload = {"items": items}

    return {
        "statusCode": 200,
        "headers": DEFAULT_HEADERS,
        "body": json.dumps(payload),
    }
