"""
S3-backed CSV read / write / update operations.
"""

import csv
import io

from botocore.exceptions import ClientError

from config import (
    CLAIMED_CSV_HEADERS,
    CLAIMED_CSV_KEY,
    RELEASED_CSV_HEADERS,
    RELEASED_CSV_KEY,
    ConnectConfig,
    logger,
)


def _s3_key(config: ConnectConfig, filename: str) -> str:
    return f"{config.s3_prefix}/{filename}"


# ── Generic helpers ─────────────────────────────────────────

def _read_csv_from_s3(s3_client, bucket: str, key: str) -> list[dict]:
    """Read a CSV from S3 and return a list of row dicts. Returns [] if the key doesn't exist."""
    try:
        resp = s3_client.get_object(Bucket=bucket, Key=key)
        body = resp["Body"].read().decode("utf-8")
        return list(csv.DictReader(io.StringIO(body)))
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("NoSuchKey", "404"):
            logger.info("CSV not found at s3://%s/%s — starting fresh", bucket, key)
            return []
        raise


def _write_csv_to_s3(
    s3_client, bucket: str, key: str, headers: list[str], rows: list[dict]
) -> None:
    """Serialise rows as CSV and upload to S3."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=headers)
    writer.writeheader()
    writer.writerows(rows)
    s3_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=buf.getvalue().encode("utf-8"),
        ContentType="text/csv",
    )
    logger.info("Wrote %d row(s) to s3://%s/%s", len(rows), bucket, key)


# ── Claimed CSV ─────────────────────────────────────────────

def append_claimed_record(s3_client, config: ConnectConfig, record: dict) -> None:
    key = _s3_key(config, CLAIMED_CSV_KEY)
    rows = _read_csv_from_s3(s3_client, config.s3_bucket, key)
    rows.append(record)
    _write_csv_to_s3(s3_client, config.s3_bucket, key, CLAIMED_CSV_HEADERS, rows)


def update_claimed_status(
    s3_client, config: ConnectConfig, phone_number_id: str, new_status: str
) -> bool:
    """Update the status of a phone_number_id in the claimed CSV. Returns True if found."""
    key = _s3_key(config, CLAIMED_CSV_KEY)
    rows = _read_csv_from_s3(s3_client, config.s3_bucket, key)

    updated = False
    for row in rows:
        if row["phone_number_id"] == phone_number_id:
            row["status"] = new_status
            updated = True

    if updated:
        _write_csv_to_s3(s3_client, config.s3_bucket, key, CLAIMED_CSV_HEADERS, rows)
        logger.info("Status for %s updated to '%s'", phone_number_id, new_status)
    else:
        logger.warning("phone_number_id '%s' not found in claimed CSV", phone_number_id)

    return updated


def load_releasable_rows(s3_client, config: ConnectConfig) -> list[dict]:
    """Return claimed CSV rows with status 'claimed' or 'association_failed'."""
    key = _s3_key(config, CLAIMED_CSV_KEY)
    rows = _read_csv_from_s3(s3_client, config.s3_bucket, key)
    return [r for r in rows if r.get("status") in ("claimed", "association_failed")]


def get_claimed_row(
    s3_client, config: ConnectConfig, phone_number_id: str
) -> dict | None:
    """Look up a single claimed row by phone_number_id."""
    key = _s3_key(config, CLAIMED_CSV_KEY)
    for row in _read_csv_from_s3(s3_client, config.s3_bucket, key):
        if row["phone_number_id"] == phone_number_id:
            return row
    return None


# ── Released CSV ────────────────────────────────────────────

def append_released_record(s3_client, config: ConnectConfig, record: dict) -> None:
    key = _s3_key(config, RELEASED_CSV_KEY)
    rows = _read_csv_from_s3(s3_client, config.s3_bucket, key)
    rows.append(record)
    _write_csv_to_s3(s3_client, config.s3_bucket, key, RELEASED_CSV_HEADERS, rows)
