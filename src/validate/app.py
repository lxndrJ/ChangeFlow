"""Validate a change request inside the Step Functions workflow.

This function is deliberately pure and stateless: it holds no data-store
permissions at all, which keeps the least-privilege story clean. The state
machine branches on the returned boolean.
"""

VALID_IMPACTS = {"NONE", "LOW", "MEDIUM", "HIGH"}


def lambda_handler(event, context):
    reasons = []

    title = str(event.get("title") or "").strip()
    description = str(event.get("description") or "").strip()
    impact = str(event.get("impact") or "").strip().upper()
    part_number = str(event.get("partNumber") or "").strip()
    requested_by = str(event.get("requestedBy") or "").strip()

    if len(title) < 4:
        reasons.append("title must be at least 4 characters")
    if len(description) < 15:
        reasons.append("description must be at least 15 characters")
    if impact not in VALID_IMPACTS:
        reasons.append("impact must be one of NONE, LOW, MEDIUM, HIGH")
    if not part_number:
        reasons.append("partNumber is required")
    if not requested_by:
        reasons.append("requestedBy is required")

    return {"valid": not reasons, "reasons": reasons}
