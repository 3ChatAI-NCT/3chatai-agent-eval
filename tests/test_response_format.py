import json
from copy import deepcopy

import pytest
from test_candidate_model import CALL

from agent_eval import response_format
from agent_eval.api import _job_view
from agent_eval.atomic_report import compact_execution

SCHEMA = {
    "type": "object", "required": ["messages"],
    "properties": {"messages": {"type": "array", "items": {
        "type": "object", "required": ["message"],
        "properties": {"message": {"type": "string"},
                       "attachments": {"type": "array"}}}}},
}


def request():
    return {"input": [{"role": "system", "content": [
        {"type": "input_text", "text": "业务规则\n" * 2000 + "JSON Schema：\n"
         + json.dumps(SCHEMA)}]}]}


def output(raw):
    return {"messages": [raw], "tool_calls": []}


def job(raw='{"messages":"hello"}'):
    item = deepcopy(CALL)
    item["request"] = request()
    return {"id": "execute-1", "case_id": "case-1", "stage": "execute",
            "status": "succeeded", "error_code": None, "result_sha256": "frozen-hash",
            "input": {"mode": "atomic_replay", "item": item},
            "result": {"candidate": output(raw),
                       "baseline": output('{"messages":[{"message":"hello"}]}')}}


def test_result_projection_marks_string_messages_without_changing_frozen_evidence():
    stored = job()
    original = deepcopy(stored)
    view = _job_view(stored)
    check = view["result"]["candidate"]["format_check"]
    assert check["status"] == "invalid"
    assert check["issues"][0]["code"] == "messages_string"
    assert check["issues"][0]["severity"] == "major"
    assert check["issues"][0]["path"] == "/messages"
    assert view["result"]["baseline"]["format_check"]["status"] == "valid"
    assert stored == original and view["result_sha256"] == "frozen-hash"


@pytest.mark.parametrize(("raw", "code"), [
    ('{"messages":"hello"}', "messages_string"),
    ('{"messages":null}', "schema_violation"),
    ('{"messages":[{"message":123}]}', "schema_violation"),
    ('{"messages":["hello"]}', "schema_violation"),
    ('{"messages":[{"message":"hello","attachments":"file"}]}', "schema_violation"),
    ('{}', "schema_violation"),
    ('hello', "invalid_json"),
    ('```json\n{"messages":[]}\n```', "invalid_json"),
    ('{"messages":[]} trailing', "invalid_json"),
    ('', "invalid_json"),
])
def test_detects_protocol_violations(raw, code):
    result = response_format.check(request(), output(raw))
    assert result["status"] == "invalid"
    assert result["issues"][0]["code"] == code


@pytest.mark.parametrize("raw", ['{"messages":[]}', '{"messages":[{"message":"hello"}]}'])
def test_valid_json_and_empty_messages_are_not_protocol_errors(raw):
    assert response_format.check(request(), output(raw))["status"] == "valid"


def test_tool_calls_are_intermediate_steps_and_not_invalid_final_responses():
    tool = {**output("Let me check"), "tool_calls": [{"name": "lookup", "arguments": {}}]}
    result = response_format.check(request(), tool)
    assert result == {"status": "not_applicable", "reason": "tool_call", "issues": []}


def test_user_supplied_schema_and_requests_without_schema_do_not_impose_a_protocol():
    user = request()
    user["input"][0]["role"] = "user"
    assert response_format.check(user, output("hello"))["status"] == "not_applicable"
    assert response_format.check({}, output("hello"))["status"] == "not_applicable"
    assert response_format.check(request(), None)["status"] == "unavailable"


def test_local_schema_references_are_validated_but_remote_references_are_not_fetched():
    schema = deepcopy(SCHEMA)
    schema["$defs"] = {"Message": schema["properties"]["messages"]["items"]}
    schema["properties"]["messages"]["items"] = {"$ref": "#/$defs/Message"}
    body = {"input": [{"role": "system", "content": "JSON Schema: " + json.dumps(schema)}]}
    result = response_format.check(body, output('{"messages":[{"message":2}]}'))
    assert result["status"] == "invalid"
    schema["properties"]["messages"]["items"] = {"$ref": "https://example.test/schema"}
    body["input"][0]["content"] = "JSON Schema: " + json.dumps(schema)
    assert response_format.check(body, output('{"messages":[{}]}'))["status"] == "unavailable"


def test_format_counts_use_final_responses_as_denominator_and_survive_compaction():
    good = job('{"messages":[{"message":"hello"}]}')
    routing = job()
    routing["result"]["candidate"]["tool_calls"] = [{"name": "lookup", "arguments": {}}]
    executions = [job(), job("plain text"), good, routing]
    counts = response_format.summarize(executions)
    assert counts["candidate"] == {"checked": 3, "invalid": 2, "messages_string": 1,
                                    "invalid_json": 1, "not_applicable": 1, "unavailable": 0}
    assert counts["baseline"]["invalid"] == 0
    assert response_format.summarize([compact_execution(row) for row in executions]) == counts
