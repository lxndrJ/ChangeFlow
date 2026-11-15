"""End-to-end simulation of ChangeFlow, no AWS account needed.

What this proves BEFORE you deploy anything:
- the real Step Functions definition (extracted from template.yaml, not a copy)
  is walked by a tiny ASL interpreter, so every state, path, choice variable
  and catch actually resolves,
- all nine Lambda handlers run against a fake boto3 (in-memory DynamoDB,
  S3, Step Functions) with the exact payloads the state machine produces,
- the full lifecycle works: submit -> validate -> callback -> approve/reject,
  plus the INVALID and timeout/EXPIRED branches,
- race safety: a second decision on an already-claimed request gets 409.

Run: python3 tests/simulate_e2e.py
"""

import importlib.util
import json
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# --------------------------------------------------------------------------
# Fake boto3 / botocore, injected before the handlers are imported.
# --------------------------------------------------------------------------


class ClientError(Exception):
    def __init__(self, error_response):
        self.response = error_response
        super().__init__(str(error_response))


def _fake_modules():
    botocore = types.ModuleType("botocore")
    botocore_exceptions = types.ModuleType("botocore.exceptions")
    botocore_exceptions.ClientError = ClientError
    botocore.exceptions = botocore_exceptions
    sys.modules["botocore"] = botocore
    sys.modules["botocore.exceptions"] = botocore_exceptions

    table = FakeTable("changeflow-requests")
    s3 = FakeS3()
    sfn = FakeSFN()

    def resource(name):
        assert name == "dynamodb", name
        return types.SimpleNamespace(Table=lambda t: table)

    def client(name):
        return {"s3": s3, "stepfunctions": sfn}[name]

    boto3 = types.ModuleType("boto3")
    boto3.resource = resource
    boto3.client = client
    sys.modules["boto3"] = boto3
    return table, s3, sfn


class FakeTable:
    def __init__(self, name):
        self.name = name
        self.items = {}

    def put_item(self, Item):
        self.items[Item["requestId"]] = Item
        return {}

    def get_item(self, Key):
        item = self.items.get(Key["requestId"])
        return {"Item": item} if item else {}

    def query(self, **kwargs):
        # statusIndex query
        status = list(kwargs["ExpressionAttributeValues"].values())[0]
        return {"Items": [i for i in self.items.values() if i.get("status") == status]}

    def scan(self):
        return {"Items": list(self.items.values())}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeNames=None,
                    ExpressionAttributeValues=None, ConditionExpression=None):
        item = self.items[Key["requestId"]]
        names = ExpressionAttributeNames or {}

        if ConditionExpression == "#st = :await":
            if item.get(names["#st"]) != ExpressionAttributeValues[":await"]:
                raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}})

        clauses = UpdateExpression[len("SET "):]
        for clause in _split_top(clauses):
            target, expr = clause.split(" = ", 1)
            target = names.get(target, target)
            if expr.startswith("list_append("):
                inner = expr[len("list_append("):-1]
                base, evt = _split_top(inner)
                list_name, empty_key = _split_top(base[len("if_not_exists("):-1])
                history = item.get(list_name) or ExpressionAttributeValues[empty_key]
                item[list_name] = list(history) + list(ExpressionAttributeValues[evt])
            else:
                item[target] = ExpressionAttributeValues[expr]
        return {}


def _split_top(s):
    out, depth = [], 0
    current = ""
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if depth == 0 and s.startswith(", ", i):
            out.append(current)
            current = ""
            i += 2
            continue
        current += ch
        i += 1
    out.append(current)
    return out


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.presigned = []

    def put_object(self, Bucket, Key, Body, ContentType):
        self.objects[(Bucket, Key)] = Body.decode("utf-8")
        return {}

    def generate_presigned_url(self, op, Params, ExpiresIn):
        self.presigned.append(Params["Key"])
        return f"https://presigned.test/{Params['Key']}"


class FakeSFN:
    def __init__(self):
        self.executions = []
        self.task_results = {}
        self.failures = []
        self.stale_tokens = set()

    def start_execution(self, stateMachineArn, name, input):
        self.executions.append({"arn": stateMachineArn, "name": name, "input": input})
        return {"executionArn": f"fake:{name}"}

    def send_task_success(self, taskToken, output):
        if taskToken in self.stale_tokens:
            raise ClientError({"Error": {"Code": "TaskTimedOut"}})
        self.task_results[taskToken] = json.loads(output)
        return {}

    def send_task_failure(self, taskToken, **kwargs):
        self.failures.append(taskToken)
        return {}


# --------------------------------------------------------------------------
# Import the real handlers.
# --------------------------------------------------------------------------

os.environ.setdefault("TABLE_NAME", "changeflow-requests")
os.environ.setdefault("BUCKET_NAME", "changeflow-artifacts")
os.environ.setdefault("STATE_MACHINE_ARN", "arn:aws:states:fake:stateMachine:changeflow-approval")

TABLE, S3, SFN = _fake_modules()


def load(name):
    path = os.path.join(ROOT, "src", name, "app.py")
    spec = importlib.util.spec_from_file_location(f"src.{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


HANDLERS = {m: load(m) for m in
            ["submit", "validate", "notify", "decide", "execute", "finalize", "list", "get", "snapshot"]}

# --------------------------------------------------------------------------
# Load the real ASL from template.yaml.
# --------------------------------------------------------------------------

def load_asl():
    text = open(os.path.join(ROOT, "template.yaml")).read()
    start = text.index("Fn::Sub: |") + len("Fn::Sub: |")
    lines = text[start:].splitlines()
    out, base_indent = [], None
    for line in lines:
        if not line.strip():
            out.append("")
            continue
        indent = len(line) - len(line.lstrip())
        if base_indent is None:
            base_indent = indent
        if indent < base_indent:
            break
        out.append(line[base_indent:])
    asl = "\n".join(out)
    for key, val in {
        "${ValidateFunction}": "validate",
        "${NotifyFunction}": "notify",
        "${ExecuteFunction}": "execute",
        "${FinalizeFunction}": "finalize",
        "${ApprovalTimeoutSeconds}": "3600",
    }.items():
        asl = asl.replace(key, val)
    return json.loads(asl)


ASL = load_asl()
TOKEN = "sim-token-0001"


def jsonpath(data, path, token):
    if path.startswith("$$."):
        assert path == "$$.Task.Token", path
        return token
    node = data
    for part in path[2:].split("."):
        node = node[part]
    return node


def resolve_parameters(params, data, token):
    # For arn:aws:states:::lambda:invoke, only the "Payload" block reaches the
    # function; "FunctionName" etc. are SDK parameters, not event content.
    if "Payload.$" in params:
        return json.loads(json.dumps(data))  # whole-input reference
    spec = params.get("Payload")
    if spec is None:
        spec = {k: v for k, v in params.items() if k not in ("FunctionName",)}
    out = {}
    for key, value in spec.items():
        if key.endswith(".$"):
            out[key[:-2]] = jsonpath(data, value, token)
        else:
            out[key] = value
    return out


def apply_result_path(data, result_path, value):
    if result_path is None:
        return data
    if result_path == "$":
        return value
    node = data
    parts = result_path[2:].split(".")
    for p in parts[:-1]:
        node = node.setdefault(p, {})
    node[parts[-1]] = value
    return data


def invoke(func_name, payload):
    return HANDLERS[func_name].lambda_handler(payload, None)


def run_state_machine(state_input, *, decision_fn, simulate_timeout=False):
    """Tiny ASL interpreter for the subset the workflow uses."""
    states = ASL["States"]
    data = state_input
    current = ASL["StartAt"]
    trace = []

    while True:
        state = states[current]
        trace.append(current)
        stype = state["Type"]

        if stype == "Choice":
            matched = False
            for choice in state["Choices"]:
                value = jsonpath(data, choice["Variable"], TOKEN)
                if "BooleanEquals" in choice and value == choice["BooleanEquals"]:
                    current, matched = choice["Next"], True
                    break
                if "StringEquals" in choice and value == choice["StringEquals"]:
                    current, matched = choice["Next"], True
                    break
            if not matched:
                current = state["Default"]
            continue

        if stype == "Task":
            resource = state["Resource"]
            if resource.endswith("waitForTaskToken"):
                payload = resolve_parameters(state["Parameters"], data, TOKEN)
                assert payload["token"] == TOKEN
                invoke("notify", payload)  # notify parks the token on the item

                if simulate_timeout:
                    catcher = next(c for c in state["Catch"] if "States.Timeout" in c["ErrorEquals"])
                    data = apply_result_path(data, catcher["ResultPath"], {"Error": "States.Timeout"})
                    current = catcher["Next"]
                    continue

                decision_fn()  # in real life: a human answers via POST /requests/{id}/decision
                decision_output = SFN.task_results[TOKEN]
                data = apply_result_path(data, state["ResultPath"], decision_output)
                current = state["Next"]
                continue

            payload = resolve_parameters(state["Parameters"], data, TOKEN)
            result = invoke(state["Parameters"]["FunctionName"], payload)
            data = apply_result_path(data, state["ResultPath"], {"Payload": result})
            if "End" in state:
                return data, trace
            current = state["Next"]
            continue

        raise AssertionError(f"unsupported state type {stype}")


# --------------------------------------------------------------------------
# HTTP-API event helpers (same shapes API Gateway v2 sends to Lambda).
# --------------------------------------------------------------------------

def api_post(body, path_params=None, query=None):
    return {
        "version": "2.0",
        "routeKey": "POST /x",
        "rawPath": "/x",
        "body": json.dumps(body),
        "isBase64Encoded": False,
        "pathParameters": path_params,
        "queryStringParameters": query,
    }


PASS = 0
def check(name, cond, detail=""):
    global PASS
    if not cond:
        print(f"FAIL  {name}  {detail}")
        sys.exit(1)
    PASS += 1
    print(f"ok    {name}")


# --------------------------------------------------------------------------

def submit_request(**overrides):
    body = {
        "partNumber": "SHIRT-COLLAR-0042",
        "title": "Switch collar interfacing to supplier B",
        "description": "Supplier B uses a fused interlining; update spec and process.",
        "impact": "MEDIUM",
        "requestedBy": "Alex",
    }
    body.update(overrides)
    return HANDLERS["submit"].lambda_handler(api_post(body), None)


def decide(request_id, decision, by="Lead", comment=None):
    event = api_post({"decision": decision, "by": by, "comment": comment}, path_params={"id": request_id})
    return HANDLERS["decide"].lambda_handler(event, None)


def history_events(item):
    return [h["event"] for h in item["history"]]


# 1) Submit ---------------------------------------------------------------
print("== 1. submit ==")
res = submit_request()
check("submit returns 201", res["statusCode"] == 201, res)
item = json.loads(res["body"])["request"]
rid = item["requestId"]
check("request id format", rid.startswith("cr-"))
check("item in DynamoDB", TABLE.get_item({"requestId": rid})["Item"]["status"] == "VALIDATING")
check("snapshot frozen in S3", (os.environ["BUCKET_NAME"], f"requests/{rid}/snapshot.json") in S3.objects)
check("snapshot content matches", json.loads(S3.objects[(os.environ["BUCKET_NAME"], f"requests/{rid}/snapshot.json")])["requestId"] == rid)
check("execution started", len(SFN.executions) == 1 and SFN.executions[0]["name"] == rid)
check("execution input carries requestId", json.loads(SFN.executions[0]["input"])["requestId"] == rid)
check("execution uses the right state machine", "stateMachine:changeflow-approval" in SFN.executions[0]["arn"])

bad = submit_request(title="ab", description="short")
# submit only checks presence; short-but-present passes through to validation
check("submit does not do length validation", bad["statusCode"] == 201)
rid_bad = json.loads(bad["body"])["request"]["requestId"]

missing = HANDLERS["submit"].lambda_handler(api_post({"partNumber": "X"}), None)
check("submit rejects missing fields with 400", missing["statusCode"] == 400)
wrong_impact = submit_request(impact="CATASTROPHIC")
check("submit rejects unknown impact with 400", wrong_impact["statusCode"] == 400)

# 2) Happy path: validate -> callback -> approve -> execute ----------------
print("== 2. approve path ==")
final, trace = run_state_machine(
    json.loads(SFN.executions[0]["input"]),
    decision_fn=lambda: check("decide returns 200", decide(rid, "approve", comment="LGTM")["statusCode"] == 200),
)
check("state trace is the approve path", trace == ["Validate", "Valid?", "NotifyApprover", "Approved?", "ApplyChange"], trace)
approved = TABLE.get_item({"requestId": rid})["Item"]
check("status EXECUTED", approved["status"] == "EXECUTED", approved)
check("history complete", history_events(approved) == ["SUBMITTED", "AWAITING_APPROVAL", "APPROVED", "EXECUTED"], approved["history"])
check("audit record in S3", json.loads(S3.objects[(os.environ["BUCKET_NAME"], f"audit/{rid}.json")])["type"] == "CHANGE_EXECUTED")
check("audit records approver", json.loads(S3.objects[(os.environ["BUCKET_NAME"], f"audit/{rid}.json")])["approvedBy"] == "Lead")
check("callback was answered with approve", SFN.task_results[TOKEN]["decision"] == "approve")

# 3) Reject path -----------------------------------------------------------
print("== 3. reject path ==")
res = submit_request(partNumber="B-100", title="Reject me please", impact="LOW")
rid2 = json.loads(res["body"])["request"]["requestId"]
final, trace = run_state_machine(
    json.loads(SFN.executions[-1]["input"]),
    decision_fn=lambda: decide(rid2, "reject", by="QA", comment="not this season"),
)
check("reject trace", trace == ["Validate", "Valid?", "NotifyApprover", "Approved?", "MarkRejected"], trace)
rejected = TABLE.get_item({"requestId": rid2})["Item"]
check("status REJECTED", rejected["status"] == "REJECTED")
check("reject history", history_events(rejected) == ["SUBMITTED", "AWAITING_APPROVAL", "REJECTED"])
audit = json.loads(S3.objects[(os.environ["BUCKET_NAME"], f"audit/{rid2}.json")])
check("reject audit", audit["outcome"] == "REJECTED" and audit["decidedBy"] == "QA", audit)

# 4) Invalid path -----------------------------------------------------------
print("== 4. invalid path ==")
final, trace = run_state_machine(
    json.loads(SFN.executions[1]["input"]),  # the short-title request from step 1
    decision_fn=lambda: None, simulate_timeout=False,
)
check("invalid trace", trace == ["Validate", "Valid?", "MarkInvalid"], trace)
invalid = TABLE.get_item({"requestId": rid_bad})["Item"]
check("status INVALID", invalid["status"] == "INVALID")
check("validation reasons in history", "title must be at least 4 characters" in invalid["history"][-1]["comment"], invalid["history"][-1])

# 5) Timeout path -----------------------------------------------------------
print("== 5. timeout path ==")
res = submit_request(partNumber="C-100", title="Nobody will approve this")
rid3 = json.loads(res["body"])["request"]["requestId"]
final, trace = run_state_machine(
    json.loads(SFN.executions[-1]["input"]), decision_fn=lambda: None, simulate_timeout=True,
)
check("timeout trace", trace == ["Validate", "Valid?", "NotifyApprover", "MarkExpired"], trace)
expired = TABLE.get_item({"requestId": rid3})["Item"]
check("status EXPIRED after timeout", expired["status"] == "EXPIRED")
check("expired audit in S3", json.loads(S3.objects[(os.environ["BUCKET_NAME"], f"audit/{rid3}.json")])["outcome"] == "EXPIRED")

# 6) Race safety: decide twice, and decide after execution ------------------
print("== 6. race safety ==")
res = submit_request(partNumber="D-100", title="Double click simulator")
rid4 = json.loads(res["body"])["request"]["requestId"]
item4 = TABLE.get_item({"requestId": rid4})["Item"]
item4["status"] = "AWAITING_APPROVAL"
item4["taskToken"] = "tok-" + rid4
first = decide(rid4, "approve", by="A")
second = decide(rid4, "reject", by="B")
check("first decision wins", first["statusCode"] == 200, first)
check("second decision blocked with 409", second["statusCode"] == 409, second)
check("table not corrupted", TABLE.get_item({"requestId": rid4})["Item"]["decidedBy"] == "A")

post_exec = decide(rid4, "approve")
check("decision after execution blocked", post_exec["statusCode"] == 409, post_exec)

SFN.stale_tokens.add("stale-token")
item4b = TABLE.get_item({"requestId": rid4})["Item"]
item4b["status"] = "AWAITING_APPROVAL"
item4b["taskToken"] = "stale-token"
stale = decide(rid4, "approve")
check("stale callback handled as 410", stale["statusCode"] == 410, stale)
check("stale request marked EXPIRED", TABLE.get_item({"requestId": rid4})["Item"]["status"] == "EXPIRED")

# 7) API read handlers ------------------------------------------------------
print("== 7. api read handlers ==")
listing = HANDLERS["list"].lambda_handler({"version": "2.0", "queryStringParameters": None}, None)
body = json.loads(listing["body"])
check("list returns all requests", body["count"] == 5, body["count"])
check("list strips task tokens", all("taskToken" not in r for r in body["requests"]))

by_status = HANDLERS["list"].lambda_handler({"version": "2.0", "queryStringParameters": {"status": "EXECUTED"}}, None)
check("status filter via GSI", json.loads(by_status["body"])["count"] == 1)

got = HANDLERS["get"].lambda_handler({"version": "2.0", "pathParameters": {"id": rid}}, None)
check("get returns request", json.loads(got["body"])["request"]["requestId"] == rid)
check("get strips task token", "taskToken" not in json.loads(got["body"])["request"])
missing_get = HANDLERS["get"].lambda_handler({"version": "2.0", "pathParameters": {"id": "cr-nope"}}, None)
check("get 404 for unknown id", missing_get["statusCode"] == 404)

snap = HANDLERS["snapshot"].lambda_handler({"version": "2.0", "pathParameters": {"id": rid}}, None)
check("snapshot returns presigned url", "https://presigned.test/requests/" in json.loads(snap["body"])["url"])

# 8) validate handler unit checks -------------------------------------------
print("== 8. validate ==")
v = HANDLERS["validate"].lambda_handler(
    {"title": "Long enough", "description": "Long enough description", "impact": "LOW",
     "partNumber": "X-1", "requestedBy": "A"}, None)
check("valid request passes", v == {"valid": True, "reasons": []}, v)
v = HANDLERS["validate"].lambda_handler({"title": "ab", "description": "short", "impact": "HUGE"}, None)
check("invalid request lists reasons", v["valid"] is False and len(v["reasons"]) >= 3, v)

print()
print(f"ALL {PASS} CHECKS PASSED - state machine, handlers and race safety behave as designed.")
