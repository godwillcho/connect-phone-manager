"""
AWS Lambda entry point for the Amazon Connect Phone Number Manager.

Accepts JSON events with actions: search, claim, release.
Configuration (instance_arn, contact_flow_arn, region) is passed per-invocation
so a single Lambda can serve multiple Connect instances.
"""

import json
import traceback

import boto3

from config import build_config, logger
from connect_operations import (
    batch_claim,
    batch_release,
    search_available_numbers,
)
from validation import validate_event


def _response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=str),
    }


def lambda_handler(event: dict, context) -> dict:
    """
    Route to the correct workflow based on event["action"].

    ── Search ────────────────────────────────────────────────
    {
      "action": "search",
      "instance_arn": "arn:aws:connect:...",
      "contact_flow_arn": "arn:aws:connect:...",
      "region": "us-east-1",            // optional, default us-east-1
      "number_type": "DID",             // DID | TOLL_FREE, default DID
      "prefix": "+1972",                // optional
      "max_results": 20                 // optional, 1-100, default 10
    }

    ── Claim (batch) ─────────────────────────────────────────
    {
      "action": "claim",
      "instance_arn": "...",
      "contact_flow_arn": "...",
      "number_type": "DID",
      "phone_numbers": ["+19725551234", "+19725555678"]
    }

    ── Release ───────────────────────────────────────────────
    {
      "action": "release",
      "instance_arn": "...",
      "contact_flow_arn": "...",
      "phone_number_ids": ["id-1", "id-2"]
    }
    """
    request_id = getattr(context, "aws_request_id", "local")
    logger.info("Invocation %s — action=%s", request_id, event.get("action"))

    # ── Validate ────────────────────────────────────────────
    try:
        event = validate_event(event)
    except ValueError as exc:
        logger.warning("Validation error: %s", exc)
        return _response(400, {"error": str(exc)})

    # ── Build config & clients ──────────────────────────────
    try:
        config = build_config(event)
    except ValueError as exc:
        logger.warning("Config error: %s", exc)
        return _response(400, {"error": str(exc)})

    connect_client = boto3.client("connect", region_name=config.region)
    s3_client = boto3.client("s3", region_name=config.region)

    action = event["action"]

    # ── Route ───────────────────────────────────────────────
    try:
        if action == "search":
            return _handle_search(connect_client, config, event)
        elif action == "claim":
            return _handle_claim(connect_client, s3_client, config, event)
        elif action == "release":
            return _handle_release(connect_client, s3_client, config, event)
    except Exception:
        logger.error("Unhandled exception:\n%s", traceback.format_exc())
        return _response(500, {
            "error": "Internal server error",
            "request_id": request_id,
        })


# ── Action handlers ─────────────────────────────────────────

def _handle_search(client, config, event: dict) -> dict:
    numbers = search_available_numbers(
        client,
        config,
        number_type=event.get("number_type", "DID"),
        prefix=event.get("prefix"),
        max_results=event.get("max_results", 10),
    )
    return _response(200, {
        "available_numbers": numbers,
        "count": len(numbers),
    })


def _handle_claim(client, s3_client, config, event: dict) -> dict:
    results = batch_claim(
        client,
        s3_client,
        config,
        phone_numbers=event["phone_numbers"],
        number_type=event["number_type"],
    )
    claimed = sum(1 for r in results if r["status"] == "claimed")
    failed = len(results) - claimed
    return _response(200, {
        "run_id": config.run_id,
        "results": results,
        "summary": {"claimed": claimed, "failed": failed},
    })


def _handle_release(client, s3_client, config, event: dict) -> dict:
    results = batch_release(
        client,
        s3_client,
        config,
        phone_number_ids=event["phone_number_ids"],
    )
    released = sum(1 for r in results if r["status"] == "released")
    failed = len(results) - released
    return _response(200, {
        "run_id": config.run_id,
        "results": results,
        "summary": {"released": released, "failed": failed},
    })
