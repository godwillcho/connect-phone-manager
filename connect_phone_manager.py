#!/usr/bin/env python3
"""
Amazon Connect Phone Number Manager

At runtime, presents a menu:
  [1] Claim   — Acquire a US phone number and associate it to a contact flow.
  [2] Release — Release phone numbers logged in the claimed CSV file.

Instance ID is derived automatically from the Instance ARN.
Rate-limit throttling is applied after every API call.

CSV files produced:
  claimed_phone_numbers.csv  — one row per claimed number (status updated on release)
  released_phone_numbers.csv — one row appended for each successfully released number
"""

import boto3
import csv
import sys
import time
import os
from datetime import datetime, timezone
from botocore.exceptions import ClientError


# ------------------------------------------------------------
# Hard-coded configuration — update these before running
# ------------------------------------------------------------
INSTANCE_ARN     = "YOUR_CONNECT_INSTANCE_ARN"   # e.g. "arn:aws:connect:us-east-1:123456789012:instance/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
CONTACT_FLOW_ARN = "YOUR_CONTACT_FLOW_ARN"       # e.g. "arn:aws:connect:us-east-1:123456789012:instance/aaaaaaaa-.../contact-flow/ffffffff-..."
DEFAULT_REGION   = "us-east-1"

# Phone number settings
PHONE_COUNTRY_CODE  = "US"
SUPPORTED_TYPES     = ["DID", "TOLL_FREE"]
DEFAULT_NUMBER_TYPE = "DID"                      # shown as default in the claim menu

# Throttle / polling settings (seconds)
WAIT_AFTER_SEARCH         = 1.0   # after search_available_phone_numbers
WAIT_AFTER_CLAIM          = 2.0   # after claim_phone_number
WAIT_AFTER_ASSOCIATE      = 1.0   # after associate_phone_number_contact_flow
WAIT_AFTER_DISASSOCIATE   = 1.0   # after disassociate_phone_number_contact_flow
WAIT_AFTER_RELEASE        = 2.0   # after release_phone_number
CLAIM_POLL_INTERVAL       = 3.0   # seconds between describe_phone_number status polls
CLAIM_POLL_MAX_ATTEMPTS   = 10    # max polls before giving up on status check

# CSV files
CLAIMED_CSV_FILE  = "claimed_phone_numbers.csv"
RELEASED_CSV_FILE = "released_phone_numbers.csv"

CLAIMED_CSV_HEADERS = [
    "timestamp",          # when the number was claimed
    "phone_number",
    "number_type",
    "phone_number_id",
    "phone_number_arn",
    "contact_flow_arn",
    "instance_id",
    "instance_arn",
    "status",
    # status values:
    #   "claimed"             — number claimed and associated successfully
    #   "association_failed"  — number claimed but flow association failed
    #   "released"            — number subsequently released successfully
    #   "release_failed"      — release was attempted but failed
]

RELEASED_CSV_HEADERS = [
    "released_at",        # when the number was released
    "phone_number",
    "number_type",
    "phone_number_id",
    "phone_number_arn",
    "contact_flow_arn",
    "instance_id",
    "instance_arn",
    "claimed_at",         # original claim timestamp, carried over from claimed CSV
]


# ------------------------------------------------------------
# Utilities
# ------------------------------------------------------------

def separator(char: str = "=", width: int = 60) -> None:
    print(char * width)


def prompt(message: str) -> str:
    """Print a prompt and return stripped user input."""
    return input(message).strip()


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def extract_instance_id(instance_arn: str) -> str:
    """
    Derive the Connect instance ID from its ARN.

    ARN format:
      arn:aws:connect:<region>:<account-id>:instance/<instance-id>

    >>> extract_instance_id("arn:aws:connect:us-east-1:123456789012:instance/aabbccdd-1122-3344-5566-aabbccddeeff")
    'aabbccdd-1122-3344-5566-aabbccddeeff'
    """
    try:
        instance_id = instance_arn.split("instance/")[-1].split("/")[0]
        if not instance_id:
            raise ValueError("Empty instance ID segment")
        return instance_id
    except (IndexError, ValueError) as exc:
        sys.exit(
            f"❌  Could not extract instance ID from ARN: '{instance_arn}'\n"
            f"    Reason: {exc}\n"
            "    Expected format: arn:aws:connect:<region>:<account>:instance/<instance-id>"
        )


def validate_config() -> None:
    errors = []
    if INSTANCE_ARN == "YOUR_CONNECT_INSTANCE_ARN":
        errors.append("  • INSTANCE_ARN is not set")
    if CONTACT_FLOW_ARN == "YOUR_CONTACT_FLOW_ARN":
        errors.append("  • CONTACT_FLOW_ARN is not set")
    if errors:
        sys.exit(
            "❌  Please update the following constants at the top of the script before running:\n"
            + "\n".join(errors)
        )


def get_connect_client():
    return boto3.client("connect", region_name=DEFAULT_REGION)


# ------------------------------------------------------------
# CSV helpers
# ------------------------------------------------------------

def _init_single_csv(filepath: str, headers: list) -> None:
    """Create a CSV file with the given headers if it does not already exist."""
    if not os.path.exists(filepath):
        with open(filepath, mode="w", newline="") as f:
            csv.DictWriter(f, fieldnames=headers).writeheader()
        print(f"📄  Created  : {filepath}")
    else:
        print(f"📄  Existing : {filepath}  (will append)")


def init_csv_files() -> None:
    """Initialise both CSV files on startup."""
    _init_single_csv(CLAIMED_CSV_FILE,  CLAIMED_CSV_HEADERS)
    _init_single_csv(RELEASED_CSV_FILE, RELEASED_CSV_HEADERS)


def append_to_claimed_csv(row: dict) -> None:
    """Append a claimed-number record to the claimed CSV."""
    with open(CLAIMED_CSV_FILE, mode="a", newline="") as f:
        csv.DictWriter(f, fieldnames=CLAIMED_CSV_HEADERS).writerow(row)
    print(f"💾  Saved to {CLAIMED_CSV_FILE}")


def append_to_released_csv(row: dict) -> None:
    """Append a released-number record to the released CSV."""
    with open(RELEASED_CSV_FILE, mode="a", newline="") as f:
        csv.DictWriter(f, fieldnames=RELEASED_CSV_HEADERS).writerow(row)
    print(f"💾  Saved to {RELEASED_CSV_FILE}")


def update_claimed_csv_status(phone_number_id: str, new_status: str) -> None:
    """
    Update the status column for a given phone_number_id in the claimed CSV.
    Rewrites the file in-place.
    """
    rows    = []
    updated = False

    with open(CLAIMED_CSV_FILE, mode="r", newline="") as f:
        for row in csv.DictReader(f):
            if row["phone_number_id"] == phone_number_id:
                row["status"] = new_status
                updated = True
            rows.append(row)

    if not updated:
        print(f"⚠️   ID '{phone_number_id}' not found in claimed CSV — status not updated.")
        return

    with open(CLAIMED_CSV_FILE, mode="w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CLAIMED_CSV_HEADERS)
        writer.writeheader()
        writer.writerows(rows)

    print(f"✏️   Status updated to '{new_status}' in {CLAIMED_CSV_FILE}")


def load_releasable_rows() -> list:
    """
    Read the claimed CSV and return rows eligible for release.
    Eligible statuses: 'claimed', 'association_failed'.
    """
    if not os.path.exists(CLAIMED_CSV_FILE):
        print(f"❌  Claimed CSV not found: {CLAIMED_CSV_FILE}\n    Claim a number first.")
        return []

    with open(CLAIMED_CSV_FILE, mode="r", newline="") as f:
        return [
            row for row in csv.DictReader(f)
            if row.get("status") in ("claimed", "association_failed")
        ]


# ------------------------------------------------------------
# API helpers — Claim
# ------------------------------------------------------------

def search_available_numbers(client, number_type: str, prefix: str | None) -> list:
    """
    Search for available US phone numbers of the given type.

    API: connect.search_available_phone_numbers
      Required : TargetArn, PhoneNumberCountryCode, PhoneNumberType
      Optional : PhoneNumberPrefix (must include +, e.g. '+1972'), MaxResults, NextToken
      Response : AvailableNumbersList[{PhoneNumber, PhoneNumberCountryCode, PhoneNumberType}]
    """
    kwargs = dict(
        TargetArn=INSTANCE_ARN,
        PhoneNumberCountryCode=PHONE_COUNTRY_CODE,
        PhoneNumberType=number_type,
        MaxResults=10,
    )
    if prefix:
        kwargs["PhoneNumberPrefix"] = prefix

    try:
        resp = client.search_available_phone_numbers(**kwargs)
        return resp.get("AvailableNumbersList", [])
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code in ("InvalidParameterException", "ResourceNotFoundException"):
            print(f"⚠️   Search failed for type '{number_type}': {exc.response['Error']['Message']}")
            return []
        raise
    finally:
        print(f"   ⏳  Waiting {WAIT_AFTER_SEARCH}s (rate-limit buffer)…")
        time.sleep(WAIT_AFTER_SEARCH)


def claim_phone_number(client, phone_number: str) -> dict:
    """
    Claim (purchase) a phone number and attach it to the hard-coded instance.

    API: connect.claim_phone_number
      Required : TargetArn, PhoneNumber
      Optional : PhoneNumberDescription, Tags, ClientToken
      Response : {PhoneNumberId, PhoneNumberArn}
      NOTE     : PhoneNumberType / PhoneNumberCountryCode are NOT accepted — they are
                 inferred from the number itself.
      NOTE     : Claiming is asynchronous. Use describe_phone_number to confirm status.
    """
    try:
        resp = client.claim_phone_number(
            TargetArn=INSTANCE_ARN,
            PhoneNumber=phone_number,
        )
        return resp
    except ClientError:
        raise
    finally:
        print(f"   ⏳  Waiting {WAIT_AFTER_CLAIM}s (rate-limit buffer)…")
        time.sleep(WAIT_AFTER_CLAIM)


def poll_claim_status(client, phone_number_id: str) -> str:
    """
    Poll describe_phone_number until the claim status is no longer IN_PROGRESS.

    API: connect.describe_phone_number
      Required : PhoneNumberId
      Response : ClaimedPhoneNumberSummary.PhoneNumberStatus.Status
                 values: CLAIMED | IN_PROGRESS | FAILED

    Returns the final status string ('CLAIMED' or 'FAILED'), or 'UNKNOWN' on timeout.
    """
    for attempt in range(1, CLAIM_POLL_MAX_ATTEMPTS + 1):
        try:
            resp   = client.describe_phone_number(PhoneNumberId=phone_number_id)
            status = (
                resp.get("ClaimedPhoneNumberSummary", {})
                    .get("PhoneNumberStatus", {})
                    .get("Status", "UNKNOWN")
            )
        except ClientError as exc:
            print(f"   ⚠️   describe_phone_number error: {exc}")
            return "UNKNOWN"

        if status != "IN_PROGRESS":
            return status

        print(f"   ⏳  Claim IN_PROGRESS — polling again in {CLAIM_POLL_INTERVAL}s "
              f"(attempt {attempt}/{CLAIM_POLL_MAX_ATTEMPTS})…")
        time.sleep(CLAIM_POLL_INTERVAL)

    print(f"   ⚠️   Timed out waiting for claim to complete after {CLAIM_POLL_MAX_ATTEMPTS} attempts.")
    return "UNKNOWN"


def associate_phone_to_flow(client, instance_id: str, phone_number_id: str) -> None:
    """
    Associate a claimed phone number with the hard-coded contact flow.

    API: connect.associate_phone_number_contact_flow
      Required : PhoneNumberId, InstanceId, ContactFlowId
      NOTE     : ContactFlowId accepts a UUID or a full ARN.
    """
    try:
        client.associate_phone_number_contact_flow(
            PhoneNumberId=phone_number_id,
            InstanceId=instance_id,
            ContactFlowId=CONTACT_FLOW_ARN,
        )
    except ClientError:
        raise
    finally:
        print(f"   ⏳  Waiting {WAIT_AFTER_ASSOCIATE}s (rate-limit buffer)…")
        time.sleep(WAIT_AFTER_ASSOCIATE)


# ------------------------------------------------------------
# API helpers — Release
# ------------------------------------------------------------

def disassociate_phone_from_flow(client, instance_id: str, phone_number_id: str) -> None:
    """
    Remove the contact flow association from a phone number.

    API: connect.disassociate_phone_number_contact_flow
      Required : PhoneNumberId, InstanceId
    """
    try:
        client.disassociate_phone_number_contact_flow(
            PhoneNumberId=phone_number_id,
            InstanceId=instance_id,
        )
    except ClientError:
        raise
    finally:
        print(f"   ⏳  Waiting {WAIT_AFTER_DISASSOCIATE}s (rate-limit buffer)…")
        time.sleep(WAIT_AFTER_DISASSOCIATE)


def release_phone_number(client, phone_number_id: str) -> None:
    """
    Release (return) a phone number back to the pool.

    API: connect.release_phone_number
      Required : PhoneNumberId
      Optional : ClientToken
      NOTE     : InstanceId is NOT a parameter for this call.
      NOTE     : Must be called in the same AWS Region where the number was claimed.
      WARNING  : Released numbers enter a 180-day cooldown and cannot be reclaimed.
    """
    try:
        client.release_phone_number(PhoneNumberId=phone_number_id)
    except ClientError:
        raise
    finally:
        print(f"   ⏳  Waiting {WAIT_AFTER_RELEASE}s (rate-limit buffer)…")
        time.sleep(WAIT_AFTER_RELEASE)


# ------------------------------------------------------------
# CLAIM workflow
# ------------------------------------------------------------

def run_claim(client, instance_id: str) -> None:
    separator()
    print("  📲  CLAIM A PHONE NUMBER")
    separator()
    print(f"  Instance ARN : {INSTANCE_ARN}")
    print(f"  Instance ID  : {instance_id}")
    print(f"  Contact Flow : {CONTACT_FLOW_ARN}")
    separator("-")

    # Choose number type
    print("\nNumber type options:")
    for idx, t in enumerate(SUPPORTED_TYPES, 1):
        default_marker = "  ← default" if t == DEFAULT_NUMBER_TYPE else ""
        print(f"  [{idx}] {t}{default_marker}")
    type_choice = prompt(
        f"\nSelect number type [1-{len(SUPPORTED_TYPES)}] "
        f"(press Enter for default '{DEFAULT_NUMBER_TYPE}'): "
    )

    if type_choice == "":
        number_type = DEFAULT_NUMBER_TYPE
    else:
        try:
            number_type = SUPPORTED_TYPES[int(type_choice) - 1]
        except (ValueError, IndexError):
            print("⚠️   Invalid selection — using default.")
            number_type = DEFAULT_NUMBER_TYPE
    print(f"   ✔  Type: {number_type}")

    # Optional area code prefix
    raw_prefix = prompt("\nEnter area code prefix to filter (e.g. +1972) or press Enter to skip: ")
    prefix = raw_prefix if raw_prefix else None

    # Search for available numbers
    print(f"\n🔍  Searching for available US {number_type} numbers…")
    candidates = search_available_numbers(client, number_type, prefix)

    if not candidates:
        print(
            "\n❌  No available numbers found."
            + (" Try a different prefix or skip the filter." if prefix else "")
        )
        return

    print(f"\nAvailable numbers ({len(candidates)} found):")
    for idx, num in enumerate(candidates, 1):
        print(f"  [{idx}] {num['PhoneNumber']}")

    # Select a number
    num_choice = prompt("\nSelect number to claim (or 'q' to cancel): ")
    if num_choice.lower() == "q":
        print("Cancelled.")
        return
    try:
        selected     = candidates[int(num_choice) - 1]
        phone_number = selected["PhoneNumber"]
    except (ValueError, IndexError):
        print("❌  Invalid selection.")
        return

    # Claim
    print(f"\n📲  Claiming {phone_number}…")
    try:
        claim_resp = claim_phone_number(client, phone_number)
    except ClientError as exc:
        print(f"❌  Failed to claim number: {exc}")
        return

    phone_number_id  = claim_resp["PhoneNumberId"]
    phone_number_arn = claim_resp["PhoneNumberArn"]
    claimed_at       = now_utc()
    print(f"✅  Claim submitted : {phone_number}  (ID: {phone_number_id})")

    # Poll until claim status is resolved (CLAIMED or FAILED)
    print("\n🔄  Verifying claim status via describe_phone_number…")
    claim_status = poll_claim_status(client, phone_number_id)

    if claim_status == "FAILED":
        print(f"❌  Claim failed according to describe_phone_number. Number will be auto-released within 24 hours.")
        return
    elif claim_status == "UNKNOWN":
        print("⚠️   Could not confirm claim status — proceeding with association attempt anyway.")
    else:
        print(f"✅  Claim confirmed : status = {claim_status}")

    # Associate with contact flow
    status = "claimed"
    print("\n🔗  Associating with contact flow…")
    try:
        associate_phone_to_flow(client, instance_id, phone_number_id)
        print("✅  Associated successfully.")
    except ClientError as exc:
        status = "association_failed"
        print(f"⚠️   Association failed: {exc}")
        print(f"    Phone Number ID  : {phone_number_id}")
        print(f"    Contact Flow ARN : {CONTACT_FLOW_ARN}")
        print("    Associate it manually in the Connect console if needed.")

    # Write to claimed CSV
    append_to_claimed_csv({
        "timestamp":        claimed_at,
        "phone_number":     phone_number,
        "number_type":      number_type,
        "phone_number_id":  phone_number_id,
        "phone_number_arn": phone_number_arn,
        "contact_flow_arn": CONTACT_FLOW_ARN,
        "instance_id":      instance_id,
        "instance_arn":     INSTANCE_ARN,
        "status":           status,
    })

    separator()
    print("  ✅  Claim complete!" if status == "claimed" else "  ⚠️   Claim done with warnings.")
    print(f"  Phone Number    : {phone_number}")
    print(f"  Number Type     : {number_type}")
    print(f"  Phone Number ID : {phone_number_id}")
    print(f"  Status          : {status}")
    print(f"  CSV             : {os.path.abspath(CLAIMED_CSV_FILE)}")
    separator()


# ------------------------------------------------------------
# RELEASE workflow
# ------------------------------------------------------------

def run_release(client, instance_id: str) -> None:
    separator()
    print("  🔓  RELEASE PHONE NUMBERS")
    separator()

    eligible = load_releasable_rows()

    if not eligible:
        print("❌  No releasable numbers found in the claimed CSV.")
        print("    Only rows with status 'claimed' or 'association_failed' are eligible.")
        return

    print(f"\nNumbers eligible for release ({len(eligible)} found):\n")
    for idx, row in enumerate(eligible, 1):
        print(
            f"  [{idx}] {row['phone_number']:<20}  "
            f"ID: {row['phone_number_id']}  "
            f"Status: {row['status']}"
        )

    # Select numbers to release
    print(f"\n  [A] Release ALL {len(eligible)} number(s)")
    choice = prompt("\nEnter selection (e.g. 1 or 1,3 — or A for all — or 'q' to cancel): ")

    if choice.lower() == "q":
        print("Cancelled.")
        return
    elif choice.lower() == "a":
        selected_rows = eligible
    else:
        try:
            indices       = [int(x.strip()) - 1 for x in choice.split(",")]
            selected_rows = [eligible[i] for i in indices]
        except (ValueError, IndexError):
            print("❌  Invalid selection.")
            return

    # Confirm — releases are irreversible
    print(f"\n⚠️   You are about to PERMANENTLY release {len(selected_rows)} number(s):")
    for row in selected_rows:
        print(f"      {row['phone_number']}  (ID: {row['phone_number_id']})")
    print("\n    Released numbers enter a 180-day cooldown and cannot be reclaimed.")
    confirm = prompt("\n    Type 'yes' to confirm, or anything else to cancel: ").lower()
    if confirm != "yes":
        print("Cancelled.")
        return

    # Release each selected number
    released_count = 0
    failed_count   = 0

    for row in selected_rows:
        phone_number_id = row["phone_number_id"]
        phone_number    = row["phone_number"]

        print(f"\n🔓  Processing {phone_number} (ID: {phone_number_id})…")

        # Step 1: disassociate from contact flow (best-effort)
        print("   ↳ Disassociating from contact flow…")
        try:
            disassociate_phone_from_flow(client, instance_id, phone_number_id)
            print("   ✅  Disassociated.")
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code in ("ResourceNotFoundException", "InvalidParameterException"):
                # Already disassociated or never was — safe to proceed to release
                print(f"   ℹ️   Skipped: {exc.response['Error']['Message']}")
            else:
                # Non-fatal — log and continue to the release attempt
                print(f"   ⚠️   Disassociation failed (non-fatal, continuing): {exc}")

        # Step 2: release the number
        print("   ↳ Releasing…")
        try:
            release_phone_number(client, phone_number_id)
            print(f"   ✅  Released: {phone_number}")

            # Update status in claimed CSV
            update_claimed_csv_status(phone_number_id, "released")

            # Append full record to released CSV
            append_to_released_csv({
                "released_at":      now_utc(),
                "phone_number":     phone_number,
                "number_type":      row.get("number_type", ""),
                "phone_number_id":  phone_number_id,
                "phone_number_arn": row.get("phone_number_arn", ""),
                "contact_flow_arn": row.get("contact_flow_arn", ""),
                "instance_id":      row.get("instance_id", ""),
                "instance_arn":     row.get("instance_arn", ""),
                "claimed_at":       row.get("timestamp", ""),
            })

            released_count += 1

        except ClientError as exc:
            print(f"   ❌  Release failed for {phone_number}: {exc}")
            update_claimed_csv_status(phone_number_id, "release_failed")
            failed_count += 1

    separator()
    print("  Release complete.")
    print(f"  ✅  Released     : {released_count}")
    if failed_count:
        print(f"  ❌  Failed       : {failed_count}")
    print(f"  Claimed CSV     : {os.path.abspath(CLAIMED_CSV_FILE)}")
    print(f"  Released CSV    : {os.path.abspath(RELEASED_CSV_FILE)}")
    separator()


# ------------------------------------------------------------
# Main menu
# ------------------------------------------------------------

def main_menu(client, instance_id: str) -> None:
    while True:
        print()
        separator()
        print("  Amazon Connect Phone Number Manager")
        separator()
        print("  [1] Claim a phone number")
        print("  [2] Release phone number(s)")
        print("  [q] Quit")
        separator("-")
        choice = prompt("  Select an option: ")

        if choice == "1":
            run_claim(client, instance_id)
        elif choice == "2":
            run_release(client, instance_id)
        elif choice.lower() == "q":
            print("\nGoodbye.\n")
            sys.exit(0)
        else:
            print("⚠️   Invalid option. Please enter 1, 2, or q.")


# ------------------------------------------------------------
# Entry point
# ------------------------------------------------------------

if __name__ == "__main__":
    validate_config()
    instance_id = extract_instance_id(INSTANCE_ARN)
    init_csv_files()
    client = get_connect_client()
    main_menu(client, instance_id)
