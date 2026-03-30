"""
Event payload validation for the Lambda handler.
"""

from config import SUPPORTED_TYPES

VALID_ACTIONS = ("search", "claim", "release")


def validate_event(event: dict) -> dict:
    """
    Validate and sanitise the incoming Lambda event.

    Raises ValueError with a descriptive message on any validation failure.
    Returns the (possibly enriched) event dict.
    """
    if not isinstance(event, dict):
        raise ValueError("Event must be a JSON object")

    action = event.get("action", "")
    if action not in VALID_ACTIONS:
        raise ValueError(
            f"'action' must be one of {VALID_ACTIONS}, got '{action}'"
        )

    # instance_arn and contact_flow_arn are optional in the event
    # because they can come from env vars set via CloudFormation parameters.
    # build_config() resolves event -> env var fallback.
    for field in ("instance_arn", "contact_flow_arn", "region"):
        val = event.get(field)
        if val is not None and (not isinstance(val, str) or not val.strip()):
            raise ValueError(f"'{field}' must be a non-empty string when provided")

    # ── Action-specific validation ──────────────────────────
    if action == "search":
        _validate_search(event)
    elif action == "claim":
        _validate_claim(event)
    elif action == "release":
        _validate_release(event)

    return event


def _validate_search(event: dict) -> None:
    number_type = event.get("number_type", "DID")
    if number_type not in SUPPORTED_TYPES:
        raise ValueError(
            f"'number_type' must be one of {SUPPORTED_TYPES}, got '{number_type}'"
        )
    event.setdefault("number_type", number_type)

    prefix = event.get("prefix")
    if prefix is not None:
        if not isinstance(prefix, str) or not prefix.startswith("+"):
            raise ValueError("'prefix' must start with '+' (e.g. '+1972')")
    # No prefix = any available US number

    max_results = event.get("max_results", 10)
    if not isinstance(max_results, int) or max_results < 1 or max_results > 100:
        raise ValueError("'max_results' must be an integer between 1 and 100")
    event["max_results"] = max_results


def _validate_claim(event: dict) -> None:
    number_type = event.get("number_type", "")
    if number_type not in SUPPORTED_TYPES:
        raise ValueError(
            f"'number_type' must be one of {SUPPORTED_TYPES}, got '{number_type}'"
        )

    phone_numbers = event.get("phone_numbers")
    if not isinstance(phone_numbers, list) or len(phone_numbers) == 0:
        raise ValueError("'phone_numbers' must be a non-empty list of E.164 phone numbers")

    for idx, pn in enumerate(phone_numbers):
        if not isinstance(pn, str) or not pn.startswith("+"):
            raise ValueError(
                f"phone_numbers[{idx}] must be an E.164 string starting with '+', got '{pn}'"
            )


def _validate_release(event: dict) -> None:
    phone_number_ids = event.get("phone_number_ids")
    if not isinstance(phone_number_ids, list) or len(phone_number_ids) == 0:
        raise ValueError("'phone_number_ids' must be a non-empty list of phone number ID strings")

    for idx, pid in enumerate(phone_number_ids):
        if not isinstance(pid, str) or not pid.strip():
            raise ValueError(
                f"phone_number_ids[{idx}] must be a non-empty string"
            )
