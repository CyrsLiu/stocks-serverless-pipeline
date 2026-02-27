# Ingestion Lambda for daily winner selection and historical backfill.

import json
import logging
import os
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import boto3

LOGGER = logging.getLogger()
LOGGER.setLevel(logging.INFO)

# Retry/runtime constants tuned for free-tier friendly execution.
MAX_RETRIES = 3
MAX_BACKFILL_DAYS = 366
DAILY_TARGET_TRADING_DAYS = 7
MARKET_DATE_LOOKBACK_DAYS = 45

# Runtime configuration from Lambda environment variables.
DYNAMODB_TABLE = os.environ["DYNAMODB_TABLE"]
MASSIVE_API_KEY = os.environ["MASSIVE_API_KEY"]
STOCK_API_BASE_URL = os.getenv("STOCK_API_BASE_URL", "https://api.polygon.io").rstrip("/")
PARTITION_KEY_VALUE = os.getenv("PARTITION_KEY_VALUE", "WATCHLIST")
DYNAMODB_TTL_DAYS = int(os.getenv("DYNAMODB_TTL_DAYS", "365"))
WATCHLIST = [ticker.strip().upper() for ticker in os.getenv("WATCHLIST", "").split(",") if ticker.strip()]

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(DYNAMODB_TABLE)


def _to_decimal(value: float, quantizer: str = "0.0001") -> Decimal:
    # Normalize a float to fixed precision for DynamoDB numeric storage.
    return Decimal(str(value)).quantize(Decimal(quantizer), rounding=ROUND_HALF_UP)


def _request_json(url: str) -> Dict[str, Any]:
    # GET JSON with retry/backoff for transient API and network failures.
    last_error: Optional[Exception] = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            request = Request(url, headers={"User-Agent": "stocks-serverless-pipeline/1.0"})
            with urlopen(request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            body = error.read().decode("utf-8", errors="ignore")
            last_error = RuntimeError(f"HTTP {error.code} calling stock API: {body[:250]}")
            if error.code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES:
                sleep_seconds = attempt * 1.5
                LOGGER.warning("Stock API returned %s. Retrying in %.1fs", error.code, sleep_seconds)
                time.sleep(sleep_seconds)
                continue
            break
        except URLError as error:
            last_error = RuntimeError(f"Network error calling stock API: {error.reason}")
            if attempt < MAX_RETRIES:
                sleep_seconds = attempt * 1.5
                LOGGER.warning("Network error. Retrying in %.1fs", sleep_seconds)
                time.sleep(sleep_seconds)
                continue
            break

    raise RuntimeError("Stock API request failed") from last_error


def _extract_open_close(payload: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    # Extract open/close from provider variants (open/close, o/c, O/C).
    open_price = payload.get("open")
    close_price = payload.get("close")

    if open_price is None or close_price is None:
        open_price = payload.get("o") if open_price is None else open_price
        close_price = payload.get("c") if close_price is None else close_price

    if open_price is None or close_price is None:
        open_price = payload.get("O") if open_price is None else open_price
        close_price = payload.get("C") if close_price is None else close_price

    if open_price is None or close_price is None:
        return None, None

    return float(open_price), float(close_price)


def _fetch_open_close(ticker: str, trading_date: str) -> Optional[Tuple[float, float]]:
    # Fetch one ticker for one date using the open/close endpoint.
    query = urlencode({"adjusted": "true", "apiKey": MASSIVE_API_KEY})
    url = f"{STOCK_API_BASE_URL}/v1/open-close/{ticker}/{trading_date}?{query}"
    payload = _request_json(url)

    status = str(payload.get("status", "")).upper()
    if status in {"NOT_FOUND", "ERROR"}:
        return None

    open_price, close_price = _extract_open_close(payload)
    if open_price is None or close_price is None:
        return None

    return open_price, close_price


def _fetch_aggregate_series(ticker: str, start_date: str, end_date: str) -> Dict[str, Tuple[float, float]]:
    # Fetch daily bars for a ticker and return {YYYY-MM-DD: (open, close)}.
    query = urlencode(
        {
            "adjusted": "true",
            "sort": "asc",
            "limit": 5000,
            "apiKey": MASSIVE_API_KEY,
        }
    )
    url = f"{STOCK_API_BASE_URL}/v2/aggs/ticker/{ticker}/range/1/day/{start_date}/{end_date}?{query}"
    payload = _request_json(url)

    status = str(payload.get("status", "")).upper()
    if status in {"ERROR", "NOT_FOUND"}:
        return {}

    results = payload.get("results", [])
    if not isinstance(results, list):
        return {}

    series: Dict[str, Tuple[float, float]] = {}
    for row in results:
        if not isinstance(row, dict):
            continue
        open_price = row.get("o")
        close_price = row.get("c")
        timestamp_ms = row.get("t")
        if open_price is None or close_price is None or timestamp_ms is None:
            continue

        try:
            candle_date = datetime.fromtimestamp(float(timestamp_ms) / 1000, tz=timezone.utc).date().isoformat()
            series[candle_date] = (float(open_price), float(close_price))
        except (TypeError, ValueError, OSError):
            continue

    return series


def _fetch_series_by_ticker(start_date: str, end_date: str) -> Tuple[Dict[str, Dict[str, Tuple[float, float]]], List[str]]:
    # Fetch aggregate series for each watchlist ticker in a single date window.
    series_by_ticker: Dict[str, Dict[str, Tuple[float, float]]] = {}
    fetch_failures: List[str] = []

    for ticker in WATCHLIST:
        try:
            series_by_ticker[ticker] = _fetch_aggregate_series(ticker, start_date, end_date)
        except Exception as error:
            LOGGER.exception("Failed to fetch aggregate series for %s: %s", ticker, error)
            fetch_failures.append(ticker)
            series_by_ticker[ticker] = {}

    return series_by_ticker, fetch_failures


def _get_last_n_market_dates_from_series(
    series_by_ticker: Dict[str, Dict[str, Tuple[float, float]]], end_date: str, count: int
) -> List[str]:
    # Compute recent market dates from the union of all ticker series.
    market_dates = {
        candle_date
        for ticker_series in series_by_ticker.values()
        for candle_date in ticker_series.keys()
        if candle_date <= end_date
    }

    ordered_dates = sorted(market_dates)
    if len(ordered_dates) <= count:
        return ordered_dates
    return ordered_dates[-count:]


def _get_recent_existing_dates(limit: int = 90) -> List[str]:
    # Read recently stored dates so daily runs can fill only missing records.
    response = table.query(
        KeyConditionExpression="pk = :pk",
        ExpressionAttributeValues={":pk": PARTITION_KEY_VALUE},
        ProjectionExpression="#d",
        ExpressionAttributeNames={"#d": "date"},
        ScanIndexForward=False,
        Limit=limit,
    )

    dates: List[str] = []
    for item in response.get("Items", []):
        item_date = item.get("date")
        if isinstance(item_date, str):
            dates.append(item_date)
    return dates


def _parse_requested_trading_date(event: Any) -> Optional[str]:
    # Parse optional single-date override (event.tradingDate or event.date).
    if not isinstance(event, dict):
        return None

    requested = event.get("tradingDate") or event.get("date")
    if not requested:
        return None

    try:
        parsed_date = datetime.strptime(str(requested), "%Y-%m-%d").date()
    except ValueError as error:
        raise RuntimeError("Invalid tradingDate format. Use YYYY-MM-DD.") from error

    if parsed_date > datetime.now(timezone.utc).date():
        raise RuntimeError("tradingDate cannot be in the future")

    return parsed_date.isoformat()


def _parse_backfill_range(event: Any) -> Optional[Tuple[date, date]]:
    # Parse optional backfill mode payload and validate date range.
    if not isinstance(event, dict):
        return None

    mode = str(event.get("mode", "")).strip().lower()
    if mode != "backfill":
        return None

    start_raw = event.get("startDate")
    end_raw = event.get("endDate")
    if not start_raw or not end_raw:
        raise RuntimeError("Backfill mode requires startDate and endDate (YYYY-MM-DD)")

    try:
        start_date = datetime.strptime(str(start_raw), "%Y-%m-%d").date()
        end_date = datetime.strptime(str(end_raw), "%Y-%m-%d").date()
    except ValueError as error:
        raise RuntimeError("Invalid backfill date format. Use YYYY-MM-DD.") from error

    if end_date < start_date:
        raise RuntimeError("endDate must be on or after startDate")

    day_span = (end_date - start_date).days + 1
    if day_span > MAX_BACKFILL_DAYS:
        raise RuntimeError(f"Backfill range too large ({day_span} days). Max is {MAX_BACKFILL_DAYS}.")

    if end_date > datetime.now(timezone.utc).date():
        raise RuntimeError("endDate cannot be in the future")

    return start_date, end_date


def _store_winner(trading_date: str, winner: Dict[str, Any]) -> None:
    # Persist one daily winner row with TTL-based expiry.
    trading_day = datetime.strptime(trading_date, "%Y-%m-%d").date()
    expires_at = int(datetime.combine(trading_day + timedelta(days=DYNAMODB_TTL_DAYS), datetime.min.time(), tzinfo=timezone.utc).timestamp())

    record = {
        "pk": PARTITION_KEY_VALUE,
        "date": trading_date,
        "ticker": winner["ticker"],
        "percentChange": _to_decimal(winner["percent_change"]),
        "closingPrice": _to_decimal(winner["close_price"]),
        "expiresAt": expires_at,
    }
    table.put_item(Item=record)


def _compute_winner_for_date(trading_date: str, series_by_ticker: Dict[str, Dict[str, Tuple[float, float]]]) -> Optional[Dict[str, Any]]:
    # Evaluate watchlist and return the max absolute percent mover for a date.
    movers: List[Dict[str, Any]] = []

    for ticker in WATCHLIST:
        ticker_series = series_by_ticker.get(ticker, {})
        open_close = ticker_series.get(trading_date)
        if not open_close:
            continue

        open_price, close_price = open_close
        if open_price == 0:
            continue

        percent_change = ((close_price - open_price) / open_price) * 100
        movers.append(
            {
                "ticker": ticker,
                "open_price": open_price,
                "close_price": close_price,
                "percent_change": percent_change,
            }
        )

    if not movers:
        return None

    return max(movers, key=lambda item: abs(item["percent_change"]))


def _store_dates_from_series(
    target_dates: List[str], series_by_ticker: Dict[str, Dict[str, Tuple[float, float]]]
) -> Tuple[List[str], List[str]]:
    # Store winners for explicit dates using already-fetched series data.
    if not target_dates:
        return [], []

    skipped_dates: List[str] = []
    stored_dates: List[str] = []
    for trading_date in sorted(set(target_dates)):
        winner = _compute_winner_for_date(trading_date, series_by_ticker)
        if winner is None:
            skipped_dates.append(trading_date)
            continue
        _store_winner(trading_date, winner)
        stored_dates.append(trading_date)

    return stored_dates, skipped_dates


def _store_dates_from_cached_series(target_dates: List[str]) -> Tuple[List[str], List[str], List[str]]:
    # Fetch series once for the full date span, then store per-day winners.
    if not target_dates:
        return [], [], []

    sorted_dates = sorted(set(target_dates))
    start_iso = sorted_dates[0]
    end_iso = sorted_dates[-1]

    series_by_ticker, fetch_failures = _fetch_series_by_ticker(start_iso, end_iso)
    if all(len(series) == 0 for series in series_by_ticker.values()):
        raise RuntimeError("No ticker returned usable aggregate data for requested dates")

    stored_dates, skipped_dates = _store_dates_from_series(sorted_dates, series_by_ticker)

    return stored_dates, skipped_dates, fetch_failures


def _run_daily_mode(event: Dict[str, Any]) -> Dict[str, Any]:
    # Run either one-date ingestion or scheduled daily catch-up mode.
    requested_trading_date = _parse_requested_trading_date(event)
    if requested_trading_date:
        # Single-date mode is useful for ad-hoc reruns and manual correction.
        LOGGER.info("Running single-date mode for %s", requested_trading_date)
        movers: List[Dict[str, Any]] = []

        for ticker in WATCHLIST:
            try:
                open_close = _fetch_open_close(ticker, requested_trading_date)
            except Exception as error:
                LOGGER.exception("Failed to fetch %s: %s", ticker, error)
                continue

            if open_close is None:
                LOGGER.warning("No open/close data for %s on %s", ticker, requested_trading_date)
                continue

            open_price, close_price = open_close
            if open_price == 0:
                LOGGER.warning("Skipping %s because open price is 0", ticker)
                continue

            percent_change = ((close_price - open_price) / open_price) * 100
            movers.append(
                {
                    "ticker": ticker,
                    "open_price": open_price,
                    "close_price": close_price,
                    "percent_change": percent_change,
                }
            )

        if not movers:
            raise RuntimeError("No valid stock data retrieved for watchlist")

        winner = max(movers, key=lambda item: abs(item["percent_change"]))
        _store_winner(requested_trading_date, winner)
        LOGGER.info("Stored winner for %s: %s", requested_trading_date, winner)

        return {
            "statusCode": 200,
            "body": json.dumps(
                {
                    "message": "Top mover stored",
                    "mode": "daily-single",
                    "tradingDate": requested_trading_date,
                    "winner": {
                        "ticker": winner["ticker"],
                        "percentChange": round(winner["percent_change"], 4),
                        "closingPrice": round(winner["close_price"], 4),
                    },
                    "evaluatedTickers": len(movers),
                }
            ),
        }

    yesterday = datetime.now(timezone.utc).date() - timedelta(days=1)
    start_iso = (yesterday - timedelta(days=MARKET_DATE_LOOKBACK_DAYS)).isoformat()
    end_iso = yesterday.isoformat()

    # Daily catch-up mode derives the latest trading dates from watchlist data,
    # then fills only records missing in DynamoDB.
    series_by_ticker, fetch_failures = _fetch_series_by_ticker(start_iso, end_iso)
    if all(len(series) == 0 for series in series_by_ticker.values()):
        raise RuntimeError("No ticker returned usable aggregate data for recent market dates")

    target_dates = _get_last_n_market_dates_from_series(series_by_ticker, end_iso, DAILY_TARGET_TRADING_DAYS)
    if not target_dates:
        raise RuntimeError("Could not determine recent market dates from watchlist data")

    latest_market_date = target_dates[-1]
    existing_dates = set(_get_recent_existing_dates())
    missing_dates = [d for d in target_dates if d not in existing_dates]

    if not missing_dates:
        return {
            "statusCode": 200,
            "body": json.dumps(
                {
                    "message": "No missing daily records",
                    "mode": "daily-catchup",
                    "latestMarketDate": latest_market_date,
                    "targetDates": target_dates,
                    "storedRecords": 0,
                    "failedTickerFetches": fetch_failures,
                }
            ),
        }

    stored_dates, skipped_dates = _store_dates_from_series(missing_dates, series_by_ticker)

    if not stored_dates:
        raise RuntimeError("Daily catch-up failed to store any missing records")

    return {
        "statusCode": 200,
        "body": json.dumps(
            {
                "message": "Daily catch-up completed",
                "mode": "daily-catchup",
                "latestMarketDate": latest_market_date,
                "targetDates": target_dates,
                "missingDates": missing_dates,
                "storedRecords": len(stored_dates),
                "storedDates": stored_dates,
                "skippedDates": skipped_dates,
                "failedTickerFetches": fetch_failures,
            }
        ),
    }


def _run_backfill_mode(start_date: date, end_date: date) -> Dict[str, Any]:
    # Backfill a historical range using one aggregate fetch per ticker.
    start_iso = start_date.isoformat()
    end_iso = end_date.isoformat()
    LOGGER.info("Running backfill from %s to %s", start_iso, end_iso)
    target_dates: List[str] = []
    current = start_date
    while current <= end_date:
        if current.weekday() <= 4:
            target_dates.append(current.isoformat())
        current += timedelta(days=1)

    stored_dates, skipped_dates, fetch_failures = _store_dates_from_cached_series(target_dates)
    if not stored_dates:
        raise RuntimeError("Backfill completed but found no valid market dates with watchlist data")

    return {
        "statusCode": 200,
        "body": json.dumps(
            {
                "message": "Backfill completed",
                "mode": "backfill",
                "startDate": start_iso,
                "endDate": end_iso,
                "storedRecords": len(stored_dates),
                "storedDates": stored_dates,
                "skippedDates": skipped_dates,
                "failedTickerFetches": fetch_failures,
            }
        ),
    }


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    # Lambda entrypoint: route to backfill mode or daily mode.
    if not WATCHLIST:
        raise RuntimeError("WATCHLIST is empty")
    if not MASSIVE_API_KEY:
        raise RuntimeError("MASSIVE_API_KEY is empty")

    backfill_range = _parse_backfill_range(event)
    if backfill_range is not None:
        start_date, end_date = backfill_range
        return _run_backfill_mode(start_date, end_date)

    return _run_daily_mode(event or {})
