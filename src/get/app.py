"""GET /requests/{id} - fetch a single request including its history."""

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
    request_id = (event.get("pathParameters") or {}).get("id")
    if not request_id:
        return _respond(400, {"error": "missing request id"})

    item = dynamodb.Table(TABLE).get_item(Key={"requestId": request_id}).get("Item")
    if not item:
        return _respond(404, {"error": "request not found"})

    item.pop("taskToken", None)  # never expose the workflow token
    return _respond(200, {"request": item})
