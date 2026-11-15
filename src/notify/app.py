"""Park the Step Functions task token on the request item.

Invoked by the state machine with waitForTaskToken. The token is what lets a
human (or any external system) answer the workflow later through a plain HTTP
call, without any long-running server. In a production setup this is also
where you would fan out notifications (email, Slack, SNS).
"""

import os
from datetime import datetime, timezone

import boto3

dynamodb = boto3.resource("dynamodb")
TABLE = os.environ["TABLE_NAME"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds") + "Z"


def lambda_handler(event, context):
    request_id = event["requestId"]
    token = event["token"]

    dynamodb.Table(TABLE).update_item(
        Key={"requestId": request_id},
        UpdateExpression=(
            "SET #st = :status, taskToken = :token, "
            "history = list_append(if_not_exists(history, :empty), :evt)"
        ),
        ExpressionAttributeNames={"#st": "status"},
        ExpressionAttributeValues={
            ":status": "AWAITING_APPROVAL",
            ":token": token,
            ":empty": [],
            ":evt": [{"ts": _now(), "event": "AWAITING_APPROVAL", "by": "system", "comment": None}],
        },
    )

    return {"queued": True, "requestId": request_id}
