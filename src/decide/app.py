"""Human decision endpoint: approve or reject a change request.

Flow:
1. Load the item, make sure it is actually waiting for a decision.
2. Conditionally claim the decision (prevents double approvals by racing
   approvers), then resolve the Step Functions callback with SendTaskSuccess.
3. If the workflow already timed out, mark the request EXPIRED and say so.

The conditional update is what makes the whole thing safe under concurrency:
only one approver can transition a request out of AWAITING_APPROVAL.
"""

import json
import os
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

dynamodb = boto3.resource("dynamodb")
sfn = boto3.client("stepfunctions")

TABLE = os.environ["TABLE_NAME"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds") + "Z"


def _respond(status: int, payload: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(payload, default=str),
    }


def lambda_handler(event, context):
    request_id = (event.get("pathParameters") or {}).get("id")
    if not request_id:
        return _respond(400, {"error": "missing request id"})

    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _respond(400, {"error": "invalid JSON body"})

    decision = str(body.get("decision") or "").strip().lower()
    if decision not in {"approve", "reject"}:
        return _respond(400, {"error": "decision must be 'approve' or 'reject'"})
    decided_by = str(body.get("by") or "approver").strip()
    comment = str(body.get("comment") or "").strip() or None

    table = dynamodb.Table(TABLE)
    item = table.get_item(Key={"requestId": request_id}).get("Item")
    if not item:
        return _respond(404, {"error": "request not found"})
    if item.get("status") != "AWAITING_APPROVAL":
        return _respond(409, {"error": f"request is {item.get('status')}, not AWAITING_APPROVAL"})

    token = item.get("taskToken")
    if not token:
        return _respond(409, {"error": "no pending approval callback for this request"})

    event_name = "APPROVED" if decision == "approve" else "REJECTED"
    try:
        table.update_item(
            Key={"requestId": request_id},
            UpdateExpression=(
                "SET #st = :status, decidedBy = :by, decidedAt = :now, "
                "history = list_append(if_not_exists(history, :empty), :evt)"
            ),
            ConditionExpression="#st = :await",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={
                ":status": "DECIDED",
                ":by": decided_by,
                ":now": _now(),
                ":await": "AWAITING_APPROVAL",
                ":empty": [],
                ":evt": [{"ts": _now(), "event": event_name, "by": decided_by, "comment": comment}],
            },
        )
    except ClientError as err:
        if err.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return _respond(409, {"error": "request was already decided by someone else"})
        raise

    try:
        sfn.send_task_success(
            taskToken=token,
            output=json.dumps({"decision": decision, "by": decided_by, "comment": comment}),
        )
    except ClientError as err:
        code = err.response["Error"]["Code"]
        if code in {"TaskTimedOut", "TaskDoesNotExist", "InvalidToken"}:
            table.update_item(
                Key={"requestId": request_id},
                UpdateExpression="SET #st = :status",
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={":status": "EXPIRED"},
            )
            return _respond(410, {"error": "approval window has expired"})
        raise

    return _respond(200, {"requestId": request_id, "decision": decision, "by": decided_by})
