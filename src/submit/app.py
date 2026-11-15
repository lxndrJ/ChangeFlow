"""Submit a new engineering change request.

Validates the payload, freezes a canonical snapshot in S3 (the same idea as a
frozen report snapshot in a PLM: the record can never be silently rewritten
later), persists the item to DynamoDB and starts the Step Functions workflow.
"""

import json
import os
import uuid
from datetime import datetime, timezone

import boto3

dynamodb = boto3.resource("dynamodb")
s3 = boto3.client("s3")
sfn = boto3.client("stepfunctions")

TABLE = os.environ["TABLE_NAME"]
BUCKET = os.environ["BUCKET_NAME"]
STATE_MACHINE_ARN = os.environ["STATE_MACHINE_ARN"]

VALID_IMPACTS = {"NONE", "LOW", "MEDIUM", "HIGH"}
REQUIRED = ["partNumber", "title", "description", "impact", "requestedBy"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds") + "Z"


def _respond(status: int, payload: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(payload, default=str),
    }


def _clean(value) -> str:
    return str(value or "").strip()


def lambda_handler(event, context):
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _respond(400, {"error": "invalid JSON body"})

    missing = [f for f in REQUIRED if not _clean(body.get(f))]
    if missing:
        return _respond(400, {"error": "missing fields: " + ", ".join(missing)})

    impact = _clean(body.get("impact")).upper()
    if impact not in VALID_IMPACTS:
        return _respond(400, {"error": "impact must be one of NONE, LOW, MEDIUM, HIGH"})

    request_id = f"cr-{uuid.uuid4().hex[:12]}"
    submitted_at = _now()
    item = {
        "requestId": request_id,
        "partNumber": _clean(body.get("partNumber")),
        "title": _clean(body.get("title")),
        "description": _clean(body.get("description")),
        "impact": impact,
        "requestedBy": _clean(body.get("requestedBy")),
        "status": "VALIDATING",
        "submittedAt": submitted_at,
        "history": [
            {"ts": submitted_at, "event": "SUBMITTED", "by": _clean(body.get("requestedBy")), "comment": None}
        ],
    }

    snapshot_key = f"requests/{request_id}/snapshot.json"
    s3.put_object(
        Bucket=BUCKET,
        Key=snapshot_key,
        Body=json.dumps(item, indent=2, default=str).encode("utf-8"),
        ContentType="application/json",
    )

    table = dynamodb.Table(TABLE)
    table.put_item(Item=item)

    sfn.start_execution(
        stateMachineArn=STATE_MACHINE_ARN,
        name=request_id,
        input=json.dumps(item, default=str),
    )

    return _respond(201, {"request": item, "snapshotKey": snapshot_key})
