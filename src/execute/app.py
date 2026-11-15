"""Apply an approved change.

Called by the state machine after a successful human approval. Writes the
terminal state to DynamoDB and stores an immutable audit record in S3, so the
full transition (who approved what, when, with which frozen snapshot) can be
reconstructed without trusting the mutable DynamoDB item alone.
"""

import json
import os
from datetime import datetime, timezone

import boto3

dynamodb = boto3.resource("dynamodb")
s3 = boto3.client("s3")

TABLE = os.environ["TABLE_NAME"]
BUCKET = os.environ["BUCKET_NAME"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds") + "Z"


def lambda_handler(event, context):
    request_id = event["requestId"]
    decision = event.get("decision") or {}
    # The callback task result is the raw SendTaskSuccess output. Tolerate a
    # {"Payload": ...} wrapper too in case of direct lambda:invoke handoff.
    if isinstance(decision.get("Payload"), dict):
        decision = decision["Payload"]
    decided_by = decision.get("by") or "approver"
    comment = decision.get("comment")

    table = dynamodb.Table(TABLE)
    updated_at = _now()
    table.update_item(
        Key={"requestId": request_id},
        UpdateExpression=(
            "SET #st = :status, executedAt = :now, "
            "history = list_append(if_not_exists(history, :empty), :evt)"
        ),
        ExpressionAttributeNames={"#st": "status"},
        ExpressionAttributeValues={
            ":status": "EXECUTED",
            ":now": updated_at,
            ":empty": [],
            ":evt": [{"ts": updated_at, "event": "EXECUTED", "by": "system", "comment": comment}],
        },
    )

    record = {
        "type": "CHANGE_EXECUTED",
        "requestId": request_id,
        "partNumber": event.get("partNumber"),
        "title": event.get("title"),
        "impact": event.get("impact"),
        "requestedBy": event.get("requestedBy"),
        "submittedAt": event.get("submittedAt"),
        "approvedBy": decided_by,
        "approvalComment": comment,
        "executedAt": updated_at,
    }
    audit_key = f"audit/{request_id}.json"
    s3.put_object(
        Bucket=BUCKET,
        Key=audit_key,
        Body=json.dumps(record, indent=2, default=str).encode("utf-8"),
        ContentType="application/json",
    )

    return {"status": "EXECUTED", "requestId": request_id, "auditKey": audit_key, "executedAt": updated_at}
