# Amazon Connect Phone Number Manager

A serverless tool for searching, claiming (purchasing), and releasing US phone numbers on Amazon Connect. Deployed as an AWS Lambda function via CloudFormation with S3-backed CSV record keeping.

---

## Architecture

```
                 +------------------+
  Event JSON --> |  Lambda Function | --> Amazon Connect APIs
                 |  (Python 3.12)  |     (search/claim/release)
                 +--------+---------+
                          |
                          v
                 +------------------+
                 |    S3 Bucket     |
                 |  (CSV records)   |
                 +------------------+
                   {prefix}/{run_id}/claimed_phone_numbers.csv
                   {prefix}/{run_id}/released_phone_numbers.csv
```

**Resources created by the stack:**

| Resource | Purpose |
|---|---|
| `PhoneManagerFunction` | Lambda that handles search, claim, and release actions |
| `PhoneManagerBucket` | Versioned S3 bucket for CSV audit records |
| `PhoneManagerLambdaRole` | IAM role with Connect, S3, and CloudWatch permissions |
| `PhoneManagerTestFunction` | Lambda that runs automated validation on deploy |
| `PhoneManagerLogGroup` | CloudWatch log group (30-day retention) |

---

## Prerequisites

- AWS CLI v2 configured with credentials that have `cloudformation:*`, `iam:*`, `lambda:*`, `s3:*` permissions
- An active Amazon Connect instance
- A contact flow ARN to associate with claimed numbers

---

## Deployment

### 1. Gather your Connect ARNs

```bash
# List your Connect instances
aws connect list-instances --region us-east-1

# List contact flows for a given instance
aws connect list-contact-flows \
  --instance-id <INSTANCE_ID> \
  --region us-east-1 \
  --query "ContactFlowSummaryList[?ContactFlowType=='CONTACT_FLOW'].[Name,Arn]" \
  --output table
```

### 2. Deploy the stack

```bash
aws cloudformation deploy \
  --template-file template.yaml \
  --stack-name connect-phone-manager \
  --region us-east-1 \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    ConnectInstanceArn="arn:aws:connect:us-east-1:<ACCOUNT>:instance/<INSTANCE_ID>" \
    ContactFlowArn="arn:aws:connect:us-east-1:<ACCOUNT>:instance/<INSTANCE_ID>/contact-flow/<FLOW_ID>"
```

Deployment automatically runs 6 validation tests. The stack will fail to create if any test fails.

### 3. Verify

```bash
aws cloudformation describe-stacks \
  --stack-name connect-phone-manager \
  --region us-east-1 \
  --query "Stacks[0].Outputs" \
  --output table
```

Expected outputs include `TestSummary: 6/6 tests passed` and `TestAllPassed: True`.

---

## CloudFormation Parameters

| Parameter | Required | Default | Description |
|---|---|---|---|
| `ConnectInstanceArn` | Yes | - | Amazon Connect instance ARN |
| `ContactFlowArn` | Yes | - | Contact flow ARN for number association |
| `ConnectRegion` | No | `us-east-1` | AWS region of the Connect instance |
| `S3BucketName` | No | `connect-phone-manager-<account-id>` | Override S3 bucket name |
| `S3Prefix` | No | `connect-phone-manager` | S3 key prefix for CSV files |
| `LogLevel` | No | `INFO` | Lambda log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `LambdaTimeout` | No | `900` | Lambda timeout in seconds (30-900). Max recommended for auto-claim. |
| `LambdaMemory` | No | `256` | Lambda memory in MB (128, 256, 512, 1024) |
| `CsvRetentionDays` | No | `365` | Days to retain CSV files in S3 before auto-deletion (1-3650) |

---

## Usage

Invoke the Lambda with a JSON event. The `instance_arn` and `contact_flow_arn` fields are optional in the event payload -- they default to the values set during deployment via CloudFormation parameters.

### Search for available numbers

```bash
aws lambda invoke \
  --function-name ConnectPhoneManager-connect-phone-manager \
  --region us-east-1 \
  --cli-binary-format raw-in-base64-out \
  --payload '{
    "action": "search",
    "number_type": "DID",
    "max_results": 5
  }' \
  output.json && cat output.json | python -m json.tool
```

With an area code filter:

```bash
aws lambda invoke \
  --function-name ConnectPhoneManager-connect-phone-manager \
  --region us-east-1 \
  --cli-binary-format raw-in-base64-out \
  --payload '{
    "action": "search",
    "number_type": "DID",
    "prefix": "+1972",
    "max_results": 10
  }' \
  output.json
```

**Response:**
```json
{
  "statusCode": 200,
  "body": {
    "available_numbers": [
      {"PhoneNumber": "+18625551234", "PhoneNumberCountryCode": "US", "PhoneNumberType": "DID"}
    ],
    "count": 1
  }
}
```

### Claim (purchase) phone numbers

Two modes: **auto-claim by count** (just say how many) or **explicit phone numbers** (pick specific numbers from search results). Every claimed number gets a description visible in the Amazon Connect console.

#### Auto-claim by count (recommended)

Just specify the type and how many. The Lambda searches for available numbers and claims them automatically:

```bash
aws lambda invoke \
  --function-name ConnectPhoneManager-connect-phone-manager \
  --region us-east-1 \
  --cli-binary-format raw-in-base64-out \
  --payload '{
    "action": "claim",
    "number_type": "DID",
    "count": 95
  }' \
  output.json
```

The Lambda will:
1. Search for available numbers in batches
2. Claim each one, poll for confirmation, associate with the contact flow
3. Monitor its remaining execution time and stop safely 30s before timeout
4. Record each claimed number to the S3 CSV as it goes

**Response:**
```json
{
  "statusCode": 200,
  "body": {
    "run_id": "20260330-143022",
    "results": [...],
    "summary": {
      "requested": 95,
      "claimed": 52,
      "remaining": 43,
      "failed": 0
    }
  }
}
```

If `remaining > 0` (the Lambda ran out of time), re-invoke with the same `run_id` to continue:

```bash
aws lambda invoke \
  --function-name ConnectPhoneManager-connect-phone-manager \
  --region us-east-1 \
  --cli-binary-format raw-in-base64-out \
  --payload '{
    "action": "claim",
    "number_type": "DID",
    "count": 43,
    "run_id": "20260330-143022"
  }' \
  output.json
```

With the default 900s timeout, each invocation handles ~50-60 numbers. All results append to the same CSV when using the same `run_id`.

#### Explicit phone numbers

Pick specific numbers from search results:

```bash
aws lambda invoke \
  --function-name ConnectPhoneManager-connect-phone-manager \
  --region us-east-1 \
  --cli-binary-format raw-in-base64-out \
  --payload '{
    "action": "claim",
    "number_type": "DID",
    "phone_numbers": ["+18625551234", "+18625555678"]
  }' \
  output.json
```

#### Descriptions

Every claimed number gets a description (default: `"From phone number manager"`). You can customize it:

**Shared description for all numbers:**
```json
{
  "action": "claim",
  "number_type": "DID",
  "count": 10,
  "description": "Customer support lines"
}
```

**Per-number descriptions (explicit mode only, can mix with shared):**
```json
{
  "action": "claim",
  "number_type": "DID",
  "description": "General support",
  "phone_numbers": [
    "+18625551234",
    {"number": "+18625555678", "description": "Sales hotline"},
    {"number": "+18625559999", "description": "Billing dept"}
  ]
}
```

The first number gets `"General support"` (the shared fallback), while the other two get their own custom descriptions. Each description appears in the Amazon Connect console under the phone number's details.

The `run_id` in the response identifies the S3 folder where CSV records are stored. Pass the same `run_id` when releasing these numbers so the release updates the correct CSV files.

Each claimed number is:
1. Purchased and attached to your Connect instance with a description
2. Polled until the claim is confirmed (`CLAIMED` status)
3. Associated with the configured contact flow
4. Recorded in `{prefix}/{run_id}/claimed_phone_numbers.csv` on S3

**Status values:** `claimed`, `association_failed`, `failed`

### Release phone numbers

Pass the `phone_number_id` values returned from claim. Include the `run_id` from the claim response so the release updates the correct CSV files.

```bash
aws lambda invoke \
  --function-name ConnectPhoneManager-connect-phone-manager \
  --region us-east-1 \
  --cli-binary-format raw-in-base64-out \
  --payload '{
    "action": "release",
    "run_id": "20260330-143022",
    "phone_number_ids": ["a8a98137-538f-48a3-a97a-fcc74a3f5f59"]
  }' \
  output.json
```

**Response:**
```json
{
  "statusCode": 200,
  "body": {
    "run_id": "20260330-143022",
    "results": [
      {
        "phone_number_id": "a8a98137-538f-48a3-a97a-fcc74a3f5f59",
        "phone_number": "+18625551234",
        "status": "released",
        "error": null
      }
    ],
    "summary": {"released": 1, "failed": 0}
  }
}
```

Each released number is:
1. Disassociated from the contact flow (best-effort)
2. Released back to the AWS phone number pool
3. Updated to `released` in `{prefix}/{run_id}/claimed_phone_numbers.csv`
4. Appended to `{prefix}/{run_id}/released_phone_numbers.csv`

> **Warning:** Released numbers enter a **180-day cooldown** and cannot be reclaimed.

---

## Event Payload Reference

All actions accept these optional fields (default to CloudFormation parameter values):

| Field | Type | Description |
|---|---|---|
| `instance_arn` | string | Override the Connect instance ARN |
| `contact_flow_arn` | string | Override the contact flow ARN |
| `region` | string | Override the AWS region |
| `run_id` | string | Custom run identifier (default: auto-generated `YYYYMMDD-HHMMSS`) |

### Search

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `action` | string | Yes | - | `"search"` |
| `number_type` | string | No | `"DID"` | `"DID"` or `"TOLL_FREE"` |
| `prefix` | string | No | none | Area code filter, e.g. `"+1972"` |
| `max_results` | integer | No | `10` | 1-100 numbers to return |

### Claim

Provide **either** `count` (auto-search) **or** `phone_numbers` (explicit list), not both.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `action` | string | Yes | - | `"claim"` |
| `number_type` | string | Yes | - | `"DID"` or `"TOLL_FREE"` |
| `count` | integer | * | - | Number of phone numbers to auto-claim (1-500) |
| `phone_numbers` | array | * | - | Explicit E.164 strings or objects (see below) |
| `description` | string | No | `"From phone number manager"` | Description for claimed numbers |

\* Provide `count` or `phone_numbers`, not both.

**`phone_numbers` accepts two formats, which can be mixed:**

| Format | Example | Description used |
|---|---|---|
| Plain string | `"+18625551234"` | Shared `description` field (or default) |
| Object | `{"number": "+18625551234", "description": "Sales"}` | Per-number description (overrides shared) |

### Release

| Field | Type | Required | Description |
|---|---|---|---|
| `action` | string | Yes | `"release"` |
| `phone_number_ids` | string[] | Yes | IDs returned from claim |

---

## CSV Records (S3)

Each run produces its own CSV files in a separate S3 folder:

```
connect-phone-manager/
  20260330-143022/                    # auto-generated run_id
    claimed_phone_numbers.csv
    released_phone_numbers.csv
  20260330-160500/                    # a later run
    claimed_phone_numbers.csv
  batch-april-2026/                   # custom run_id
    claimed_phone_numbers.csv
```

Records are stored at `<S3Prefix>/<run_id>/` and automatically deleted after `CsvRetentionDays` (default 365 days).

### claimed_phone_numbers.csv

| Column | Description |
|---|---|
| `timestamp` | UTC time of claim |
| `phone_number` | E.164 number (e.g. `+18625551234`) |
| `number_type` | `DID` or `TOLL_FREE` |
| `phone_number_id` | AWS phone number ID |
| `phone_number_arn` | Full ARN of the phone number |
| `contact_flow_arn` | Contact flow it was associated with |
| `instance_id` | Connect instance ID |
| `instance_arn` | Connect instance ARN |
| `description` | Description visible in the Connect console |
| `status` | `claimed`, `association_failed`, `released`, `release_failed` |

### released_phone_numbers.csv

| Column | Description |
|---|---|
| `released_at` | UTC time of release |
| `phone_number` | E.164 number |
| `number_type` | `DID` or `TOLL_FREE` |
| `phone_number_id` | AWS phone number ID |
| `phone_number_arn` | Full ARN |
| `contact_flow_arn` | Original contact flow |
| `instance_id` | Original instance ID |
| `instance_arn` | Original instance ARN |
| `claimed_at` | Original claim timestamp |

---

## Reliability Features

- **Auto-claim by count** -- Specify how many numbers you need; the Lambda searches, claims, polls, and associates automatically in a loop
- **Timeout-aware** -- Auto-claim monitors remaining Lambda execution time and stops safely 30s before timeout, returning partial results with a `remaining` count so you can re-invoke
- **Retry with exponential backoff** -- All Connect API calls (search, claim, describe, associate, disassociate, release) automatically retry on `ThrottlingException`, `TooManyRequestsException`, `RequestLimitExceeded`, `ServiceUnavailable`, and `InternalServiceException` (up to 5 retries with jitter)
- **Pagination** -- Phone number search follows `NextToken` across pages to return up to 100 results
- **Batch operations** -- Claim and release support multiple numbers per invocation with per-number error isolation (one failure does not abort the batch)
- **Per-run isolation** -- Each invocation gets a unique `run_id` folder in S3; different runs never overwrite each other's CSV files
- **S3 lifecycle** -- CSV files auto-expire after `CsvRetentionDays` (default 365); noncurrent versions expire after 90 days
- **S3 versioning** -- CSV bucket has versioning enabled for accidental overwrite protection
- **Automated testing** -- 6 validation tests run automatically on every stack deploy

---

## File Structure

```
CLAIM PHONE NUMBER/
  template.yaml                     # CloudFormation template (all Lambda code inlined)
  connect_phone_manager.py          # Original CLI version (legacy reference)
  lambda/                           # Modular source for development/testing
    handler.py                      #   Lambda entry point
    connect_operations.py           #   Connect API wrappers + batch logic
    csv_storage.py                  #   S3-backed CSV operations
    config.py                       #   Configuration dataclass + logging
    retry.py                        #   Exponential backoff decorator
    validation.py                   #   Event payload validation
    requirements.txt                #   Python dependencies
```

The `template.yaml` contains the full Lambda code inlined in the `ZipFile` property. The `lambda/` directory is the modular equivalent used for local development and unit testing.

---

## Updating the Stack

```bash
aws cloudformation deploy \
  --template-file template.yaml \
  --stack-name connect-phone-manager \
  --region us-east-1 \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    ConnectInstanceArn="<YOUR_INSTANCE_ARN>" \
    ContactFlowArn="<YOUR_CONTACT_FLOW_ARN>"
```

To re-run the automated tests on update, change the `TestRunId` property in the `PhoneManagerTestInvocation` resource.

---

## Deleting the Stack

```bash
# Empty the S3 bucket first (required before deletion)
aws s3 rm s3://connect-phone-manager-<ACCOUNT_ID> --recursive

aws cloudformation delete-stack \
  --stack-name connect-phone-manager \
  --region us-east-1
```

> **Note:** Deleting the stack does not release phone numbers that are currently claimed. Release them first via the Lambda, or manually in the Connect console.

---

## Troubleshooting

| Issue | Resolution |
|---|---|
| Search returns no numbers | Try a different `number_type` (`TOLL_FREE`), remove the `prefix` filter, or try a different region |
| Claim fails with "Phone number not available" | The number was claimed between search and claim. Search again and pick a different number. |
| Association failed | The number was claimed but the contact flow association failed. Associate manually in the Connect console, or release and re-claim. |
| Stack deployment fails at test | Check the `PhoneManagerTestFunction` CloudWatch logs for details. Common cause: the Connect instance ARN or contact flow ARN is incorrect. |
| Auto-claim stopped early (`remaining > 0`) | The Lambda ran out of time. Re-invoke with the same `run_id` and the `remaining` count. Each invocation handles ~50-60 numbers at the default 900s timeout. |
| Throttling warnings in logs | Normal for large batches. The retry decorator handles this automatically with exponential backoff. No action needed unless all retries are exhausted. |
