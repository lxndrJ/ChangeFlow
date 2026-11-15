"""GET /requests - list change requests.

Without a filter this is a Scan (fine for a demo-scale table). With
?status=<STATUS> it queries the statusIndex GSI, which is the pattern you
would keep for production.
"""

import json
import os

import boto3

dynamodb = boto3.resource("dynamodb")
TABLE = os.environ["TABLE_NAME"]


def _respond(status: int, payload: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(payload, default=str),
    }


def lambda_handler(event, context):
    params = event.get("queryStringParameters") or {}
    status = params.get("status")

    table = dynamodb.Table(TABLE)
    if status:
        result = table.query(
            IndexName="statusIndex",
            KeyConditionExpression="#st = :s",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={":s": status.upper()},
        )
    else:
        result = table.scan()

    items = sorted(result.get("Items", []), key=lambda i: i.get("submittedAt", ""), reverse=True)
    # The task token is an internal secret of the workflow, never expose it.
    for item in items:
        item.pop("taskToken", None)

    return _respond(200, {"requests": items, "count": len(items)})
