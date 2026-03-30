"""
AWS Connect API wrappers with retry, pagination, and batch support.
"""

import time
from datetime import datetime, timezone

from botocore.exceptions import ClientError

from config import (
    CLAIM_POLL_INTERVAL,
    CLAIM_POLL_MAX_ATTEMPTS,
    PHONE_COUNTRY_CODE,
    ConnectConfig,
    logger,
)
from csv_storage import (
    append_claimed_record,
    append_released_record,
    update_claimed_status,
    get_claimed_row,
)
from retry import retry_with_backoff


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


# ── Search (with pagination) ───────────────────────────────

@retry_with_backoff()
def _search_page(client, kwargs: dict) -> dict:
    return client.search_available_phone_numbers(**kwargs)


def search_available_numbers(
    client,
    config: ConnectConfig,
    number_type: str,
    prefix: str | None = None,
    max_results: int = 10,
) -> list[dict]:
    """
    Search for available US phone numbers with full pagination.

    Returns up to *max_results* entries across multiple API pages.
    """
    collected: list[dict] = []
    next_token: str | None = None

    while len(collected) < max_results:
        page_size = min(10, max_results - len(collected))
        kwargs: dict = {
            "TargetArn": config.instance_arn,
            "PhoneNumberCountryCode": PHONE_COUNTRY_CODE,
            "PhoneNumberType": number_type,
            "MaxResults": page_size,
        }
        if prefix:
            kwargs["PhoneNumberPrefix"] = prefix
        if next_token:
            kwargs["NextToken"] = next_token

        try:
            resp = _search_page(client, kwargs)
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code in ("InvalidParameterException", "ResourceNotFoundException"):
                logger.warning(
                    "Search failed for type '%s': %s",
                    number_type,
                    exc.response["Error"]["Message"],
                )
                break
            raise

        page = resp.get("AvailableNumbersList", [])
        collected.extend(page)
        next_token = resp.get("NextToken")
        if not next_token or not page:
            break

    logger.info("Search returned %d available number(s)", len(collected))
    return collected[:max_results]


# ── Claim ───────────────────────────────────────────────────

@retry_with_backoff()
def _claim(client, instance_arn: str, phone_number: str, description: str | None = None) -> dict:
    kwargs = {"TargetArn": instance_arn, "PhoneNumber": phone_number}
    if description:
        kwargs["PhoneNumberDescription"] = description
    return client.claim_phone_number(**kwargs)


def claim_phone_number(client, config: ConnectConfig, phone_number: str, description: str | None = None) -> dict:
    logger.info("Claiming %s on instance %s", phone_number, config.instance_id)
    return _claim(client, config.instance_arn, phone_number, description)


# ── Poll claim status ──────────────────────────────────────

@retry_with_backoff(max_retries=2)
def _describe(client, phone_number_id: str) -> dict:
    return client.describe_phone_number(PhoneNumberId=phone_number_id)


def poll_claim_status(client, phone_number_id: str) -> str:
    """Poll until claim status is no longer IN_PROGRESS. Returns CLAIMED | FAILED | UNKNOWN."""
    for attempt in range(1, CLAIM_POLL_MAX_ATTEMPTS + 1):
        try:
            resp = _describe(client, phone_number_id)
            status = (
                resp.get("ClaimedPhoneNumberSummary", {})
                .get("PhoneNumberStatus", {})
                .get("Status", "UNKNOWN")
            )
        except ClientError as exc:
            logger.error("describe_phone_number error: %s", exc)
            return "UNKNOWN"

        if status != "IN_PROGRESS":
            return status

        logger.info(
            "Claim IN_PROGRESS — poll %d/%d, retrying in %.0fs",
            attempt,
            CLAIM_POLL_MAX_ATTEMPTS,
            CLAIM_POLL_INTERVAL,
        )
        time.sleep(CLAIM_POLL_INTERVAL)

    logger.warning("Timed out waiting for claim after %d attempts", CLAIM_POLL_MAX_ATTEMPTS)
    return "UNKNOWN"


# ── Associate / Disassociate ────────────────────────────────

@retry_with_backoff()
def associate_phone_to_flow(client, config: ConnectConfig, phone_number_id: str) -> None:
    client.associate_phone_number_contact_flow(
        PhoneNumberId=phone_number_id,
        InstanceId=config.instance_id,
        ContactFlowId=config.contact_flow_arn,
    )
    logger.info("Associated %s with flow %s", phone_number_id, config.contact_flow_arn)


@retry_with_backoff()
def disassociate_phone_from_flow(
    client, config: ConnectConfig, phone_number_id: str
) -> None:
    client.disassociate_phone_number_contact_flow(
        PhoneNumberId=phone_number_id,
        InstanceId=config.instance_id,
    )
    logger.info("Disassociated %s", phone_number_id)


# ── Release ─────────────────────────────────────────────────

@retry_with_backoff()
def release_phone_number(client, phone_number_id: str) -> None:
    client.release_phone_number(PhoneNumberId=phone_number_id)
    logger.info("Released %s", phone_number_id)


# ── Batch claim ─────────────────────────────────────────────

def batch_claim(
    client,
    s3_client,
    config: ConnectConfig,
    phone_numbers: list[dict],
    number_type: str,
) -> list[dict]:
    """
    Claim multiple phone numbers sequentially.

    Each entry in phone_numbers is {"number": str, "description": str|None}.
    For each: claim -> poll -> associate -> record to S3 CSV.
    Returns a list of per-number result dicts.
    """
    results: list[dict] = []

    for entry in phone_numbers:
        phone_number = entry["number"]
        desc = entry.get("description")
        result: dict = {
            "phone_number": phone_number,
            "phone_number_id": None,
            "phone_number_arn": None,
            "description": desc or "",
            "status": "failed",
            "error": None,
        }

        # 1. Claim
        try:
            resp = claim_phone_number(client, config, phone_number, desc)
        except ClientError as exc:
            result["error"] = str(exc)
            logger.error("Claim failed for %s: %s", phone_number, exc)
            results.append(result)
            continue

        pn_id = resp["PhoneNumberId"]
        pn_arn = resp["PhoneNumberArn"]
        result["phone_number_id"] = pn_id
        result["phone_number_arn"] = pn_arn
        claimed_at = _now_utc()

        # 2. Poll
        claim_status = poll_claim_status(client, pn_id)
        if claim_status == "FAILED":
            result["status"] = "failed"
            result["error"] = "Claim status FAILED — number will auto-release within 24h"
            logger.error("Claim FAILED for %s", phone_number)
            results.append(result)
            continue

        if claim_status == "UNKNOWN":
            logger.warning("Could not confirm claim for %s — attempting association anyway", phone_number)

        # 3. Associate
        status = "claimed"
        try:
            associate_phone_to_flow(client, config, pn_id)
        except ClientError as exc:
            status = "association_failed"
            result["error"] = f"Association failed: {exc}"
            logger.warning("Association failed for %s: %s", phone_number, exc)

        result["status"] = status

        # 4. Record to S3
        append_claimed_record(s3_client, config, {
            "timestamp": claimed_at,
            "phone_number": phone_number,
            "number_type": number_type,
            "phone_number_id": pn_id,
            "phone_number_arn": pn_arn,
            "contact_flow_arn": config.contact_flow_arn,
            "instance_id": config.instance_id,
            "instance_arn": config.instance_arn,
            "description": desc or "",
            "status": status,
        })

        results.append(result)

    return results


# ── Batch release ───────────────────────────────────────────

def batch_release(
    client,
    s3_client,
    config: ConnectConfig,
    phone_number_ids: list[str],
) -> list[dict]:
    """
    Release multiple phone numbers sequentially.

    For each ID: disassociate (best-effort) -> release -> update CSVs.
    Returns a list of per-number result dicts.
    """
    results: list[dict] = []

    for pn_id in phone_number_ids:
        claimed_row = get_claimed_row(s3_client, config, pn_id)
        result: dict = {
            "phone_number_id": pn_id,
            "phone_number": claimed_row["phone_number"] if claimed_row else "",
            "status": "release_failed",
            "error": None,
        }

        # 1. Disassociate (best-effort)
        try:
            disassociate_phone_from_flow(client, config, pn_id)
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code in ("ResourceNotFoundException", "InvalidParameterException"):
                logger.info("Disassociate skipped for %s: %s", pn_id, exc.response["Error"]["Message"])
            else:
                logger.warning("Disassociate failed (non-fatal) for %s: %s", pn_id, exc)

        # 2. Release
        try:
            release_phone_number(client, pn_id)
        except ClientError as exc:
            result["error"] = str(exc)
            logger.error("Release failed for %s: %s", pn_id, exc)
            update_claimed_status(s3_client, config, pn_id, "release_failed")
            results.append(result)
            continue

        result["status"] = "released"

        # 3. Update CSVs
        update_claimed_status(s3_client, config, pn_id, "released")
        if claimed_row:
            append_released_record(s3_client, config, {
                "released_at": _now_utc(),
                "phone_number": claimed_row.get("phone_number", ""),
                "number_type": claimed_row.get("number_type", ""),
                "phone_number_id": pn_id,
                "phone_number_arn": claimed_row.get("phone_number_arn", ""),
                "contact_flow_arn": claimed_row.get("contact_flow_arn", ""),
                "instance_id": claimed_row.get("instance_id", ""),
                "instance_arn": claimed_row.get("instance_arn", ""),
                "claimed_at": claimed_row.get("timestamp", ""),
            })

        results.append(result)

    return results
