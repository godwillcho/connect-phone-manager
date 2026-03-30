"""
Centralized configuration, logging, and shared constants.
"""

import logging
import os
from dataclasses import dataclass

# ── Logger ──────────────────────────────────────────────────
logger = logging.getLogger("connect_phone_manager")
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("[%(levelname)s] %(name)s — %(message)s")
    )
    logger.addHandler(handler)

# ── Phone number constants ──────────────────────────────────
PHONE_COUNTRY_CODE = "US"
SUPPORTED_TYPES = ("DID", "TOLL_FREE")

# ── Polling settings ────────────────────────────────────────
CLAIM_POLL_INTERVAL = 3.0       # seconds between status polls
CLAIM_POLL_MAX_ATTEMPTS = 10    # max polls before giving up

# ── S3 defaults (overridable via env vars) ──────────────────
DEFAULT_S3_PREFIX = "connect-phone-manager"

CLAIMED_CSV_KEY = "claimed_phone_numbers.csv"
RELEASED_CSV_KEY = "released_phone_numbers.csv"

CLAIMED_CSV_HEADERS = [
    "timestamp",
    "phone_number",
    "number_type",
    "phone_number_id",
    "phone_number_arn",
    "contact_flow_arn",
    "instance_id",
    "instance_arn",
    "description",
    "status",
]

RELEASED_CSV_HEADERS = [
    "released_at",
    "phone_number",
    "number_type",
    "phone_number_id",
    "phone_number_arn",
    "contact_flow_arn",
    "instance_id",
    "instance_arn",
    "claimed_at",
]


# ── Runtime config built from the Lambda event ──────────────
@dataclass(frozen=True)
class ConnectConfig:
    instance_arn: str
    instance_id: str
    contact_flow_arn: str
    region: str
    s3_bucket: str
    s3_prefix: str


def extract_instance_id(instance_arn: str) -> str:
    """
    Derive the Connect instance ID from its ARN.

    ARN format: arn:aws:connect:<region>:<account>:instance/<instance-id>
    """
    try:
        if "instance/" not in instance_arn:
            raise ValueError("ARN does not contain 'instance/'")
        instance_id = instance_arn.split("instance/")[-1].split("/")[0]
        if not instance_id:
            raise ValueError("Empty instance ID segment")
        return instance_id
    except (IndexError, ValueError) as exc:
        raise ValueError(
            f"Could not extract instance ID from ARN '{instance_arn}': {exc}. "
            "Expected format: arn:aws:connect:<region>:<account>:instance/<instance-id>"
        ) from exc


def build_config(event: dict) -> ConnectConfig:
    """Construct a ConnectConfig from the Lambda event payload, with env-var fallbacks."""
    instance_arn = event.get("instance_arn") or os.environ.get("CONNECT_INSTANCE_ARN", "")
    contact_flow_arn = event.get("contact_flow_arn") or os.environ.get("CONTACT_FLOW_ARN", "")
    region = event.get("region") or os.environ.get("CONNECT_REGION", "us-east-1")

    if not instance_arn:
        raise ValueError("'instance_arn' is required (event or CONNECT_INSTANCE_ARN env var)")
    if not contact_flow_arn:
        raise ValueError("'contact_flow_arn' is required (event or CONTACT_FLOW_ARN env var)")

    s3_bucket = event.get("s3_bucket") or os.environ.get("S3_BUCKET", "")
    if not s3_bucket:
        raise ValueError(
            "'s3_bucket' must be provided in the event or set as the S3_BUCKET env var"
        )

    s3_prefix = event.get("s3_prefix") or os.environ.get(
        "S3_PREFIX", DEFAULT_S3_PREFIX
    )

    return ConnectConfig(
        instance_arn=instance_arn,
        instance_id=extract_instance_id(instance_arn),
        contact_flow_arn=contact_flow_arn,
        region=region,
        s3_bucket=s3_bucket,
        s3_prefix=s3_prefix,
    )
