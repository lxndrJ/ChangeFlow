# Submission: ChangeFlow - serverless engineering-change approvals

## The problem

Small product-development teams need change control: an engineer proposes a change to a part, a human approves or rejects it, and the decision must be auditable afterwards. The tools that do this properly - PLM/ERP change-management modules - are built for enterprises: expensive, heavy, and they need someone to run them. A three-person team doesn't adopt that. It either improvises in spreadsheets or skips change control entirely.

ChangeFlow is the missing middle: a real change-approval workflow with frozen snapshots, an audit trail, and clean approve/reject history - built entirely from five managed AWS services, costing $0 while idle, with nothing to patch or babysit after deploy.

## What the build does, end to end

1. An engineer submits a change request (part, title, description, impact) via a static web UI on S3 (or `POST /requests` through API Gateway).
2. The `submit` Lambda freezes a canonical JSON snapshot of the request in S3 (like a frozen report snapshot in PLM practice: the record can't be silently rewritten later), persists the item to DynamoDB and starts the Step Functions workflow.
3. The state machine **validates** the request (a pure, stateless Lambda with zero data-store permissions), then **pauses** waiting for a human using the `waitForTaskToken` callback pattern with a configurable timeout. While it waits, it costs nothing.
4. An approver clicks Approve/Reject in the UI. The `decide` Lambda claims the request with a conditional DynamoDB update (`ConditionExpression: status = AWAITING_APPROVAL`) so double approvals are impossible even under concurrent clicks, then resolves the callback via `SendTaskSuccess`.
5. Approved requests are executed (terminal status + immutable audit record in S3); rejected, invalid or timed-out requests are closed the same way - `States.Timeout` is caught and the request is finalized as EXPIRED. The UI shows the whole event timeline live.

## Services and how they connect

- **API Gateway (HTTP API)**: four routes (submit, list/get, decision, snapshot URL), per-request pricing.
- **Lambda**: nine small Python 3.12 functions on Graviton arm64, each with least-privilege IAM (the validate function literally has no policies).
- **DynamoDB**: single table with a status GSI, on-demand billing, SSE. Conditional updates make approvals race-safe.
- **S3**: frozen request snapshots + immutable audit records; also hosts the static UI (one bucket for artifacts, one public website bucket).
- **Step Functions**: the workflow itself - validate, wait for a human via callback, branch on the decision, execute or close.

Everything is defined in one AWS SAM template (`sam build && sam deploy --guided`), including the state machine as an inline ASL definition and per-function IAM policies. `scripts/deploy-frontend.sh` wires the UI to the API, `scripts/smoke-test.sh` proves the whole flow with curl: submit -> approve -> EXECUTED, and `tests/simulate_e2e.py` verifies the entire workflow logic locally before any deploy (43 assertions over the real state machine definition: all four lifecycle paths, race safety, callback expiry).

## Why it scales to zero

No servers, no containers, no schedules: on-demand DynamoDB, pay-per-request Lambda/API Gateway, S3, Step Functions state transitions. An idle deployment bills **$0**/month; demo-scale usage stays within or near the free tier. After `sam deploy` there is nothing left to maintain - the timeout that expires unanswered requests lives in the state machine definition, not in a cron job.

## Reusability

The core pattern - submit, park a Step Functions task token in DynamoDB, let any external system answer through a plain HTTP call, resume the workflow - is a drop-in for any human-in-the-loop or two-person-rule flow: expense approvals, deploy gates, content sign-off, quota increases. Everything else (validation rules, impact levels, execution side effects) is one Lambda swap away.

## Demo video

[LINK] - shows submission through the web UI, the execution waiting in the Step Functions console, the approve click resuming the workflow, the final EXECUTED status with timeline, and the S3 snapshot/audit objects.
