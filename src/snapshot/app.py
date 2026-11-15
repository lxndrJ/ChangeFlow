"""GET /requests/{id}/snapshot - presigned URL to the frozen S3 snapshot."""

import json
import os

import boto3

s3 = boto3.client("s3")
BUCKET = os.environ["BUCKET_NAME"]


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

    key = f"requests/{request_id}/snapshot.json"
    url = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": BUCKET, "Key": key},
        ExpiresIn=3600,
    )
    return _respond(200, {"requestId": request_id, "snapshotKey": key, "url": url})
