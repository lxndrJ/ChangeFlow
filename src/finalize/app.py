"""Close a request that did not make it through: INVALID, REJECTED or EXPIRED.

Invoked directly by the state machine on the terminal paths. Mirrors the
execute function: terminal status in DynamoDB plus an immutable audit record
in S3.
"""

import json
import os
from datetime import datetime, timezone

import boto3

dynamodb = boto3.resource("dynamodb")
s3 = boto3.client("s3")

TABLE = os.environ["TABLE_NAME"]
BUCKET = os.environ["BUCKET_NAME"]

OUTCOME_EVENT = {
    "INVALID": "VALIDATION_FAILED",
    "EXPIRED": "EXPIRED",
    # REJECTED is already recorded by the decide handler (who, comment),
    # so finalize does not append a duplicate history event for it.
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds") + "Z"


def lambda_handler(event, context):
    request_id = event["requestId"]
    outcome = event.get("outcome", "EXPIRED").upper()
    reason = event.get("reason")
    if isinstance(reason, list):
        reason = "; ".join(str(r) for r in reason)
    decision = event.get("decision") or {}
    actor = decision.get("by") or "system"
    comment = decision.get("comment")

    closed_at = _now()
    history_event = OUTCOME_EVENT.get(outcome)

    table = dynamodb.Table(TABLE)
    table.update_item(
        Key={"requestId": request_id},
        UpdateExpression=(
            "SET #st = :status, closedAt = :now, "
            "history = list_append(if_not_exists(history, :empty), :evt)"
        ),
        ExpressionAttributeNames={"#st": "status"},
        ExpressionAttributeValues={
            ":status": outcome,
            ":now": closed_at,
            ":empty": [],
            ":evt": [{"ts": closed_at, "event": history_event, "by": actor, "comment": reason or comment}]
            if history_event else [],
        },
    )

    record = {
        "type": "CHANGE_CLOSED",
        "requestId": request_id,
        "outcome": outcome,
        "reason": reason,
        "decidedBy": actor,
        "comment": comment,
        "closedAt": closed_at,
    }
    s3.put_object(
        Bucket=BUCKET,
        Key=f"audit/{request_id}.json",
        Body=json.dumps(record, indent=2, default=str).encode("utf-8"),
        ContentType="application/json",
    )

    return {"status": outcome, "requestId": request_id, "closedAt": closed_at}
